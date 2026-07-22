# Scanned Images Translator

Translate scanned document pages (manuals, brochures, spec sheets) **in
place on the image** — like Google Translate's camera mode, but scriptable,
full-resolution, editable, and able to run fully offline on a Mac.

The pipeline OCRs each page, groups the text into visual blocks, translates
them, then redraws the translation over the original scan — preserving the
page's layout, tables, icons, logos and artwork.

## Features

- **macOS Vision OCR** with character-level boxes; 30+ recognition
  languages (Japanese, Chinese, Korean, Arabic, most European languages…)
- **Three translation engines**, tried in order:
  1. **Apple Translation framework** — offline, free, private (macOS 15+)
  2. **Google Cloud Translation v3** — needs a GCP project with billing
  3. Google's free web endpoint — keyless last resort
- **Any language pair**, including right-to-left targets (Arabic, Hebrew,
  Persian, Urdu) with correct shaping, RTL alignment and RTL-aware fonts
- **Browser-based WYSIWYG editor** (`gui.py`):
  - page navigation with a translation-layer opacity slider
  - move / resize / edit / create / delete text blocks; undo (⌘Z)
  - font size, line spacing, font color, background (white / picked from
    the image with an eyedropper / transparent)
  - overflow badges on blocks whose text exceeds their box
  - saved images match the editor pixel-for-pixel
- **Batch CLI** (`translate_pages.py`) with a per-page cache — re-renders
  are instant; OCR and translation run only once per page
- Icon/graphic protection: bullets, buttons and arrows misread by OCR are
  kept as original pixels, never painted over

## Requirements

- macOS 15+ (Apple Translation; older macOS still works with Google engines)
- Xcode Command Line Tools (to build the two Swift helpers)
- Python 3 with [Pillow](https://python-pillow.org) — built with **libraqm**
  for right-to-left targets

## Build (one time)

```bash
swiftc -O -o ocr ocr.swift            # Vision OCR helper
swiftc -O -o translate translate.swift # Apple Translation helper (macOS 15+)
```

## Usage

### GUI

```bash
python3 gui.py
```

Opens `http://localhost:8877`. Use **Files ▾** to open an image or a folder
of scans, edit blocks as needed, then **Save current page** (⌘S), **Save
current as…**, or **Save all pages**. The first save picks the output —
Image or per-page searchable PDF — and the image format — PNG, PNG Optimized (web palette + pattern dither, ~6× smaller),
PNG-8 Grayscale (8 shades), PNG 1-bit (clean 50% threshold), or JPEG at
60/30/10% quality — and the destination; later saves reuse both. Output: `<page>_<lang>.png|.jpg` at full source resolution. **Files ▾ → Settings…** selects the OCR language, the
target language and the translation engine (and offers the one-time Apple
language-pack download).

### CLI

```bash
# translate a folder of scans (writes <page>_<lang>.png next to inputs)
python3 translate_pages.py scans/

# choose languages / engine
python3 translate_pages.py --source ja-JP --target ar --engine apple scans/

# with the official Google API and document-specific OCR fixes
python3 translate_pages.py --project <gcp-project> --fixes fixes-carmarty.json scans/
```

`./ocr --list-langs` and `./translate --list-langs` print the supported
OCR / translation languages.

## Notes

- Cached OCR + translations live in `<outdir>/.cache/<page>.json`; delete a
  page's file (or pass `--force`) to redo it. Manual edits made in the GUI
  are stored in the same cache.
- Inputs: `.jp2 .png .jpg .jpeg .tif .tiff .bmp`.
- macOS-only (Vision + Apple Translation). Swapping in another OCR backend
  only requires keeping the `ocr` helper's JSON contract.

## License

MIT
