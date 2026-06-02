# pageless-print

Convert **any webpage into a single-page PDF** — one page sized to fit the
entire rendered page, with **real selectable text** (not a screenshot) and the
site's on-screen styling, colors, and layout preserved.

## Features

- **Exports a website as one PDF page** — fit to the content as tightly as
  possible (near-zero trailing blank space).
- **Real selectable text**, not an image — output is true PDF text.
- **Preserves desktop styling, layout, and colors** — renders at desktop width
  so responsive / media-query layouts resolve to their desktop view.
- **Viewport-unit (`vh`/`%`) correction** — pins viewport-relative elements to
  their on-screen size so the print layout matches the screen (no ballooning).
- **Supports dynamic content / lazy image loading** — runs real Chromium and
  waits for network idle, web fonts, and content height to stabilise.
- **Handles long / lazy-scroll pages** — autoscrolls to trigger lazy content
  (with a safety cap so infinite feeds still terminate).
- **Renders very tall pages as one tall page** — no pagination, no clipping.
- **Self-verifying** — checks page count + trailing blank in-process and
  binary-searches the tightest single-page height; re-renders so the output
  file always matches the reported metrics.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python pageless_pdf.py <url> [output.pdf]
```

Example:

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf
```

It prints a verification report when done:

```
=== RESULT ===
  PDF pages        : 1
  Page height      : 13266.0 CSS px  (9949.9 pt)
  Trailing blank   : 1.33 CSS px  (0.010% of page)
  Selectable text  : yes (Chromium print-to-PDF)
  Status           : PASS
```

## Not handled automatically

Infinite-scroll feeds (deliberately capped to terminate), interaction-gated
content (cookie banners, "load more" buttons, tabs), and authenticated /
login-walled pages.
