# pageless-print

Convert any webpage into a single-page PDF. One page sized to fit the entire
rendered page, with real selectable text (not a screenshot) and the site's
on-screen styling, colors and layout preserved.

## Features

- Exports a website as one PDF page fit tightly to the content with near zero
  trailing blank space
- Real selectable PDF text not an image
- Preserves desktop styling, layout and colors by rendering at desktop width so
  responsive media query layouts resolve to their desktop view
- Corrects viewport units (vh and percent) by pinning viewport relative
  elements to their on-screen size so the print layout matches the screen
- Supports dynamic content and lazy image loading by running real Chromium and
  waiting for network idle, web fonts and content height to stabilise
- Handles long lazy scroll pages by autoscrolling to trigger lazy content with
  a safety cap so infinite feeds still terminate
- Renders very tall pages as one tall page with no pagination and no clipping
- Self verifying. It checks page count and trailing blank in process then
  binary searches the tightest single page height and re renders so the output
  file always matches the reported metrics

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python pageless_pdf.py <url> [output.pdf]
```

Example

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf
```

It prints a verification report when done

```
=== RESULT ===
  PDF pages        : 1
  Page height      : 13266.0 CSS px  (9949.9 pt)
  Trailing blank   : 1.33 CSS px  (0.010% of page)
  Selectable text  : yes (Chromium print-to-PDF)
  Status           : PASS
```

## Not handled automatically

Infinite scroll feeds are capped so they terminate. Interaction gated content
such as cookie banners, load more buttons and tabs is not clicked.
Authenticated or login walled pages are not supported.
