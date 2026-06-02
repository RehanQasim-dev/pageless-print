# pageless-print

Convert **any webpage into a single-page PDF** — one page sized to fit the
entire rendered page, with **real selectable text** (not a screenshot) and the
site's on-screen styling, colors, and layout preserved.

The single page is fit to the content **as tightly as possible** (near-zero
trailing blank space), and the script **verifies its own output** and iterates
until the result is correct.

## Usage

```bash
python pageless_pdf.py <url> [output.pdf]
```

Example:

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf
```

## Install

```bash
pip install -r requirements.txt
playwright install chromium
# (or, if only the headless shell is missing:)
playwright install chromium-headless-shell
```

## How it works

1. **Render** the page in headless Chromium (Playwright) at a 1440px desktop
   width, using `emulate_media("screen")` so on-screen styling is preserved.
2. **Load fully**: wait for `load` → `networkidle`, autoscroll to trigger
   lazy-loaded / `IntersectionObserver` content, then wait for web fonts and
   for the content height to stabilise (catches late JS-rendered/streamed
   content).
3. **Freeze viewport units**: Chromium's print-to-PDF resolves `vh`/`%` heights
   against the *paper* height, not the screen — which silently balloons the
   layout and breaks single-page fitting. The script detects viewport-dependent
   elements generically (by diffing heights across two viewport sizes — no
   per-site selectors) and pins them to their on-screen pixel height so the
   print layout matches the desktop.
4. **Size to one page**: generate the PDF with the page height set to the
   measured content height.
5. **Self-correct**: verify the output entirely in-process with PyMuPDF (page
   count + trailing blank, measured by rasterising only the bottom strip — fast
   and memory-bounded even for very tall pages), then **binary-search** the
   smallest single-page height to minimise trailing blank, and re-render at that
   height so the output file always matches the reported metrics.

## Output

The script prints a verification report, e.g.:

```
=== RESULT ===
  PDF pages        : 1
  Page height      : 13266.0 CSS px  (9949.9 pt)
  Trailing blank   : 1.33 CSS px  (0.010% of page)
  Selectable text  : yes (Chromium print-to-PDF)
  Status           : PASS
```

## Supported / limitations

**Supported:** JS-rendered SPA content, lazy-on-scroll content, async/streamed
content, web-font reflow, and genuinely very tall pages (rendered as one tall
page).

**Not handled automatically:** infinite-scroll feeds (deliberately capped to
avoid never terminating), interaction-gated content (cookie banners, "load
more" buttons, tabs), and authenticated/login-walled pages.
