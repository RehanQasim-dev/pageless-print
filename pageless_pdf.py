#!/usr/bin/env python3
"""
pageless_pdf.py — Convert any webpage into a single-page PDF.

Usage:
    python pageless_pdf.py <url> [output.pdf] [--dark] [--declutter] [--font-scale N]

What it does
------------
Renders a webpage in a real (headless Chromium) browser at desktop width,
triggers lazy-loaded / JS-rendered content by scrolling, measures the full
rendered content size, then emits a PDF whose single page is sized to fit the
entire page exactly. The PDF contains real, selectable text (Chromium's
print-to-PDF, not a screenshot) and preserves screen styling/colors/layout.

Self-correction
---------------
After generating the PDF the script verifies its own output entirely in-process
using PyMuPDF:
  * the PDF must have exactly ONE page,
  * the content must be fully contained (no clipping / no overflow page),
  * trailing blank space at the bottom must be below a small threshold.
It iterates, shrinking/growing the page height, until those hold or a max
iteration cap is reached. No human needs to open the PDF.
"""

import sys
import os
import math
import shutil
import subprocess

import numpy as np
import fitz  # PyMuPDF
from playwright.sync_api import sync_playwright

# ---- tunables ---------------------------------------------------------------
VIEWPORT_WIDTH = 1440          # desktop layout width (CSS px)
DEVICE_SCALE = 1
MAX_ITERATIONS = 14
BLANK_THRESHOLD_PX = 4.0       # acceptable trailing blank space (CSS px)
SAFETY_PAD_PX = 1.0            # tiny pad to avoid sub-pixel overflow -> 2nd page
NAV_TIMEOUT_MS = 60_000
# Post-load settle: wait until content height is stable for N samples (or a
# timeout), so late JS-rendered / streamed content is captured.
STABILIZE_INTERVAL_MS = 500
STABILIZE_TIMEOUT_MS = 12_000
STABLE_SAMPLES = 3
CSS_TO_PT = 72.0 / 96.0        # CSS px -> PDF points
# Upper bound on a single page's height (CSS px). Chromium can emit pages far
# taller than the 200in PDF default, so this is generous; it only guards
# against pathological/runaway pages and unbounded memory.
MAX_PAGE_HEIGHT_PX = 300_000
# Blank-space detection: render only the bottom strip so we never allocate a
# giant pixmap for very tall pages.
MEASURE_ZOOM = 2.0             # pixmap px per PDF point (precision)
MEASURE_STRIP_PT = 3000.0      # how much of the page bottom to rasterise
# -----------------------------------------------------------------------------


# JS that scrolls the whole document to force lazy-loaded content to appear,
# then returns once the scroll height has stabilised.
AUTOSCROLL_JS = """
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const docH = () => Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement.scrollHeight
  );
  const vp = window.innerHeight || 1000;
  const start = docH();
  // Runaway guard: some pages append content endlessly while scrolling
  // (infinite feeds). Stop if the document blows past a sane multiple.
  const runawayCap = Math.max(start * 6, 30000);

  // Pass 1: progressive scroll, one ~viewport at a time, letting
  // viewport-triggered lazy content load. Bounded number of steps.
  let y = 0, guard = 0;
  while (y < docH() - vp && guard < 80) {
    y += Math.floor(vp * 0.85);
    window.scrollTo(0, y);
    await sleep(150);
    guard += 1;
    if (docH() > runawayCap) break;
  }

  // Pass 2: settle — wait for height to stabilise, but only a few gentle
  // checks so we don't keep poking an infinite feed.
  let last = -1, stable = 0, g2 = 0;
  while (stable < 2 && g2 < 12) {
    const h = docH();
    window.scrollTo(0, h);
    await sleep(200);
    if (docH() === last) { stable += 1; } else { stable = 0; }
    last = docH();
    g2 += 1;
    if (docH() > runawayCap) break;
  }
  window.scrollTo(0, 0);
  await sleep(150);
}
"""

# Returns the full rendered content box of the document (CSS px).
MEASURE_JS = """
() => {
  const de = document.documentElement;
  const body = document.body;
  const width = Math.max(
    de.scrollWidth, de.offsetWidth, de.clientWidth,
    body ? body.scrollWidth : 0, body ? body.offsetWidth : 0
  );
  const height = Math.max(
    de.scrollHeight, de.offsetHeight, de.clientHeight,
    body ? body.scrollHeight : 0, body ? body.offsetHeight : 0
  );
  return { width, height };
}
"""

# --- viewport-unit freezing ---------------------------------------------------
# Chromium's print-to-PDF resolves viewport units (vh/vw) and percentage
# heights against the *paper* size, not the on-screen viewport. On pages with
# vh-sized scroll containers (sticky sidebars, TOCs, capped code blocks) this
# makes the print layout balloon — often 2x+ taller — and breaks single-page
# fitting. We neutralise it generically (no per-site selectors): tag every
# element and record its on-screen height, resize the viewport tall so vh
# resolves large (as it will on the tall paper), then pin every element whose
# height changed back to its on-screen pixel height. The print layout then
# matches what a desktop browser shows.

# Tag each element and capture its current (on-screen) rendered height.
TAG_AND_RECORD_JS = """
() => {
  let i = 0; const rec = {};
  for (const e of document.querySelectorAll('body, body *')) {
    e.setAttribute('data-ppf', i);
    rec[i] = Math.round(e.getBoundingClientRect().height);
    i += 1;
  }
  return rec;
}
"""

# After the viewport has been resized tall, pin every element whose height
# changed (i.e. it was viewport-relative) back to its recorded on-screen height.
FREEZE_JS = """
(screen) => {
  let pinned = 0;
  for (const e of document.querySelectorAll('[data-ppf]')) {
    const i = e.getAttribute('data-ppf');
    const h0 = screen[i];
    if (h0 == null) continue;
    const h1 = Math.round(e.getBoundingClientRect().height);
    if (Math.abs(h1 - h0) > 4) {            // viewport-dependent element
      const cs = getComputedStyle(e);
      e.style.setProperty('height', h0 + 'px', 'important');
      e.style.setProperty('min-height', h0 + 'px', 'important');
      e.style.setProperty('max-height', h0 + 'px', 'important');
      const ov = cs.overflowY;
      if (ov === 'auto' || ov === 'scroll' || ov === 'visible') {
        e.style.setProperty('overflow', 'hidden', 'important');  // keep it clipped, as on screen
      }
      pinned += 1;
    }
  }
  return pinned;
}
"""

# --- optional font scaling ----------------------------------------------------
# Enlarge text without widening the page: multiply every element's font-size
# (and any px line-height) by a factor. Because the page width is unchanged,
# text reflows taller and ends up larger relative to the page, so it reads
# bigger at fit-width. Two passes (read all, then apply) so inherited font-size
# is not scaled twice. Unitless / "normal" line-heights follow the font on their
# own and are left alone.
FONT_SCALE_JS = """
(scale) => {
  const els = Array.from(document.querySelectorAll('*'));
  const data = els.map(e => {
    const cs = getComputedStyle(e);
    return [e, parseFloat(cs.fontSize), cs.lineHeight];
  });
  let n = 0;
  for (const [e, fs, lh] of data) {
    if (fs && !isNaN(fs)) {
      e.style.setProperty('font-size', (fs * scale) + 'px', 'important');
      if (lh && lh.endsWith('px')) {
        const v = parseFloat(lh);
        if (!isNaN(v)) e.style.setProperty('line-height', (v * scale) + 'px', 'important');
      }
      n += 1;
    }
  }
  return n;
}
"""

# --- optional declutter -------------------------------------------------------
# Conservative readability pass (opt-in). Removes things that are clearly not
# content and that hurt a static single-page PDF: ad iframes/slots, elements
# with unambiguous ad signals, and overlay clutter (cookie/consent banners,
# popups, chat widgets) when they are actually overlay-positioned. Finally it
# de-floats any remaining fixed/sticky element so nothing hovers over the page.
# Matching is tight (word boundaries + a curated host list) to avoid nuking real
# content, and every removal is reported (never silent).
DECLUTTER_JS = """
() => {
  const bodyTextLen = ((document.body && document.body.innerText) || '').length || 1;
  const samples = [], kept = [];
  const label = (e) => {
    let s = e.tagName.toLowerCase();
    if (e.id) s += '#' + e.id;
    const cls = (e.className && e.className.toString) ? e.className.toString().trim().split(/\\s+/).filter(Boolean).slice(0, 2).join('.') : '';
    if (cls) s += '.' + cls;
    return s.slice(0, 70);
  };
  // Safety net: never remove an element holding a big share of the page's text
  // (guards against a content wrapper that happens to match a keyword).
  const holdsContent = (e) => {
    const t = ((e.innerText || '').length);
    return t > 1500 && t > 0.25 * bodyTextLen;
  };
  const kill = (e, why) => {
    if (!e || !e.parentNode) return false;
    if (holdsContent(e)) { if (kept.length < 10) kept.push(why + ' KEPT(content) -> ' + label(e)); return false; }
    if (samples.length < 30) samples.push(why + ' -> ' + label(e));
    e.parentNode.removeChild(e);
    return true;
  };

  const AD_HOSTS = ['doubleclick.net','googlesyndication.com','googleadservices.com',
    'adservice.google','taboola.com','outbrain.com','adnxs.com','amazon-adsystem.com',
    'adsafeprotected.com','2mdn.net','criteo','pubmatic','rubiconproject','moatads'];
  // Strong, unambiguous ad-infrastructure signals only. Deliberately excludes
  // bare "ad"/"ads"/"sponsor"/"promo"/"banner" to avoid false positives such as
  // "Remove ads", "Our Sponsors", "header banner", "lead", "download".
  const reAdStrong = /(^|[-_])(adslot|adunit|adsbygoogle|advert(isement|ising)?|googlead|google-?ads?|dfp|gpt-?ad|ad-(slot|unit|container|wrapper|banner|box|region|rail|placeholder|label|wrap))([-_]|$)/i;
  // High-confidence consent/cookie terms.
  const reCookie = /(cookie|consent|gdpr|ccpa|onetrust|truste|cookiebar|cmp-)/i;
  // Other overlay clutter; only removed when it is a true overlay.
  const reOverlay = /(newsletter|subscribe|popup|modal|lightbox|backdrop|paywall|interstitial|chat-?widget|intercom|drift|livechat|tawk|zendesk)/i;

  let removed = 0;

  // 1) Ad iframes by host.
  for (const f of Array.from(document.querySelectorAll('iframe'))) {
    const src = (f.src || '') + ' ' + (f.getAttribute('data-src') || '');
    if (AD_HOSTS.some(h => src.indexOf(h) !== -1)) removed += kill(f, 'ad-iframe') ? 1 : 0;
  }

  // 2) Explicit ad tags / slots (data attributes are unambiguous).
  for (const e of Array.from(document.querySelectorAll(
      'ins.adsbygoogle, [data-ad-client], [data-ad-slot], [data-adunit], [data-google-query-id], [data-ad]'))) {
    removed += kill(e, 'ad-tag') ? 1 : 0;
  }

  // 3) Ad / overlay-clutter by class / id / aria / role.
  for (const e of Array.from(document.querySelectorAll('[class], [id], [aria-label]'))) {
    if (!e.parentNode) continue;
    const id = e.id || '';
    const cls = (e.className && e.className.toString) ? e.className.toString() : '';
    const aria = e.getAttribute('aria-label') || '';
    const role = e.getAttribute('role') || '';
    const hay = id + ' ' + cls;
    const cs = getComputedStyle(e);
    const z = parseInt(cs.zIndex, 10) || 0;
    const fixed = cs.position === 'fixed';
    const dialog = role === 'dialog' || role === 'alertdialog';
    const overlay = fixed || cs.position === 'sticky' || dialog || z >= 100;
    let why = null;
    if (reAdStrong.test(' ' + hay + ' ') || /advertis/i.test(aria)) why = 'ad';
    else if (reCookie.test(hay) && overlay) why = 'cookie/consent';
    else if (reOverlay.test(hay) && (fixed || dialog)) why = 'overlay';   // stricter for these
    if (why) removed += kill(e, why) ? 1 : 0;
  }

  // 4) De-float only position:fixed elements (they hover at the viewport in a
  // tall PDF). Sticky already collapses to its in-flow spot when printing, so
  // we leave it alone to avoid side effects.
  let defloated = 0;
  for (const e of Array.from(document.querySelectorAll('body *'))) {
    if (!e.parentNode) continue;
    if (getComputedStyle(e).position === 'fixed') {
      e.style.setProperty('position', 'static', 'important');
      defloated += 1;
    }
  }
  return { removed, defloated, samples, kept };
}
"""

# --- optional soft dark mode --------------------------------------------------
# Generic dark mode that KEEPS TEXT SELECTABLE. A CSS `filter` on the root would
# force Chromium to rasterise the whole page (losing real PDF text), so instead
# we recolour each element's actual colour properties: flip every colour's
# *lightness* (in HSL) while preserving hue and saturation, softly (so light
# backgrounds become ~#1e1e1e and dark text becomes ~#e6e6e6, never pure
# black/white). Images/videos are left untouched. Already-dark pages are
# skipped. Because only colours change (not `filter`), text stays selectable.
DARK_JS = """
() => {
  const parse = (c) => {
    const m = c && c.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const p = m[1].split(',').map(s => parseFloat(s.trim()));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const rgb2hsl = (r, g, b) => {
    r/=255; g/=255; b/=255;
    const mx=Math.max(r,g,b), mn=Math.min(r,g,b); let h=0,s=0,l=(mx+mn)/2;
    if (mx!==mn){ const d=mx-mn; s=l>0.5?d/(2-mx-mn):d/(mx+mn);
      if (mx===r) h=(g-b)/d+(g<b?6:0); else if (mx===g) h=(b-r)/d+2; else h=(r-g)/d+4; h/=6; }
    return [h,s,l];
  };
  const hue2 = (p,q,t) => { if(t<0)t+=1; if(t>1)t-=1;
    if(t<1/6)return p+(q-p)*6*t; if(t<1/2)return q; if(t<2/3)return p+(q-p)*(2/3-t)*6; return p; };
  const hsl2rgb = (h,s,l) => {
    let r,g,b;
    if (s===0){ r=g=b=l; } else { const q=l<0.5?l*(1+s):l+s-l*s, p=2*l-q;
      r=hue2(p,q,h+1/3); g=hue2(p,q,h); b=hue2(p,q,h-1/3); }
    return [Math.round(r*255), Math.round(g*255), Math.round(b*255)];
  };
  // Soft lightness flip: L -> 0.92 - 0.84*L  (white 1->0.08, black 0->0.92).
  const flip = (c) => {
    const [h,s,l] = rgb2hsl(c.r,c.g,c.b);
    const nl = 0.92 - 0.84*l;
    const [r,g,b] = hsl2rgb(h,s,nl);
    return `rgba(${r}, ${g}, ${b}, ${c.a})`;
  };

  // Skip pages that are already dark.
  const bodyBg = parse(getComputedStyle(document.body || document.documentElement).backgroundColor);
  if (bodyBg && bodyBg.a > 0.1) {
    const lum = (0.2126*bodyBg.r + 0.7152*bodyBg.g + 0.0722*bodyBg.b)/255;
    if (lum < 0.35) return { applied: false, reason: 'page already dark' };
  }

  // Pass 1: read every element's ORIGINAL colours before mutating anything.
  // (Reading after writing would double-flip inherited properties like `color`.)
  const els = Array.from(document.querySelectorAll('*'));
  const sides = ['Top','Right','Bottom','Left'];
  const plan = els.map((e) => {
    const cs = getComputedStyle(e);
    const bg = parse(cs.backgroundColor);
    const col = parse(cs.color);
    const borders = sides.map(s => parse(cs['border'+s+'Color']));
    return { e, bg, col, borders };
  });

  // Pass 2: apply the flipped colours.
  let n = 0;
  for (const pl of plan) {
    const { e, bg, col, borders } = pl;
    if (bg && bg.a > 0.05) e.style.setProperty('background-color', flip(bg), 'important');
    if (col && col.a > 0.05) e.style.setProperty('color', flip(col), 'important');
    borders.forEach((bc, i) => {
      if (bc && bc.a > 0.05)
        e.style.setProperty('border-'+sides[i].toLowerCase()+'-color', flip(bc), 'important');
    });
    n += 1;
  }

  // Dark fallback for transparent roots (so gaps/paper show dark, not white).
  const rootBg = parse(getComputedStyle(document.documentElement).backgroundColor);
  if (!rootBg || rootBg.a < 0.05)
    document.documentElement.style.setProperty('background-color', '#1a1a1a', 'important');
  if (document.body) {
    const bb = parse(getComputedStyle(document.body).backgroundColor);
    if (!bb || bb.a < 0.05)
      document.body.style.setProperty('background-color', '#1a1a1a', 'important');
  }
  return { applied: true, recolored: n };
}
"""


def log(msg):
    print(msg, flush=True)


def render_pdf(page, width_css, height_css, out_path):
    """Emit a PDF with a single page of exactly width_css x height_css (CSS px)."""
    page.pdf(
        path=out_path,
        width=f"{width_css}px",
        height=f"{height_css}px",
        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        print_background=True,
        scale=1,
        prefer_css_page_size=False,
    )


def measure_pdf(out_path, page_height_css):
    """
    Inspect the produced PDF *in process* (no human reading required).

    Returns dict:
      pages        : number of PDF pages
      blank_css    : trailing blank space at bottom of page 1, in CSS px
      blank_pct    : that blank space as % of page height
      page_h_pt    : page-1 height in PDF points
    """
    doc = fitz.open(out_path)
    pages = doc.page_count
    page = doc[0]
    page_w_pt = page.rect.width
    page_h_pt = page.rect.height

    # Rasterise only the bottom strip -> bounded memory even for very tall pages.
    strip_pt = min(page_h_pt, MEASURE_STRIP_PT)
    clip = fitz.Rect(0, page_h_pt - strip_pt, page_w_pt, page_h_pt)
    pix = page.get_pixmap(matrix=fitz.Matrix(MEASURE_ZOOM, MEASURE_ZOOM),
                          clip=clip, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    doc.close()

    # Background reference = median of the two BOTTOM corners. The very bottom
    # corners are almost always page background, so a coloured footer bar (a
    # different colour) is correctly treated as content, not as blank space.
    corners = np.stack([arr[-1, 0], arr[-1, -1]]).astype(np.int16)
    bg = np.median(corners, axis=0)

    tol = 10  # per-channel colour tolerance
    diff = np.abs(arr.astype(np.int16) - bg).max(axis=2)
    row_is_blank = (diff <= tol).all(axis=1)  # row is blank iff every px ~= bg

    # Count contiguous blank rows from the bottom upward.
    blank_rows = 0
    for r in range(pix.height - 1, -1, -1):
        if row_is_blank[r]:
            blank_rows += 1
        else:
            break

    blank_pt = blank_rows / MEASURE_ZOOM           # pixmap px -> PDF points
    blank_css = blank_pt / CSS_TO_PT               # points -> CSS px
    saturated = blank_rows == pix.height           # blank filled the whole strip
    blank_pct = (blank_css / page_height_css) * 100.0 if page_height_css else 0.0
    return {
        "pages": pages,
        "blank_css": blank_css,
        "blank_pct": blank_pct,
        "page_h_pt": page_h_pt,
        "saturated": saturated,
    }


def finalize_pdf(out_path):
    """Shrink the final PDF and, if a tool is available, linearise it.

    This does NOT change page count, layout, or selectable text. It only lowers
    file size and memory footprint (garbage-collect + recompress) and enables
    fast-web-view (linearisation) so viewers open and stream it faster. It does
    not change the per-tile rasterisation cost of a very tall page.
    """
    try:
        before = os.path.getsize(out_path)
    except OSError:
        return
    # Dependency-free: garbage-collect unused objects + recompress streams/fonts.
    try:
        doc = fitz.open(out_path)
        tmp = out_path + ".opt"
        doc.save(tmp, garbage=4, deflate=True, deflate_fonts=True, clean=True)
        doc.close()
        os.replace(tmp, out_path)
    except Exception as e:
        log(f"      (finalize note: {e})")
        return
    # Optional: linearise for faster first paint, if qpdf or mutool is present.
    linearised = False
    qpdf = shutil.which("qpdf")
    mutool = shutil.which("mutool")
    try:
        tmp = out_path + ".lin"
        if qpdf:
            subprocess.run([qpdf, "--linearize", out_path, tmp],
                           check=True, capture_output=True, timeout=180)
            os.replace(tmp, out_path); linearised = True
        elif mutool:
            subprocess.run([mutool, "clean", "-l", out_path, tmp],
                           check=True, capture_output=True, timeout=180)
            os.replace(tmp, out_path); linearised = True
    except Exception:
        pass  # linearisation is a bonus; ignore if the tool errors
    after = os.path.getsize(out_path)
    log(f"      finalized: {before/1e6:.2f} MB -> {after/1e6:.2f} MB"
        + (" (linearized)" if linearised else ""))


def convert(url, out_path, dark=False, declutter=False, font_scale=1.0):
    with sync_playwright() as pw:
        # Prefer the full Chromium build if present (headless-shell may be
        # missing); fall back to whatever Playwright resolves by default.
        launch_kwargs = dict(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"],
        )
        try:
            exe = pw.chromium.executable_path
            if exe and os.path.exists(exe):
                launch_kwargs["executable_path"] = exe
        except Exception:
            pass
        browser = pw.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": 1200},
            device_scale_factor=DEVICE_SCALE,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        log(f"[1/4] Loading {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        for state in ("load", "networkidle"):
            try:
                page.wait_for_load_state(state, timeout=NAV_TIMEOUT_MS)
            except Exception:
                pass  # some pages never go fully idle (polling, analytics) — proceed

        log("[2/4] Scrolling to trigger lazy-loaded / JS content")
        try:
            page.evaluate(AUTOSCROLL_JS)
        except Exception as e:
            log(f"      (autoscroll note: {e})")
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # Wait for the page to actually settle: web fonts finished loading and
        # the content height stable for a few consecutive samples. This catches
        # JS that renders/streams content after the network goes idle.
        try:
            page.evaluate("() => document.fonts ? document.fonts.ready : null")
        except Exception:
            pass
        last_h, stable, waited = -1, 0, 0
        while stable < STABLE_SAMPLES and waited < STABILIZE_TIMEOUT_MS:
            try:
                h = page.evaluate(
                    "() => Math.max(document.body ? document.body.scrollHeight : 0,"
                    " document.documentElement.scrollHeight)"
                )
            except Exception:
                break
            stable = stable + 1 if h == last_h else 0
            last_h = h
            page.wait_for_timeout(STABILIZE_INTERVAL_MS)
            waited += STABILIZE_INTERVAL_MS
        log(f"      content settled at {last_h}px")

        # Preserve on-screen styling (not Chromium's default print stylesheet).
        page.emulate_media(media="screen")

        # Declutter must run before measuring/freezing since it changes layout.
        if declutter:
            log("      Decluttering (ads / overlays / fixed elements)")
            try:
                res = page.evaluate(DECLUTTER_JS)
                log(f"      decluttered: removed {res.get('removed', 0)} element(s), "
                    f"de-floated {res.get('defloated', 0)} fixed element(s)")
                for s in res.get("samples", [])[:8]:
                    log(f"        - {s}")
                for s in res.get("kept", [])[:5]:
                    log(f"        - {s}")
            except Exception as e:
                log(f"      (declutter note: {e})")

        # Enlarge text before measuring/freezing (it reflows the layout).
        if font_scale and abs(font_scale - 1.0) > 0.001:
            try:
                n = page.evaluate(FONT_SCALE_JS, font_scale)
                log(f"      font scaled x{font_scale:g} ({n} element(s))")
            except Exception as e:
                log(f"      (font-scale note: {e})")

        # Freeze viewport-relative elements so the print layout matches the
        # screen (see FREEZE_JS notes). Record screen heights, expose vh sizing
        # by resizing the viewport tall, pin the changed elements, restore.
        log("[3/4] Freezing viewport-relative elements (vh/% heights)")
        # Use a fixed, modest probe viewport (never derived from content
        # height) so a `min-height:100vh` filler can't drive an ever-growing
        # viewport. A 3500px probe is tall enough to expose vh-sized scroll
        # containers (they change height) while staying cheap and stable.
        PROBE_VIEWPORT_H = 3500
        try:
            screen_heights = page.evaluate(TAG_AND_RECORD_JS)
            page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": PROBE_VIEWPORT_H})
            page.wait_for_timeout(400)
            pinned = page.evaluate(FREEZE_JS, screen_heights)
            page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": 1200})
            page.wait_for_timeout(300)
            log(f"      pinned {pinned} viewport-relative element(s)")
        except Exception as e:
            log(f"      (freeze note: {e})")

        if dark:
            try:
                res = page.evaluate(DARK_JS)
                if res.get("applied"):
                    log(f"      dark mode applied (recoloured {res.get('recolored', 0)} element(s))")
                else:
                    log(f"      dark mode skipped: {res.get('reason', 'n/a')}")
            except Exception as e:
                log(f"      (dark mode note: {e})")

        dims = page.evaluate(MEASURE_JS)
        width_css = math.ceil(dims["width"])
        content_h = float(dims["height"])
        log(f"      rendered content: {width_css} x {content_h:.1f} CSS px")

        if content_h > MAX_PAGE_HEIGHT_PX:
            log(f"      WARNING: content height {content_h:.0f}px exceeds Chromium's "
                f"single-page limit (~{MAX_PAGE_HEIGHT_PX}px). Clamping; the very "
                f"bottom may not fit on one page.")
            content_h = float(MAX_PAGE_HEIGHT_PX)

        log("[4/4] Generating + self-verifying PDF")

        renders = 0

        def attempt(h):
            nonlocal renders
            renders += 1
            render_pdf(page, width_css, h, out_path)
            m = measure_pdf(out_path, h)
            log(
                f"   try {renders}: page_height={h:.1f}px  pages={m['pages']}  "
                f"trailing_blank={m['blank_css']:.2f}px ({m['blank_pct']:.3f}%)"
            )
            return m

        def good_enough(m):
            return m["pages"] == 1 and (m["blank_css"] <= BLANK_THRESHOLD_PX
                                        or m["blank_pct"] <= 0.5)

        # Phase 1: find a height that yields exactly one page (grow if the
        # initial guess overflows — print layout can be a touch taller).
        hi = content_h + SAFETY_PAD_PX           # smallest known 1-page height
        hi_m = None
        lo = None                                # largest known 2+page height
        grow = max(50.0, content_h * 0.04)
        h = hi
        while renders < MAX_ITERATIONS:
            m = attempt(h)
            if m["pages"] == 1:
                hi, hi_m = h, m
                break
            lo = h                               # this height is too short
            h += grow
            grow *= 1.7
        best = (hi, hi_m) if hi_m is not None else (h, m)

        # Phase 2: binary-search the smallest single-page height to squeeze out
        # trailing blank. We bracket the 1-page / 2-page boundary: any height
        # below it overflows, so the boundary is the tightest possible fit.
        if hi_m is not None and not good_enough(hi_m):
            while renders < MAX_ITERATIONS and not good_enough(best[1]):
                if lo is None:
                    # No 2-page bracket yet: jump down by the current blank to
                    # try to create one (or land directly on a tighter fit).
                    cand = hi - hi_m["blank_css"] - SAFETY_PAD_PX
                    if hi - cand < 1.0:
                        break
                    m = attempt(cand)
                    if m["pages"] == 1:
                        hi, hi_m = cand, m
                        best = (hi, hi_m)
                    else:
                        lo = cand
                else:
                    if hi - lo <= 1.0:
                        break
                    mid = (hi + lo) / 2.0
                    m = attempt(mid)
                    if m["pages"] == 1:
                        hi, hi_m = mid, m
                        best = (hi, hi_m)
                    else:
                        lo = mid

        # Re-render at the chosen height so the file on disk matches the report.
        best_h = best[0]
        final_m = attempt(best_h)
        best = (best_h, final_m)
        if good_enough(final_m):
            log("   -> converged: one page, content contained, blank minimal.")
        elif final_m["pages"] == 1:
            log("   -> tightest single page reached (residual blank is the "
                "page's own bottom whitespace).")
        else:
            log("   -> could not fit one page within limits; see warning above.")

        # Shrink + linearise for faster opening and lower memory in viewers.
        finalize_pdf(out_path)

        ctx.close()
        browser.close()
        return best


def main():
    args = sys.argv[1:]
    dark = False
    declutter = False
    for flag in ("--dark", "-d"):
        while flag in args:
            dark = True
            args.remove(flag)
    for flag in ("--declutter", "-c"):
        while flag in args:
            declutter = True
            args.remove(flag)

    # --font-scale N  /  --font-scale=N  /  --font N
    font_scale = 1.0
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--font-scale", "--font"):
            if i + 1 < len(args):
                try:
                    font_scale = float(args[i + 1])
                except ValueError:
                    pass
                del args[i:i + 2]
                continue
        elif a.startswith("--font-scale=") or a.startswith("--font="):
            try:
                font_scale = float(a.split("=", 1)[1])
            except ValueError:
                pass
            del args[i]
            continue
        i += 1

    if not args:
        print(__doc__)
        print("Error: missing <url>", file=sys.stderr)
        sys.exit(2)

    url = args[0]
    out_path = args[1] if len(args) > 1 else "output.pdf"
    out_path = os.path.abspath(out_path)

    height_css, m = convert(url, out_path, dark=dark, declutter=declutter,
                            font_scale=font_scale)

    page_w_pt = (VIEWPORT_WIDTH) * CSS_TO_PT  # informational
    print("\n=== RESULT ==================================================")
    print(f"  Output file      : {out_path}")
    print(f"  PDF pages        : {m['pages']}")
    print(f"  Page height      : {height_css:.1f} CSS px  ({m['page_h_pt']:.1f} pt)")
    print(f"  Trailing blank   : {m['blank_css']:.2f} CSS px  ({m['blank_pct']:.3f}% of page)")
    print(f"  Selectable text  : yes (Chromium print-to-PDF)")
    ok = m["pages"] == 1 and (m["blank_css"] <= BLANK_THRESHOLD_PX
                              or m["blank_pct"] <= 0.5)
    print(f"  Status           : {'PASS' if ok else 'CHECK — see iterations above'}")
    print("=============================================================")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
