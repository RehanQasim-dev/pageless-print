# pageless-print

Convert any webpage into a single-page PDF. One page sized to fit the entire
rendered page, with real selectable text (not a screenshot) and the site's
on-screen styling, colors and layout preserved.

## Features

- Exports a website as one PDF page fit tightly to the content with near zero
  trailing blank space
- Real selectable PDF text not an image
- Preserves desktop styling, layout and colors by rendering at desktop width so
  responsive media query layouts resolve to their desktop view. The width is
  auto-detected from the display so the page matches a maximized browser window
- Corrects viewport units (vh and percent) by pinning viewport relative
  elements to their on-screen size so the print layout matches the screen
- Supports dynamic content and lazy image loading by running real Chromium and
  waiting for network idle, web fonts and content height to stabilise
- Handles long lazy scroll pages by autoscrolling to trigger lazy content with
  a safety cap so infinite feeds still terminate
- Renders very tall pages as one tall page with no pagination and no clipping
- Wraps horizontally scrolling code blocks so long lines are not cut off the
  right edge, since a PDF cannot scroll sideways
- Reveals content trapped in internally scrolling panes such as sticky code
  panels by expanding them to full height, and un-collapses zero height
  virtualized panes that never mounted, so nothing is clipped vertically
- Optimizes the output by garbage collecting, recompressing and linearizing so
  it opens faster and uses less memory in viewers, without changing layout or
  text. Linearization uses qpdf or mutool if present
- Optional browser style zoom with `--zoom N` as a percent like `--zoom 150`
  that reflows and magnifies the page just like Ctrl plus in a browser, while
  keeping the page at the desktop width
- Optional font scaling with `--font-scale N` that enlarges only text while
  keeping the page width fixed, so the text reflows larger and reads bigger at
  fit width. Useful for sites with small type such as deepwiki
- Optional soft dark mode with `--dark` that flips colors by lightness so
  backgrounds become soft dark and text soft light, keeps hues and images
  intact, leaves already dark pages untouched and keeps text selectable
- Optional cautious declutter with `--declutter` that removes ad iframes and ad
  slots, removes overlay clutter like cookie banners and popups when they are
  truly overlay positioned, and de-floats fixed elements so nothing hovers over
  the page. Matching is tight to avoid false positives, a content element is
  never removed, and every removal is logged
- Optional paged output for editors and fast scrolling. A single very tall page
  renders blurry and slow in viewers past about 200 inches, so these modes split
  the content into pages that render crisply, never splitting a diagram, table,
  code block or paragraph across a break
  - `--paged` chooses the page height that wastes the least blank at the breaks,
    keeping pages as large as possible, and trims the last page to its content
  - `--paged2` keeps every page the same height and chooses the height that
    minimizes blank summed over all pages, packing content evenly into uniform
    pages
- Fits the single page to content by rendering one tall page and cropping it to
  the content bounding box top and bottom, so pages whose content is offset or
  centered still fit tightly with no blank band

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
python pageless_pdf.py <url> [output.pdf] [--zoom N] [--font-scale N] [--paged] [--paged2] [--dark] [--declutter]
```

Example

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf
```

Paged output for PDF editors and fast scrolling

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf --paged
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf --paged2
```

Soft dark mode

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf --dark
```

Declutter ads and overlays

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf --declutter
```

Browser style zoom

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf --zoom 150
```

Bigger text for small-type sites

```bash
python pageless_pdf.py https://en.wikipedia.org/wiki/PDF out.pdf --font-scale 1.4
```

Flags can be combined.

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
