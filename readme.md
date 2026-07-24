# Images Translator

**English** · [عربي](readme-ar.md)

Turn scanned pages — manuals, brochures, spec sheets — into translated
pages that still *look like the original*. The tool reads the text with
OCR, translates it, and redraws the translation right on top of the scan,
keeping the tables, diagrams, icons and logos exactly where they were.
Then you fine-tune the result in a visual editor.

Think of it as Google Translate's camera mode, but full-resolution,
editable, scriptable, and able to run fully offline. Works on macOS,
Linux and Windows.

![Images Translator editing a scanned manual page](docs/screenshot.png)

*A Japanese manual, automatically translated to English — layout, tables
and artwork untouched.*

## What it can do

- **Read 30+ languages** with the macOS Vision OCR engine, or **Tesseract**
  (works on older macOS, Linux and Windows too — pick it in Settings).
- **Translate offline** with Apple's built-in translator (macOS 15+), or
  through Google Cloud Translation — including right-to-left targets like
  Arabic, with correct shaping and alignment.
- **Edit everything visually** in your browser: move, resize and rewrite
  text boxes, change fonts, colors and backgrounds, pick a patch color
  straight from the image, undo with ⌘Z. What you see is exactly what
  gets saved.
- **Save the way you want**: PNG, optimized PNG-8, grayscale, crisp 1-bit
  black & white, JPEG — or a per-page **PDF with a hidden, searchable
  text layer**.
- **Batch a whole book** from the command line; every page is cached, so
  re-runs take seconds.

## Samples

Pages of a 1990s Japanese car-manual, straight out of the tool.

Example 1:

- [Original scanned page (Japanese)](docs/example_page_org.jpg)
- [Translated to English — PDF with a searchable text layer](docs/example_page_en.pdf)
- [Translated to Arabic — optimized PNG-8](docs/example_page_ar.png)

Example 2:

- [Original scanned page (Japanese)](docs/example2_page_org.jpg)
- [Translated to English — optimized PNG-8](docs/example2_page_en.png)

## Installation

Runs on **macOS, Linux and Windows**. You need Python 3 and, for OCR,
either macOS Vision (macOS 15+) or Tesseract (everywhere else).

```bash
git clone https://github.com/techana/scanned-images-translator.git
cd scanned-images-translator
pip3 install -r requirements.txt
```

Then set up OCR for your platform:

**macOS 15+** — the built-in Vision engine is the default; just build the
two small Swift helpers once (needs Xcode CLT — `xcode-select --install`):

```bash
swiftc -O -o ocr ocr.swift          # Vision OCR
swiftc -O -o translate translate.swift   # Apple offline translation
```

Offline translation then downloads its language pack on first use.

**Linux** — install Tesseract, its language data, and fonts:

```bash
sudo apt install tesseract-ocr tesseract-ocr-jpn tesseract-ocr-ara \
                 fonts-dejavu fonts-noto zenity
# (swap in the tesseract-ocr-<lang> packages for your source languages)
```

**Windows** — install [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki)
(the UB-Mannheim build; tick the language data you need during setup) and
make sure `tesseract.exe` is on your PATH. Fonts and file dialogs are
already present.

On Linux/Windows the app defaults to Tesseract for OCR and Google for
translation (Apple's offline translator is macOS-only). Pick the OCR
engine, languages and translator any time in **Files ▾ → Settings…**

## Using it

**The editor:**

```bash
python3 gui.py
```

Your browser opens the app. **Files ▾ → Load a folder…**, pick your
scans, and pages translate as you view them. Click any text box to
adjust it; drag the *Translation* slider to peek at the original
underneath. **⌘S** saves the page — the first save asks where and in
which format, and remembers your answer.

Languages and the translation engine live in **Files ▾ → Settings…**

**The command line**, for whole folders at once:

```bash
python3 translate_pages.py scans/                          # Japanese → English
python3 translate_pages.py --source ja-JP --target ar scans/   # → Arabic
```

Output lands next to the inputs as `<page>_<language>.png`.

## Good to know

- **A recent Mac gives the best results.** macOS Vision (the default on
  macOS 15+) runs Apple's neural text-recognition models, trained on
  real-world photos and scans, and it clearly outperforms Tesseract on
  noisy halftone pages — especially Japanese. Tesseract is neural too
  (LSTM, since v4) and is a solid portable fallback for Linux, Windows
  and older macOS, but expect rougher OCR on grainy scans, which means
  more fixing up by hand in the editor.
- OCR and translation results are cached per page in `.cache/` — edits
  live there too, and re-rendering a full book takes seconds.
- `./ocr --list-langs` and `./translate --list-langs` show every language
  your machine supports.
- Recurring OCR mistakes in a specific document can be fixed once with a
  small corrections file (see `fixes-carmarty.json` for an example, and
  pass it with `--fixes`).

## License

MIT
