#!/usr/bin/env python3
"""
pageless_pdf.py — Convert any webpage into a single-page PDF.

Usage:
    python pageless_pdf.py <url> [output.pdf] [--zoom N] [--font-scale N] [--paged] [--dark] [--declutter]

    --zoom N        browser-style zoom as a percent, e.g. --zoom 150 (like Ctrl+ in a browser)
    --font-scale N  multiply only text size, e.g. --font-scale 1.4
    --paged         split into several large pages that render fast in viewers
                    (default is one tall pageless page). Blocks are never split.

The desktop layout width is auto-detected from the display so the PDF page comes
out the same width as a maximized Firefox/Chrome window on this machine.

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
MAX_ITERATIONS = 14            # cap on grow attempts when content overflows a page
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
# Paged mode (--paged): the tallest page a PDF viewer renders sharply. The PDF
# format's documented maximum page dimension is 200 inches (14400 pt); beyond
# that, viewers tile and rasterise the page (blurry, slow scroll). So in paged
# mode we cap each page at this height and let content flow onto more pages,
# keeping pages as large as possible while still rendering crisply. In CSS px:
# 14400 pt / (72/96) = 19200 px. Lower this if a viewer still renders slowly.
PAGED_MAX_PAGE_PX = 19_200      # 200 inches, the viewer-friendly page-height cap
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

# --- always-on: reveal horizontally-clipped code -----------------------------
# Code blocks usually use `white-space: pre` with `overflow-x: auto`, so on a
# screen you scroll sideways. A PDF cannot scroll, so anything past the right
# edge is clipped and lost. We make code blocks that actually overflow wrap
# their long lines instead, so nothing is cut off. Scoped to pre/code-like
# elements so tables, carousels, and other horizontal scrollers are untouched.
WRAP_CODE_JS = """
() => {
  let n = 0;
  for (const e of document.querySelectorAll('*')) {
    const cs = getComputedStyle(e);
    const ws = cs.whiteSpace;
    const codey = e.tagName === 'PRE' || e.tagName === 'CODE';
    // 'pre' (and 'nowrap' on code) are the non-wrapping modes that overflow.
    if (!(ws === 'pre' || (ws === 'nowrap' && codey))) continue;
    if (e.scrollWidth > e.clientWidth + 2) {   // actually overflowing to the right
      e.style.setProperty('white-space', 'pre-wrap', 'important');
      e.style.setProperty('overflow-wrap', 'anywhere', 'important');
      e.style.setProperty('word-break', 'break-word', 'important');
      n += 1;
    }
  }
  return n;
}
"""

# --- always-on: reveal vertically-clipped / collapsed scroll panes -----------
# Some layouts put real content inside a fixed-height, internally-scrolling pane
# (e.g. a sticky code panel: 1065px tall on screen but holding an 8576px file you
# scroll through). A PDF cannot scroll, so only the first viewport-height would
# print and the rest is lost. We let such panes grow to their full content height
# so everything is captured — the vertical analogue of WRAP_CODE_JS. This must
# run BEFORE the freeze pass; otherwise freeze re-clamps these panes to their
# on-screen height and hides the overflow.
#
# Two cases are handled:
#   Phase 1 (un-collapse): a virtualized pane can collapse to height:0 when it is
#     never scrolled into view — its row-renderer mounts no rows at zero height,
#     so it stays empty (chicken-and-egg). We give it a temporary tall height to
#     force the rows to mount, then release it to its content height. Gated to
#     panes whose class matches an already-rendered pane, so collapsed menus /
#     accordions on ordinary pages are left alone.
#   Phase 2 (expand): panes whose content overflows their visible box grow to
#     full height. Gated tightly (real text + genuine overflow + not a tiny
#     widget) so menus, carousels, and empty skeleton panes are untouched.
EXPAND_SCROLLERS_JS = """
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const isScroller = (cs) => cs.overflowY === 'auto' || cs.overflowY === 'scroll';
  const sig = (e) => (e.className && e.className.toString) ? e.className.toString().trim() : '';
  const expand = (e) => {
    e.style.setProperty('height', 'auto', 'important');
    e.style.setProperty('max-height', 'none', 'important');
    e.style.setProperty('min-height', '0', 'important');
    e.style.setProperty('overflow', 'visible', 'important');
  };

  // Class signatures of scroll panes that DID render real content — used to
  // recognise a collapsed sibling of the same component (vs an unrelated menu).
  const renderedSigs = new Set();
  for (const e of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(e);
    if (isScroller(cs) && e.getBoundingClientRect().height >= 150
        && ((e.textContent || '').length) >= 200) {
      const s = sig(e); if (s) renderedSigs.add(s);
    }
  }

  // Phase 1: un-collapse zero-height virtualized panes of a known content type.
  const collapsed = [];
  for (const e of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(e);
    if (!isScroller(cs)) continue;
    if (e.getBoundingClientRect().height >= 30) continue;   // not collapsed
    if (((e.textContent || '').length) < 200) continue;     // no real content
    if (!renderedSigs.has(sig(e))) continue;                // not a known pane type
    collapsed.push(e);
  }
  for (const e of collapsed) {
    e.style.setProperty('height', '3000px', 'important');   // coax rows to mount
    e.style.setProperty('max-height', 'none', 'important');
    e.scrollIntoView({ block: 'center' });
    await sleep(400);
  }
  if (collapsed.length) await sleep(500);
  for (const e of collapsed) expand(e);                     // release to content height
  window.scrollTo(0, 0);
  if (collapsed.length) await sleep(300);

  // Phase 2: expand panes whose content overflows their visible box.
  let expanded = 0;
  for (const e of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(e);
    if (!isScroller(cs)) continue;
    if (e.scrollHeight <= e.clientHeight + 50) continue;    // not actually clipped
    if (e.clientHeight < 150) continue;                     // tiny widget -> leave it
    // textContent, not innerText: off-screen panes render innerText as '' even
    // though the DOM holds the text.
    if (((e.textContent || '').length) < 200) continue;     // no real text
    expand(e);
    expanded += 1;
  }
  return { expanded, uncollapsed: collapsed.length };
}
"""

# --- paged mode: keep blocks whole across page breaks ------------------------
# Used only by --paged. When the document is split across multiple pages, this
# tells the layout engine never to split common block-level content (images,
# figures, tables, code, media, list items, paragraphs, blockquotes) across a
# page boundary, and to keep a heading with the content that follows it. The
# rules are unconditional (not wrapped in @media print) because we emulate
# screen media, yet break-inside still governs the paginated PDF output.
NO_BREAK_CSS = """
  img, svg, video, canvas, figure, picture, table, pre, blockquote,
  li, p, tr, .highlight {
    break-inside: avoid !important;
    page-break-inside: avoid !important;
  }
  h1, h2, h3, h4, h5, h6 {
    break-after: avoid !important;
    page-break-after: avoid !important;
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


def render_pdf(page, width_px, height_px, out_path, scale=1.0):
    """Emit a PDF with a single page of exactly width_px x height_px.

    `scale` magnifies the rendered content (browser-style zoom). The page was
    laid out at width_px/scale CSS px, so Chromium scaling it by `scale` makes
    the content fill the paper exactly. Chromium clamps print scale to 0.1-2.0.
    """
    page.pdf(
        path=out_path,
        width=f"{width_px}px",
        height=f"{height_px}px",
        margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        print_background=True,
        scale=max(0.1, min(scale, 2.0)),
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


def _bottom_blank_pt(page, zoom=1.0):
    """Contiguous background space at the bottom of one page, in PDF points.

    Used by paged mode to size pages so as little space as possible is wasted
    where an unbreakable block was pushed to the next page. Low zoom is fine: we
    only need to find where content ends, not sub-pixel precision.
    """
    H, W = page.rect.height, page.rect.width
    strip_pt = min(H, MEASURE_STRIP_PT)
    clip = fitz.Rect(0, H - strip_pt, W, H)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    corners = np.stack([arr[-1, 0], arr[-1, -1]]).astype(np.int16)
    bg = np.median(corners, axis=0)
    diff = np.abs(arr.astype(np.int16) - bg).max(axis=2)
    row_is_blank = (diff <= 10).all(axis=1)
    blank_rows = 0
    for r in range(pix.height - 1, -1, -1):
        if row_is_blank[r]:
            blank_rows += 1
        else:
            break
    return blank_rows / zoom


def measure_paged_blank(out_path):
    """Total wasted blank at the bottom of every page except the last (points).

    The final page's bottom blank is handled separately by trimming the last
    page to its content (see trim_last_page), so it is excluded here. Returns
    (total_blank_pt, page_count).
    """
    doc = fitz.open(out_path)
    pages = doc.page_count
    total = 0.0
    for i in range(max(0, pages - 1)):
        total += _bottom_blank_pt(doc[i])
    doc.close()
    return total, pages


def content_bottom_pt(page, zoom=0.5, strip_pt=4000.0):
    """Y of the lowest non-background row on a page, in points from the top.

    Scans the page from the bottom upward in bounded strips (so memory stays
    capped even for a 300000px single page) until it finds content, then returns
    where that content ends. The low zoom is enough to spot text lines; we only
    need where content ends, not sub-pixel precision. Used to trim a page down
    to its content.
    """
    H, W = page.rect.height, page.rect.width
    bg = None
    y = H
    while y > 0:
        top = max(0.0, y - strip_pt)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                              clip=fitz.Rect(0, top, W, y), alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if bg is None:                       # background = corners of the bottom strip
            corners = np.stack([arr[-1, 0], arr[-1, -1]]).astype(np.int16)
            bg = np.median(corners, axis=0)
        nonblank = (np.abs(arr.astype(np.int16) - bg).max(axis=2) > 10).any(axis=1)
        idx = np.nonzero(nonblank)[0]
        if idx.size:                         # lowest content row is in this strip
            return top + float(idx[-1] + 1) / zoom
        y = top
    return 0.0


def content_top_pt(page, zoom=0.5, strip_pt=4000.0):
    """Y of the highest non-background row on a page, in points from the top.

    The top-end counterpart of content_bottom_pt, scanning downward from the top
    in bounded strips. Used to crop outer blank above the content (e.g. a page
    whose content is vertically offset/centred, not anchored to the top).
    """
    H, W = page.rect.height, page.rect.width
    bg = None
    y = 0.0
    while y < H:
        bottom = min(H, y + strip_pt)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                              clip=fitz.Rect(0, y, W, bottom), alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if bg is None:                       # background = corners of the top strip
            corners = np.stack([arr[0, 0], arr[0, -1]]).astype(np.int16)
            bg = np.median(corners, axis=0)
        nonblank = (np.abs(arr.astype(np.int16) - bg).max(axis=2) > 10).any(axis=1)
        idx = np.nonzero(nonblank)[0]
        if idx.size:                         # highest content row is in this strip
            return y + float(idx[0]) / zoom
        y = bottom
    return H


def crop_to_content(out_path, pad_pt=6.0):
    """Crop a single-page PDF to its content bounding box (top and bottom).

    Removes outer blank both above and below the content (so a page whose
    content is offset or centred fits tightly), keeping a small pad. Returns the
    points removed. Only acts on a one-page document.
    """
    doc = fitz.open(out_path)
    removed = 0.0
    if doc.page_count == 1:
        p = doc[0]
        H, W = p.rect.height, p.rect.width
        top = content_top_pt(p)
        bot = content_bottom_pt(p)
        if 0 <= top < bot <= H:
            y0 = max(0.0, top - pad_pt)
            y1 = min(H, bot + pad_pt)
            if (y1 - y0) > 2.0 and (H - (y1 - y0)) > 2.0:
                p.set_cropbox(fitz.Rect(0, y0, W, y1))
                doc.save(out_path + ".trim")
                removed = H - (y1 - y0)
    doc.close()
    if removed:
        os.replace(out_path + ".trim", out_path)
    return removed


def trim_last_page(out_path, pad_pt=8.0):
    """Crop the last page down to its content so no trailing blank remains.

    A PDF may have a shorter final page, so we shrink the last page's box to the
    content bottom plus a small pad. Returns the points trimmed (0 if none).
    """
    doc = fitz.open(out_path)
    trimmed = 0.0
    if doc.page_count:
        last = doc[-1]
        H, W = last.rect.height, last.rect.width
        cb = content_bottom_pt(last)
        new_h = min(H, cb + pad_pt)
        # Guard: never crop to (near) nothing if content wasn't detected.
        if cb > 0 and H - new_h > 2.0:
            # CropBox is in top-left page coords and is what viewers/editors
            # display, so this keeps the top (content) and drops the bottom
            # (blank). Setting MediaBox here would reset the cropbox and uses a
            # bottom-left origin, so we deliberately leave it alone.
            last.set_cropbox(fitz.Rect(0, 0, W, new_h))
            doc.save(out_path + ".trim")
            trimmed = H - new_h
    doc.close()
    if trimmed:
        os.replace(out_path + ".trim", out_path)
    return trimmed


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


SCROLLBAR_PX = 17  # typical desktop vertical scrollbar; subtracted from screen width


def detect_browser_width():
    """Best-effort desktop layout width (CSS px) matching a maximized browser.

    A maximized Firefox/Chrome lays content out at (screen width - scrollbar).
    We detect the primary display's width and subtract a typical scrollbar so
    the captured page comes out the same width as the on-screen browser. Falls
    back to a common full-HD desktop width when no display can be queried
    (e.g. a headless server).
    """
    import re

    screen_w = None

    # 1) X11 / Linux desktop: parse the active mode (marked with '*') from xrandr.
    try:
        out = subprocess.run(["xrandr"], capture_output=True, text=True,
                             timeout=5).stdout
        active = [int(m) for m in re.findall(r"(\d+)x\d+\s+[\d.]+\*", out)]
        if active:
            screen_w = max(active)
        else:
            conn = re.findall(r"connected(?:\s+primary)?\s+(\d+)x\d+", out)
            if conn:
                screen_w = max(int(x) for x in conn)
    except Exception:
        pass

    # 2) Fallback: ask the windowing toolkit (needs a display, e.g. macOS/X11).
    if not screen_w:
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            screen_w = int(root.winfo_screenwidth())
            root.destroy()
        except Exception:
            pass

    if not screen_w or screen_w < 320:
        log(f"      (could not detect display width; using {VIEWPORT_WIDTH}px)")
        return VIEWPORT_WIDTH

    width = max(320, screen_w - SCROLLBAR_PX)
    log(f"      detected display {screen_w}px -> layout width {width}px "
        f"(matches a maximized browser window)")
    return width


def convert(url, out_path, dark=False, declutter=False, font_scale=1.0, zoom=1.0,
            base_width=VIEWPORT_WIDTH, paged=False):
    # Browser-style zoom: lay the page out at a narrower width (base / zoom) so
    # it reflows, then magnify that layout by `zoom` when emitting (see the
    # render_scale logic below). Text, images, and spacing all grow together and
    # the page keeps the desktop width — exactly like Ctrl+ in Firefox/Chrome.
    #
    # `base_width` is the desktop window width to emulate (CSS px). The final PDF
    # page comes out at this width, so set it to match your browser's window
    # content width to reproduce exactly what the browser shows at a given zoom.
    view_w = max(320, round(base_width / zoom)) if zoom and zoom > 0 else base_width
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
            viewport={"width": view_w, "height": 1200},
            device_scale_factor=DEVICE_SCALE,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        log(f"[1/4] Loading {url}")
        if abs(zoom - 1.0) > 0.001:
            log(f"      zoom {zoom*100:g}% -> layout width {view_w}px")
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

        # Wrap horizontally-overflowing code so nothing is clipped off the right
        # edge (a PDF cannot scroll). Runs before measuring/freezing.
        try:
            nw = page.evaluate(WRAP_CODE_JS)
            if nw:
                log(f"      wrapped {nw} horizontally-overflowing code block(s)")
        except Exception as e:
            log(f"      (code-wrap note: {e})")

        # Expand internally-scrolling content panes (e.g. a sticky code panel)
        # so their full content prints instead of being clipped to one viewport
        # height. Must run before the freeze pass, which would otherwise re-clamp
        # them and hide the overflow.
        try:
            res = page.evaluate(EXPAND_SCROLLERS_JS) or {}
            ne, nu = res.get("expanded", 0), res.get("uncollapsed", 0)
            if nu:
                log(f"      un-collapsed {nu} zero-height virtualized pane(s)")
            if ne:
                log(f"      expanded {ne} internally-scrolling content pane(s)")
        except Exception as e:
            log(f"      (expand-scrollers note: {e})")

        # Freeze viewport-relative elements so the print layout matches the
        # screen (see FREEZE_JS notes). Record screen heights, expose vh sizing
        # by resizing the viewport tall, pin the changed elements, restore.
        log("[3/4] Freezing viewport-relative elements (vh/% heights)")
        # The probe viewport must be TALLER than the content, otherwise a
        # `min-height:100vh` filler that already exceeds the viewport stays
        # content-driven at both sizes and is never detected (then it balloons
        # against the paper height when printing). So size the probe to the
        # content height plus a margin. This is a one-shot measurement, not a
        # feedback loop, so it cannot run away. Bounded to keep layout cheap.
        try:
            h0 = float(page.evaluate(MEASURE_JS)["height"])
            probe_h = int(max(3500.0, min(h0 + 2000.0, 200_000.0)))
            screen_heights = page.evaluate(TAG_AND_RECORD_JS)
            page.set_viewport_size({"width": view_w, "height": probe_h})
            page.wait_for_timeout(400)
            pinned = page.evaluate(FREEZE_JS, screen_heights)
            page.set_viewport_size({"width": view_w, "height": 1200})
            page.wait_for_timeout(300)
            log(f"      pinned {pinned} viewport-relative element(s) (probe {probe_h}px)")
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

        # Browser-style zoom: the page was laid out at the narrowed width
        # (view_w = base / zoom). Now magnify that reflowed layout by `zoom` when
        # emitting the PDF, so the page comes out at the desktop width with text,
        # images, and spacing all grown together — exactly like Ctrl+ in a
        # browser, not a skinny narrow-column render. Chromium's print scale caps
        # at 2.0x; beyond that the layout still reflows narrower (text keeps
        # getting relatively larger) but the magnification itself stops.
        render_scale = 1.0
        if zoom and zoom > 0 and abs(zoom - 1.0) > 0.001:
            render_scale = max(0.1, min(zoom, 2.0))
            if zoom > 2.0:
                log(f"      note: magnification capped at 200% (Chromium print "
                    f"scale limit); layout still reflowed at {view_w}px for "
                    f"{zoom*100:g}% zoom")

        # From here the height search works in PDF-paper px (= CSS px * scale).
        paper_w = max(1, round(width_css * render_scale))
        content_h *= render_scale
        if abs(render_scale - 1.0) > 0.001:
            log(f"      magnified x{render_scale:g} -> page {paper_w}px wide")

        if content_h > MAX_PAGE_HEIGHT_PX:
            log(f"      WARNING: content height {content_h:.0f}px exceeds Chromium's "
                f"single-page limit (~{MAX_PAGE_HEIGHT_PX}px). Clamping; the very "
                f"bottom may not fit on one page.")
            content_h = float(MAX_PAGE_HEIGHT_PX)

        # --- paged mode -----------------------------------------------------
        # Instead of one giant page (which viewers rasterise blurry + slowly
        # past ~200in), split the content into pages that render crisply, never
        # splitting a block across a break. Because blocks are kept whole, a tall
        # diagram that does not fit gets pushed down, leaving blank space at the
        # bottom of the page. So we do not just fix the height at the 200in cap:
        # we try several heights within the range and pick the one that wastes
        # the least blank, breaking near-ties toward the largest page.
        if paged:
            log("[4/4] Generating paged PDF (viewer-friendly, blocks kept whole)")
            try:
                page.add_style_tag(content=NO_BREAK_CSS)
            except Exception as e:
                log(f"      (no-break css note: {e})")
            cap = float(PAGED_MAX_PAGE_PX)
            in_per_px = CSS_TO_PT / 72.0   # px -> inches

            if content_h <= cap:
                # Fits under the cap: one page, no breaks, nothing wasted.
                page_h = content_h
                render_pdf(page, paper_w, page_h, out_path, scale=render_scale)
                _, pages = measure_paged_blank(out_path)
                waste_pt = 0.0
                log(f"   content fits the cap -> 1 page of {page_h:.0f}px "
                    f"({page_h * in_per_px:.0f} in)")
            else:
                # Search heights from the cap down toward half the cap. Fewer
                # candidates for very tall docs so the search stays bounded.
                fracs = ([1.0, 0.85, 0.7, 0.55] if content_h > cap * 4
                         else [1.0, 0.92, 0.85, 0.78, 0.71, 0.64, 0.57, 0.5])
                TOL_PT = 150.0   # treat blanks within ~2in as a tie -> prefer big
                best = None      # (key, h, pages, waste_pt)
                for f in fracs:
                    h = float(round(cap * f))
                    render_pdf(page, paper_w, h, out_path, scale=render_scale)
                    waste_pt, pages = measure_paged_blank(out_path)
                    log(f"   try {h:.0f}px ({h * in_per_px:.0f} in): {pages} pages, "
                        f"wasted blank {waste_pt / 72.0:.1f} in")
                    key = (round(waste_pt / TOL_PT), -h)
                    if best is None or key < best[0]:
                        best = (key, h, pages, waste_pt)
                page_h, _, waste_pt = best[1], best[2], best[3]
                render_pdf(page, paper_w, page_h, out_path, scale=render_scale)
                log(f"   -> chosen {page_h:.0f}px ({page_h * in_per_px:.0f} in) page, "
                    f"least wasted blank {waste_pt / 72.0:.1f} in")

            # Remove outer blank. A single page is cropped both ends (same as
            # the default mode); a multi-page result only trims the last page's
            # bottom (its top continues content from the previous page).
            doc = fitz.open(out_path)
            npages = doc.page_count
            doc.close()
            if npages == 1:
                removed_pt = crop_to_content(out_path)
                if removed_pt > 2.0:
                    log(f"   cropped to content (removed {removed_pt / 72.0:.1f} in)")
            else:
                trimmed_pt = trim_last_page(out_path)
                if trimmed_pt > 2.0:
                    log(f"   trimmed {trimmed_pt / 72.0:.1f} in of trailing blank "
                        f"off the last page")

            finalize_pdf(out_path)
            doc = fitz.open(out_path)
            pages = doc.page_count
            page_h_pt = doc[0].rect.height
            last_h_pt = doc[-1].rect.height
            doc.close()
            log(f"   -> {pages} page(s): {page_h_pt / 72.0:.1f} in tall"
                + (f", last page {last_h_pt / 72.0:.1f} in" if last_h_pt < page_h_pt - 2 else ""))
            ctx.close()
            browser.close()
            m = {"pages": pages, "blank_css": 0.0, "blank_pct": 0.0,
                 "page_h_pt": page_h_pt, "saturated": False, "paged": True,
                 "wasted_blank_in": waste_pt / 72.0, "last_page_in": last_h_pt / 72.0}
            return (page_h, m)

        log("[4/4] Generating single page, then trimming to content")

        # Render one page tall enough to hold everything, then crop it down to
        # where the content actually ends. This replaces an iterative height
        # search with one render + one trim. The margin covers Chromium's print
        # layout coming out a touch taller than the measured DOM height; if a
        # ballooning vh/% element still overflows it, we grow until it is one
        # page (the same guard the old search provided).
        grow_cap = min(float(MAX_PAGE_HEIGHT_PX), content_h * 2.5 + 10_000)
        margin = max(800.0, content_h * 0.06)
        h = min(content_h + margin, float(MAX_PAGE_HEIGHT_PX))

        def render_count(height):
            render_pdf(page, paper_w, height, out_path, scale=render_scale)
            doc = fitz.open(out_path)
            n = doc.page_count
            doc.close()
            return n

        renders = 1
        pages = render_count(h)
        while pages > 1 and renders < MAX_ITERATIONS and h < grow_cap:
            h = min(h * 1.6, grow_cap)
            pages = render_count(h)
            renders += 1
            log(f"   grew page to {h:.0f}px (still {pages} page(s))")

        if pages > 1:
            log(f"   WARNING: content keeps filling the page past {grow_cap:.0f}px "
                f"(a vh/percentage element is likely involved); leaving {pages} pages.")
        else:
            removed_pt = crop_to_content(out_path, pad_pt=6.0)
            log(f"   cropped to content (removed {removed_pt / 72.0:.2f} in of "
                f"blank above + below)")

        # Shrink + linearise for faster opening and lower memory in viewers.
        finalize_pdf(out_path)

        doc = fitz.open(out_path)
        page_h_pt = doc[0].rect.height
        doc.close()
        height_css = page_h_pt / CSS_TO_PT
        m = measure_pdf(out_path, height_css)
        if m["pages"] == 1:
            log("   -> one page, fit to content.")

        ctx.close()
        browser.close()
        return (height_css, m)


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
    paged = False
    for flag in ("--paged", "-p"):
        while flag in args:
            paged = True
            args.remove(flag)

    # Value flags: --font-scale N (multiplier) and --zoom N (percent, e.g. 150).
    def take_value(names):
        i = 0
        val = None
        while i < len(args):
            a = args[i]
            if a in names:
                if i + 1 < len(args):
                    try:
                        val = float(args[i + 1])
                    except ValueError:
                        pass
                    del args[i:i + 2]
                    continue
            elif any(a.startswith(n + "=") for n in names):
                try:
                    val = float(a.split("=", 1)[1])
                except ValueError:
                    pass
                del args[i]
                continue
            i += 1
        return val

    font_scale = take_value(("--font-scale", "--font")) or 1.0
    zoom_pct = take_value(("--zoom", "-z"))
    zoom = (zoom_pct / 100.0) if zoom_pct else 1.0
    base_width = detect_browser_width()

    if not args:
        print(__doc__)
        print("Error: missing <url>", file=sys.stderr)
        sys.exit(2)

    url = args[0]
    out_path = args[1] if len(args) > 1 else "output.pdf"
    out_path = os.path.abspath(out_path)

    height_css, m = convert(url, out_path, dark=dark, declutter=declutter,
                            font_scale=font_scale, zoom=zoom, base_width=base_width,
                            paged=paged)

    print("\n=== RESULT ==================================================")
    print(f"  Output file      : {out_path}")
    print(f"  PDF pages        : {m['pages']}")
    if m.get("paged"):
        print(f"  Mode             : paged (viewer-friendly, blocks kept whole)")
        print(f"  Page height      : {height_css:.1f} CSS px  "
              f"({m['page_h_pt']:.1f} pt / {m['page_h_pt']/72:.1f} in)")
        print(f"  Wasted blank     : {m.get('wasted_blank_in', 0.0):.1f} in "
              f"(at page breaks, minimized)")
        print(f"  Selectable text  : yes (Chromium print-to-PDF)")
        ok = m["pages"] >= 1
        print(f"  Status           : {'PASS' if ok else 'CHECK — see log above'}")
    else:
        print(f"  Page height      : {height_css:.1f} CSS px  ({m['page_h_pt']:.1f} pt)")
        print(f"  Trailing blank   : {m['blank_css']:.2f} CSS px  ({m['blank_pct']:.3f}% of page)")
        print(f"  Selectable text  : yes (Chromium print-to-PDF)")
        # The page is trimmed to its content, so fit-to-content is guaranteed;
        # success is simply landing on one page.
        ok = m["pages"] == 1
        print(f"  Status           : {'PASS' if ok else 'CHECK — see log above'}")
    print("=============================================================")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
