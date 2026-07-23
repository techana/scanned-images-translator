#!/usr/bin/env python3
"""Translate scanned Japanese pages to English, in place on the image.

Works on any scanned Japanese document image (manuals, brochures, flyers):
    input.jp2/png/jpg -> macOS Vision OCR (char-level boxes, ./ocr helper)
                      -> group lines into text blocks (headings, TOC entries,
                         bullets, paragraphs), protect icon/graphic glyphs
                      -> translate blocks ja->en (Cloud Translation API if a
                         GCP project is available, else free gtx endpoint)
                      -> erase original text per segment, draw the
                         translation fitted into the ORIGINAL text area
                      -> <stem>_<target>.png at full resolution

Usage:
  python3 translate_pages.py page.png                     # one file
  python3 translate_pages.py scans/                       # every image in dir
  python3 translate_pages.py -o out/ --project my-gcp-id scans/
  python3 translate_pages.py --fixes myproject-fixes.json scans/

Options:
  -o / --out DIR     output directory (default: next to each input)
  --project ID       GCP project for Cloud Translation API v3 (or set
                     GCP_PROJECT env var); omit to use the free endpoint
  --fixes FILE       JSON list of document-specific OCR corrections:
                     [["bad","good"], ["pat","repl","regex"], ...]
  --force            re-OCR and re-translate even if cached

Cached OCR+translations live in <outdir>/.cache/<stem>.json — delete a
page's file (or use --force) to re-translate it; otherwise re-runs only
re-render, which makes layout iteration nearly instant.
"""

import argparse
import json
import os
import platform
import re
import sys
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TOOLS_DIR = Path(__file__).resolve().parent
OCR_BIN = TOOLS_DIR / "ocr"
TRANSLATE_BIN = TOOLS_DIR / "translate"   # Apple Translation helper (macOS 15+)

# Language / engine settings (see set_langs; gui.py drives these from its
# Settings dialog, the CLI from --source/--target/--engine).
OCR_LANG = "ja-JP"        # source language, BCP-47 (always — even for
                          # Tesseract, which maps it via BCP47_TO_TESS)
SOURCE_LANG = "ja"        # translation source (derived from OCR_LANG)
TARGET_LANG = "en"        # translation target (./translate --list-langs)
ENGINE = "auto"           # translation: auto = Apple, then Google, then gtx
OCR_ENGINE = "vision"     # "vision" (macOS Vision) | "tesseract" | "disabled"


RTL_LANGS = {"ar", "he", "fa", "ur"}


def is_rtl():
    return TARGET_LANG in RTL_LANGS


def default_ocr_engine():
    """Vision on macOS 15+, Tesseract everywhere else (older macOS,
    Linux, Windows)."""
    import platform
    if platform.system() == "Darwin":
        try:
            if int(platform.mac_ver()[0].split(".")[0]) >= 15:
                return "vision"
        except (ValueError, IndexError):
            pass
    return "tesseract"


def _source_lang(ocr_lang, ocr_engine):
    """2-letter translation-source code from the OCR source language, whose
    format depends on the engine: BCP-47 (`ja-JP`) for Vision, a Tesseract
    traineddata code (`jpn`) for Tesseract."""
    if ocr_engine == "tesseract" and "-" not in ocr_lang:
        return TESS_TO_ISO.get(ocr_lang, ocr_lang[:2])
    return ocr_lang.split("-")[0]


def set_langs(ocr_lang=None, target=None, engine=None, ocr_engine=None):
    global OCR_LANG, SOURCE_LANG, TARGET_LANG, ENGINE, OCR_ENGINE
    if ocr_engine:
        OCR_ENGINE = ocr_engine
    if ocr_lang:
        OCR_LANG = ocr_lang
        SOURCE_LANG = _source_lang(ocr_lang, OCR_ENGINE)
    if target:
        TARGET_LANG = target
    if engine:
        ENGINE = engine

IMAGE_EXTS = {".jp2", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# Fonts are resolved cross-platform: the first existing path wins, else a
# scan of the OS font directories, else PIL's default (see load_font).
FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    # Linux (DejaVu / Liberation / Noto ship on most distros)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    # Windows
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
]

# Kana + kanji only: full-width digits/punctuation (ＦＦ００–ＦＦ６５) must NOT
# trigger translation, or page numbers like ２１ become "twenty one".
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def is_japanese(text):
    """True if text contains real kana/kanji. The kana block also holds
    punctuation-like marks (・ leader dots, ー, 〜) — strip them first so
    dot-leader runs like `…・・21` don't get sent for translation."""
    return bool(CJK_RE.search(re.sub(r"[・ーヽヾゝゞ〜]", "", text)))


def needs_translation(text):
    """Should this block's text be sent for translation?  For Japanese the
    battle-tested kana/kanji gate applies; for other source languages any
    real letter qualifies (numbers/punctuation-only blocks stay pixels)."""
    if SOURCE_LANG == "ja":
        return is_japanese(text)
    return bool(re.search(r"[^\W\d_]", text))


# Layout constants are defined at a reference scan width and rescaled per
# page, so 300-dpi and 72-dpi inputs behave identically.
REF_WIDTH = 4264
MIN_FONT = 26          # px at REF_WIDTH
PAD = 8                # px horizontal padding around erased segments
PAD_V = 4              # px vertical padding (small: don't clip table rules)
LINE_SPACING = 1.18


def set_scale(width):
    """Rescale layout constants for the current page width."""
    global MIN_FONT, PAD, PAD_V
    k = width / REF_WIDTH
    MIN_FONT = max(10, int(26 * k))
    PAD = max(2, int(8 * k))
    PAD_V = max(1, int(4 * k))


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- OCR

def run_ocr(png_path):
    """Returns (lines, keepouts): OCR lines split into text segments around
    protected icon glyphs, plus the icon boxes to keep free of paint."""
    out = subprocess.run([str(OCR_BIN), str(png_path), OCR_LANG],
                         capture_output=True, text=True, check=True)
    lines, keepouts = [], []
    for raw in json.loads(out.stdout):
        ln = process_line(raw, keepouts)
        if ln:
            lines.append(ln)
    return lines, keepouts


# ------------------------------------------- Tesseract OCR (cross-platform)
#
# Deliberately separate from the Vision path above: this shells out to the
# `tesseract` binary via pytesseract and emits line dicts WITHOUT per-char
# boxes, so process_line wraps each as a single segment and every
# downstream stage (grouping, translation, rendering) is reused unchanged.
# Vision's icon-protection heuristics never run here (they are tuned to
# Vision's Japanese misreads); nothing in run_ocr is touched.

# BCP-47 primary subtag -> Tesseract traineddata code (zh keyed by script).
BCP47_TO_TESS = {
    "en": "eng", "ja": "jpn", "ar": "ara", "fr": "fra", "de": "deu",
    "es": "spa", "it": "ita", "pt": "por", "ko": "kor", "ru": "rus",
    "uk": "ukr", "th": "tha", "vi": "vie", "tr": "tur", "id": "ind",
    "cs": "ces", "da": "dan", "nl": "nld", "no": "nor", "nb": "nor",
    "nn": "nor", "ms": "msa", "pl": "pol", "ro": "ron", "sv": "swe",
    "hi": "hin", "he": "heb", "fa": "fas", "ur": "urd", "el": "ell",
    "bg": "bul", "hu": "hun", "fi": "fin", "sk": "slk", "hr": "hrv",
    "sr": "srp", "sl": "slv", "lt": "lit", "lv": "lav", "et": "est",
    "zh-Hans": "chi_sim", "zh-Hant": "chi_tra",
    "yue-Hans": "chi_sim", "yue-Hant": "chi_tra",
}


# Tesseract traineddata code -> ISO 639-1 (for the translation source).
TESS_TO_ISO = {
    "eng": "en", "jpn": "ja", "ara": "ar", "fra": "fr", "deu": "de",
    "spa": "es", "ita": "it", "por": "pt", "kor": "ko", "rus": "ru",
    "ukr": "uk", "tha": "th", "vie": "vi", "tur": "tr", "ind": "id",
    "ces": "cs", "dan": "da", "nld": "nl", "nor": "no", "msa": "ms",
    "pol": "pl", "ron": "ro", "swe": "sv", "hin": "hi", "heb": "he",
    "fas": "fa", "urd": "ur", "ell": "el", "bul": "bg", "hun": "hu",
    "fin": "fi", "slk": "sk", "hrv": "hr", "srp": "sr", "slv": "sl",
    "lit": "lt", "lav": "lv", "est": "et", "chi_sim": "zh",
    "chi_tra": "zh",
}


def tess_lang(bcp47):
    """BCP-47 language tag -> Tesseract traineddata code."""
    if bcp47 in BCP47_TO_TESS:
        return BCP47_TO_TESS[bcp47]
    base = bcp47.split("-")[0]
    return BCP47_TO_TESS.get(base, base[:3])


def to_tess_code(lang):
    """Accept either a Tesseract code (jpn) or a BCP-47 tag (ja-JP) and
    return the Tesseract code — the source dropdown now offers installed
    Tesseract codes directly, but stale BCP-47 values are handled too."""
    return tess_lang(lang) if "-" in lang else lang


def tesseract_installed_langs():
    """Traineddata codes available to the local `tesseract` binary."""
    try:
        out = subprocess.run(["tesseract", "--list-langs"],
                             capture_output=True, text=True, timeout=30)
        return [l.strip() for l in out.stdout.splitlines()[1:] if l.strip()]
    except Exception:
        return []


def run_ocr_tesseract(png_path):
    """Returns (lines, keepouts) like run_ocr, using Tesseract.  Words are
    grouped into their source text lines; no per-char boxes are emitted."""
    import pytesseract

    lang = to_tess_code(OCR_LANG)
    # always pair with English: manuals mix in Latin words/part numbers,
    # and eng improves layout even for the primary script
    if "eng" not in lang.split("+"):
        lang = lang + "+eng"
    data = pytesseract.image_to_data(
        Image.open(png_path), lang=lang,
        output_type=pytesseract.Output.DICT)

    groups = {}
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if not txt or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append(i)

    lines, keepouts = [], []
    for idxs in groups.values():
        x0 = min(data["left"][i] for i in idxs)
        y0 = min(data["top"][i] for i in idxs)
        x1 = max(data["left"][i] + data["width"][i] for i in idxs)
        y1 = max(data["top"][i] + data["height"][i] for i in idxs)
        text = " ".join(data["text"][i].strip() for i in idxs
                        if data["text"][i].strip())
        raw = {"text": text, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
        ln = process_line(raw, keepouts)   # no chars -> single-seg line
        if ln:
            lines.append(ln)
    return lines, keepouts


def is_icon_char(c):
    """Glyphs that are really page graphics (buttons, arrows, markers)."""
    cp = ord(c)
    return (0x2190 <= cp <= 0x21FF      # arrows
            or 0x2460 <= cp <= 0x24FF   # circled digits/letters
            or 0x25A0 <= cp <= 0x25FF   # geometric shapes ■□◆▶ etc.
            or 0x2600 <= cp <= 0x27BF   # misc symbols, ♪, dingbats
            or 0x2B00 <= cp <= 0x2BFF   # more arrows/shapes
            or cp == 0x3013)            # 〓 geta mark (unreadable glyph)


def process_line(ln, keepouts):
    """Split an OCR line into text segments around protected icon glyphs.

    Leading icons (bullets, button glyphs) and interior icons (⏮⏭, arrows
    in running text) are excluded from the text and the erase mask — their
    boxes go to `keepouts` so nothing is ever painted over them.  Lines
    that are nothing but icons/checkboxes are dropped (pure graphics).
    Returns the adjusted line (with `segs`) or None to drop it."""
    chars = ln.pop("chars", None)
    if not chars:
        ln["segs"] = [{"x": ln["x"], "y": ln["y"], "w": ln["w"], "h": ln["h"]}]
        return ln
    h = ln["h"]

    def sane(ch):
        # Vision emits degenerate boxes (x=0 / full-width) for some chars.
        return not ch["c"].isspace() and 0 < ch["w"] < 4 * h

    def protect(ch):
        if sane(ch):
            keepouts.append({k: ch[k] for k in ("x", "y", "w", "h")})

    i, bullet = 0, False
    while i < len(chars):
        c = chars[i]["c"]
        nxt = chars[i + 1]["c"] if i + 1 < len(chars) else " "
        if c.isspace():
            i += 1
        elif (is_icon_char(c)
              # +/-/checkbox glyphs misread as text chars
              or (c in "+-±ロ〇○◯" and nxt.isspace())
              # a lone kanji followed by a space never happens in real
              # Japanese text — it's an icon glyph misread (e.g. ➡ as 日)
              or (0x3400 <= ord(c) <= 0x9FFF and nxt.isspace()
                  and i + 2 < len(chars))):
            protect(chars[i])
            bullet, i = True, i + 1
        else:
            break
    if i >= len(chars):
        return None                      # icons only — leave pixels alone

    # split the remainder at interior icon glyphs -> segments
    groups, cur = [], []
    for ch in chars[i:]:
        if is_icon_char(ch["c"]) and sane(ch):
            protect(ch)
            if cur:
                groups.append(cur)
                cur = []
        else:
            cur.append(ch)
    if cur:
        groups.append(cur)

    segs, parts = [], []
    for g in groups:
        boxes = [ch for ch in g if sane(ch)]
        if not boxes:
            continue
        x0 = min(ch["x"] for ch in boxes)
        x1 = max(ch["x"] + ch["w"] for ch in boxes)
        segs.append({"x": x0, "y": ln["y"], "w": x1 - x0, "h": h})
        parts.append("".join(ch["c"] for ch in g).strip())
    text = "".join(parts).strip()
    if not segs or not text or re.fullmatch(r"[ロ〇○◯□ー・ヽ\s]+", text):
        return None                      # checkbox rows etc. — graphics
    ln["segs"] = segs
    ln["x"] = segs[0]["x"]
    ln["w"] = segs[-1]["x"] + segs[-1]["w"] - ln["x"]
    ln["text"] = text
    if bullet:
        ln["bullet"] = True
    return ln


# ------------------------------------------------------- block grouping

def group_blocks(lines):
    """Group OCR lines into visual text blocks.

    Two lines join the same block when they are vertically adjacent
    (gap < 0.75 x line height) and share the same left edge (within
    0.6 x line height).  Headings outdented from their body text
    naturally become their own blocks."""
    lines = sorted(lines, key=lambda r: (r["y"], r["x"]))
    mark_toc_entries(lines)
    blocks = []
    for ln in lines:
        placed = False
        if ln.get("toc") or ln.get("bullet"):
            # TOC entries stand alone; a bullet row starts its own block
            # (following plain lines may still join it below).
            blocks.append([ln])
            continue
        for blk in blocks:
            last = blk[-1]
            gap = ln["y"] - (last["y"] + last["h"])
            ref_h = max(last["h"], ln["h"])
            same_left = abs(ln["x"] - last["x"]) < 0.6 * ref_h
            # Never merge lines of very different sizes: keeps stylized
            # logos / display titles separate from nearby body text.
            similar_size = ref_h < 1.8 * min(last["h"], ln["h"])
            # A short trailing fragment ("ます。") indented under its
            # sentence joins even without left-edge alignment.
            contained = (ln["w"] < 0.7 * last["w"]
                         and last["x"] - 0.6 * ref_h <= ln["x"]
                         <= last["x"] + 0.5 * last["w"]
                         and gap < 0.5 * ref_h)
            if (-0.3 * ref_h < gap < 0.75 * ref_h and similar_size
                    and (same_left or contained)):
                blk.append(ln)
                placed = True
                break
        if not placed:
            blocks.append([ln])
    blocks = [sub for blk in blocks for sub in split_heading(blk)]
    return [sub for blk in blocks for sub in split_ragged_list(blk)]


def split_ragged_list(blk):
    """Break apart falsely merged list/TOC blocks.

    Real paragraphs have near-uniform line widths and usually punctuation;
    a 3+ line block with punctuation-free lines of wildly varying width is
    a list of separate entries — translate each line on its own."""
    if len(blk) < 3:
        return [blk]
    # Only 。 marks real prose; list entries still use 、 commas freely.
    if any("。" in l["text"] for l in blk):
        return [blk]
    widths = [l["w"] for l in blk[:-1]]          # ignore the final line
    if max(widths) > 1.5 * min(widths):
        return [[l] for l in blk]
    return [blk]


LEADER_RE = re.compile(r"^[・.。…•\-—─\s\d]+$")


def mark_toc_entries(lines):
    """Flag lines that have a dot-leader run to their right at the same
    height (table-of-contents entries) so they never merge into paragraphs."""
    leaders = [l for l in lines
               if LEADER_RE.match(l["text"]) and len(l["text"]) >= 4]
    for ln in lines:
        if ln in leaders:
            continue
        for ld in leaders:
            overlap = (min(ln["y"] + ln["h"], ld["y"] + ld["h"])
                       - max(ln["y"], ld["y"]))
            if overlap > 0.5 * ln["h"] and ld["x"] > ln["x"] + 0.5 * ln["w"]:
                ln["toc"] = True
                break


def split_heading(blk):
    """Detach a heading-like first line (short, no sentence-ending mark)
    from its body so the two translate independently."""
    if len(blk) < 2:
        return [blk]
    first = blk[0]
    width = max(l["x"] + l["w"] for l in blk) - min(l["x"] for l in blk)
    if first["w"] < 0.7 * width and "。" not in first["text"]:
        return [[first], blk[1:]]
    return [blk]


# Frequent Vision-OCR confusions in halftone print — generic, any document.
# Document-specific corrections go in a --fixes JSON file instead.
OCR_FIXES = [
    ("ブレーヤ", "プレーヤ"),   # pu/bu dakuten misread in プレーヤ (player)
    ("パツド", "パッド"),       # small-tsu misread in パッド (pad)
    ("接綂", "接続"),           # rare kanji misread of 接続 (connection)
    ("晝", "書"),               # 書 misread as archaic 晝 (説明書, 保証書...)
]
EXTRA_FIXES = []               # loaded from --fixes: (pattern, repl, is_regex)


def load_fixes(path):
    for entry in json.loads(Path(path).read_text()):
        pat, repl = entry[0], entry[1]
        is_regex = len(entry) > 2 and entry[2] == "regex"
        EXTRA_FIXES.append((pat, repl, is_regex))


def block_text(blk):
    """Join a block's lines: CJK-style scripts join without spaces (unless
    ASCII meets ASCII); everything else joins with spaces."""
    spaceless = SOURCE_LANG in ("ja", "zh", "yue", "th")
    text = ""
    for ln in blk:
        t = ln["text"].strip()
        if text and t and (not spaceless
                           or (text[-1].isascii() and t[0].isascii())):
            text += " "
        text += t
    for bad, good in OCR_FIXES:
        text = text.replace(bad, good)
    for pat, repl, is_regex in EXTRA_FIXES:
        text = re.sub(pat, repl, text) if is_regex else text.replace(pat, repl)
    return text


def block_bbox(blk):
    x0 = min(l["x"] for l in blk)
    y0 = min(l["y"] for l in blk)
    x1 = max(l["x"] + l["w"] for l in blk)
    y1 = max(l["y"] + l["h"] for l in blk)
    return x0, y0, x1, y1


# ---------------------------------------------------------- translation
#
# Primary backend: official Cloud Translation API v3 (GCP project with
# billing; free tier covers ~500k chars/month).  Fallback: the keyless
# translate.googleapis.com gtx endpoint (unofficial, fine at low volume).

GCP_PROJECT = os.environ.get("GCP_PROJECT", "")
_api_token = None


def api_token():
    global _api_token
    if _api_token is None:
        if not GCP_PROJECT:
            _api_token = ""
        else:
            try:
                _api_token = subprocess.run(
                    ["gcloud", "auth", "print-access-token"],
                    capture_output=True, text=True, check=True).stdout.strip()
            except Exception:
                _api_token = ""      # remember the failure, use fallback
    return _api_token


def translate_apple(texts):
    """Offline translation via the Apple Translation framework helper
    (./translate, macOS 15+).  Raises if the helper is missing, the
    language pair is unsupported, or its model isn't downloaded yet."""
    if not TRANSLATE_BIN.exists():
        raise RuntimeError("helper not built: swiftc -O -o translate translate.swift")
    body = json.dumps({"source": SOURCE_LANG, "target": TARGET_LANG,
                       "texts": texts})
    out = subprocess.run([str(TRANSLATE_BIN)], input=body,
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"exit {out.returncode}")
    return json.loads(out.stdout)


def translate_google(texts):
    """One batched Cloud Translation v3 call (needs GCP_PROJECT + gcloud)."""
    token = api_token()
    if not token:
        raise RuntimeError("no GCP project / gcloud token")
    body = json.dumps({
        "contents": texts,
        "mimeType": "text/plain",
        "sourceLanguageCode": SOURCE_LANG,
        "targetLanguageCode": TARGET_LANG,
    }).encode()
    req = urllib.request.Request(
        f"https://translation.googleapis.com/v3/projects/{GCP_PROJECT}"
        "/locations/global:translateText",
        data=body,
        headers={"Authorization": f"Bearer {token}",
                 "x-goog-user-project": GCP_PROJECT,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return [t["translatedText"] for t in data["translations"]]


def translate_batch(texts):
    """Translate a list of strings SOURCE_LANG -> TARGET_LANG.
    Engine order: --engine apple/google forces one backend; auto tries the
    offline Apple helper first, then the Google API.  The keyless gtx web
    endpoint is always the last resort."""
    engines = {"apple": [translate_apple],
               "google": [translate_google],
               "auto": [translate_apple, translate_google]}[ENGINE]
    for eng in engines:
        try:
            return eng(texts)
        except Exception as e:
            log(f"    {eng.__name__} failed ({e})")
    log("    falling back to free web endpoint")
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(translate_text, texts))


def translate_text(text, retries=4):
    """Single string via the free Google Translate web endpoint (client=gtx)."""
    q = urllib.parse.quote(text)
    url = ("https://translate.googleapis.com/translate_a/single"
           f"?client=gtx&sl={SOURCE_LANG}&tl={TARGET_LANG}&dt=t&q={q}")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            return "".join(seg[0] for seg in data[0] if seg[0])
        except Exception as e:
            wait = 2 ** attempt
            log(f"    translate retry {attempt + 1} in {wait}s ({e})")
            time.sleep(wait)
    raise RuntimeError(f"translation failed for: {text[:60]}")


# ------------------------------------------------------------ rendering

# Fonts that cover Arabic AND Latin (a single font must render mixed
# strings like "لوحة MARTY" without tofu — GeezaPro drops Latin under PIL).
ARABIC_FONTS = [
    "/System/Library/Fonts/Supplemental/Tahoma.ttf",           # macOS
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/GeezaPro.ttc",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",  # Linux
    "/usr/share/fonts/truetype/kacst/KacstOne.ttf",
    "/usr/share/fonts/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/TTF/NotoNaskhArabic-Regular.ttf",
    "C:/Windows/Fonts/tahoma.ttf",                             # Windows
    "C:/Windows/Fonts/arial.ttf",
]


def _scan_font_dirs():
    """First usable .ttf/.ttc/.otf found under the OS font directories —
    a last resort when none of the known paths exist."""
    import glob
    system = platform.system()
    if system == "Windows":
        dirs = ["C:/Windows/Fonts", os.path.expanduser("~/AppData/Local/Microsoft/Windows/Fonts")]
    elif system == "Darwin":
        dirs = ["/System/Library/Fonts", "/Library/Fonts",
                os.path.expanduser("~/Library/Fonts")]
    else:
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts",
                os.path.expanduser("~/.fonts"),
                os.path.expanduser("~/.local/share/fonts")]
    prefer = ("dejavusans.ttf", "notosans-regular.ttf",
              "liberationsans-regular.ttf", "arial.ttf")
    found = []
    for d in dirs:
        if os.path.isdir(d):
            for ext in ("ttf", "ttc", "otf"):
                found += glob.glob(os.path.join(d, "**", "*." + ext),
                                   recursive=True)
    for name in prefer:                 # a plain sans-serif if we can find one
        for f in found:
            if os.path.basename(f).lower() == name:
                return f
    return found[0] if found else None


_font_fallback = None                   # cached result of the dir scan


def load_font(size):
    global _font_fallback
    # Helvetica has no Arabic glyphs; PIL+raqm shapes RTL fine with these
    candidates = (ARABIC_FONTS + FONT_CANDIDATES if is_rtl()
                  else FONT_CANDIDATES)
    for path in candidates:
        try:
            return ImageFont.truetype(path, int(size))
        except OSError:
            continue
    if _font_fallback is None:
        _font_fallback = _scan_font_dirs() or ""
    if _font_fallback:
        try:
            return ImageFont.truetype(_font_fallback, int(size))
        except OSError:
            pass
    raise RuntimeError(
        "no usable font found — install a TrueType font "
        "(Linux: 'sudo apt install fonts-dejavu fonts-noto', "
        "or fonts-noto-core for Arabic)")


# Glyphs our render fonts lack -> closest equivalents.  The browser
# transparently falls back to another font for these; PIL cannot, so the
# saved image would show tofu boxes.
RENDER_SUBS = {"\u30fb": "\u2022", "\u25cf": "\u2022", "\u301c": "~"}


def sub_glyphs(text):
    for bad, good in RENDER_SUBS.items():
        text = text.replace(bad, good)
    return text


def wrap_text(draw, text, font, width):
    """Greedy word-wrap to a pixel width (mirrors the GUI's CSS pre-wrap):
    explicit newlines are honored, and leading spaces on each line are
    preserved (GUI text arrives with non-breaking spaces — normalize)."""
    text = sub_glyphs(text.replace("\u00a0", " "))   # NBSP from GUI
    lines = []
    for para in text.split("\n"):
        lead = para[:len(para) - len(para.lstrip(" "))]
        words = para.split()
        if not words:
            lines.append("")             # blank line keeps its height
            continue
        cur = lead + words[0]
        for w in words[1:]:
            trial = cur + " " + w
            if draw.textlength(trial, font=font) <= width:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    # trailing newlines (easy to type unnoticed) must not grow the backing
    while lines and not lines[-1]:
        lines.pop()
    return lines


def gui_edited(blk):
    """True when the block's geometry was changed in the GUI (resize, font
    size or line spacing) — such blocks render GUI-style, without the
    automatic per-segment erase."""
    return "gui_bbox" in blk or "font_px" in blk or "line_spacing" in blk


def default_font_px(blk, med_h):
    """Initial font size for a block: the LARGEST size — never bigger than
    the original line height allows — at which the wrapped translation
    still fits within the original text area's height.  This keeps the
    text covering the source area only, instead of overflowing it (the
    old `med_h * 0.88` ignored how much text there was, so long
    translations rendered far too large)."""
    x0, y0, x1, y1 = blk.get("gui_bbox") or blk["bbox"]
    w, h = x1 - x0, y1 - y0
    cap = max(MIN_FONT, int(med_h * 0.88))
    text = (blk.get("en") or "").strip()
    if not text or w <= 0 or h <= 0:
        return cap
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    lo, hi, best = MIN_FONT, cap, MIN_FONT
    while lo <= hi:
        mid = (lo + hi) // 2
        lines = wrap_text(draw, text, load_font(mid), w)
        if len(lines) * mid * LINE_SPACING <= h * 1.05:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def render_gui_block(draw, blk, med_h):
    """Render a block the way the GUI shows it: text wrapped into the
    (possibly user-resized) rectangle at the chosen font size, spacing and
    colors, on a backing that follows the text extent."""
    x0, y0, x1, y1 = blk.get("gui_bbox") or blk["bbox"]
    size = int(blk.get("font_px") or default_font_px(blk, med_h))
    ls = float(blk.get("line_spacing") or 1.0)
    font = load_font(size)
    lines = wrap_text(draw, blk["en"], font, x1 - x0)
    lh = size * ls
    bg = blk.get("bg_color") or "white"
    # WYSIWYG: the backing must be exactly the GUI's text box (no padding),
    # or it covers neighboring artwork like table rules
    if bg != "transparent":
        draw.rectangle([x0, y0, x1, y0 + lh * len(lines)], fill=bg)
    # CSS half-leading: the browser centers each line's glyphs (ascent +
    # descent tall) inside the lh-tall line box; PIL's default anchor is
    # the ascender top, so without this offset baked text sits visibly
    # lower than in the editor.
    ascent, descent = font.getmetrics()
    y = y0 + (lh - (ascent + descent)) / 2
    rtl = is_rtl()
    # alignment is user-settable per block; the BiDi base direction stays
    # tied to the target language (RTL shaping survives left-alignment)
    align = blk.get("align") or ("right" if rtl else "left")
    direction = "rtl" if rtl else None
    fill = blk.get("font_color") or (20, 20, 20)
    for line in lines:
        x = x0
        if align == "right":
            x = x1 - draw.textlength(line, font=font, direction=direction)
        draw.text((x, y), line, font=font, direction=direction, fill=fill)
        y += lh


def render_page(img, blocks, keepouts):
    """WYSIWYG render: every block is drawn exactly the way the GUI shows
    it — same rectangle, font, spacing, colors and greedy wrap — via
    render_gui_block.  (The former slot layout produced different wraps
    than the editor, which broke page layout the moment a user relied on
    what they saw.)  Erase patches remain only for blocks whose geometry
    was never edited in the GUI; "transparent" backgrounds skip both the
    erase and the text backing.  keepouts is retained for signature
    compatibility."""
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    for blk in blocks:
        # GUI geometry-edited blocks get NO automatic erase: their only
        # backing is the text-following rectangle in render_gui_block, so
        # shrinking a block/font in the GUI un-hides misjudged pixels
        # (icons etc.).  Color-only edits keep the patches.  Deleted
        # blocks leave the original pixels entirely alone.
        if "en" not in blk or blk.get("deleted") or gui_edited(blk):
            continue
        bg = blk.get("bg_color") or "white"
        if bg == "transparent":
            continue                     # no erase: text over raw pixels
        for ln in blk["lines"]:
            for s in ln.get("segs", [ln]):
                draw.rectangle([s["x"] - PAD, s["y"] - PAD_V,
                                s["x"] + s["w"] + PAD, s["y"] + s["h"] + PAD_V],
                               fill=bg)
    for blk in blocks:
        if "en" not in blk or blk.get("deleted"):
            continue
        slots = [s for ln in blk["lines"] for s in ln.get("segs", [ln])]
        med_h = (sorted(s["h"] for s in slots)[len(slots) // 2]
                 if slots else MIN_FONT)  # GUI-created blocks have no lines
        render_gui_block(draw, blk, med_h)
    return img


# ------------------------------------------------------------- pipeline

def prepare_page(src, out_dir, force=False):
    """Convert to PNG + OCR + translate, all cached; no rendering.
    Returns (page_png, blocks, keepouts).  Shared by the CLI pipeline
    and gui.py, which renders the blocks as an HTML overlay instead."""
    stem = src.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / ".cache"
    cache_dir.mkdir(exist_ok=True)
    cache = cache_dir / f"{stem}.json"

    # OCR needs a PNG; convert other formats once, kept beside the output.
    if src.suffix.lower() == ".png":
        page_png = src
    else:
        page_png = out_dir / f"{stem}_jp.png"
        if not page_png.exists():
            log(f"  converting {src.name}")
            Image.open(src).save(page_png)

    set_scale(Image.open(page_png).size[0])

    want = [OCR_LANG, TARGET_LANG, OCR_ENGINE]
    if cache.exists() and not force:
        data = json.loads(cache.read_text())
        # Normalize legacy stamps: 2-element ones predate the engine field
        # (they were Vision); "no-ocr" is the old "disabled".
        stamp = list(data.get("langs", ["ja-JP", "en"]))
        if len(stamp) == 2:
            stamp.append("vision")
        elif stamp[2] == "no-ocr":
            stamp = [stamp[0], stamp[1], "disabled"]
        # With OCR disabled any existing cache is used as-is (edits are
        # never discarded by flipping the engine to Disable).  Otherwise a
        # language OR engine change redoes the page (its content belongs
        # to the old settings).
        if OCR_ENGINE == "disabled" or stamp == want:
            return page_png, data["blocks"], data["keepouts"]
        log(f"  settings changed — redoing page")

    if OCR_ENGINE == "disabled":
        log("  OCR disabled — blank page (manual text boxes only)")
        blocks, keepouts = [], []
    else:
        log(f"  OCR ({OCR_ENGINE})...")
        if OCR_ENGINE == "tesseract":
            ocr_lines, keepouts = run_ocr_tesseract(page_png)
        else:
            ocr_lines, keepouts = run_ocr(page_png)
        blocks = []
        for blk in group_blocks(ocr_lines):
            blocks.append({"lines": blk, "bbox": block_bbox(blk),
                           "text": block_text(blk)})
        todo = [b for b in blocks if needs_translation(b["text"])]
        log(f"  {len(blocks)} blocks, translating {len(todo)}...")
        if todo:
            for b, en in zip(todo, translate_batch([b["text"] for b in todo])):
                b["en"] = en
    cache.write_text(json.dumps({"blocks": blocks, "keepouts": keepouts,
                                 "langs": want},
                                ensure_ascii=False, indent=1))
    return page_png, blocks, keepouts


def _bayer_offsets(h, w):
    """(h, w) ordered-dither threshold offsets in (-0.5, 0.5)."""
    import numpy as np
    bayer = np.array([[ 0, 32,  8, 40,  2, 34, 10, 42],
                      [48, 16, 56, 24, 50, 18, 58, 26],
                      [12, 44,  4, 36, 14, 46,  6, 38],
                      [60, 28, 52, 20, 62, 30, 54, 22],
                      [ 3, 35, 11, 43,  1, 33,  9, 41],
                      [51, 19, 59, 27, 49, 17, 57, 25],
                      [15, 47,  7, 39, 13, 45,  5, 37],
                      [63, 31, 55, 23, 61, 29, 53, 21]], dtype=np.float32)
    return np.tile((bayer + 0.5) / 64.0 - 0.5,
                   (h // 8 + 1, w // 8 + 1))[:h, :w]


def save_png_optimized(img, path):
    """PNG-8 with the web-safe ("restrictive") palette and ordered Bayer
    pattern dithering — Photoshop's Save-for-Web PNG-8/Restrictive/Pattern
    combo. Roughly 6x smaller than truecolor PNG on scanned pages."""
    import numpy as np
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = a.shape[:2]
    a += _bayer_offsets(h, w)[..., None] * 51.0   # web-safe step = 51
    q = np.clip(np.round(a / 51.0) * 51.0, 0, 255).astype(np.uint8)
    out = Image.fromarray(q, "RGB").convert(
        "P", palette=Image.Palette.WEB, dither=Image.Dither.NONE)
    out.save(path, "PNG", optimize=True)


def save_png_gray(img, path, levels=8):
    """Palettized grayscale PNG, Bayer-dithered onto a FIXED evenly-spaced
    ramp of `levels` shades from pure black (0) to pure white (255) —
    the palette always contains exactly these shades, whatever the page
    happens to contain."""
    import numpy as np
    a = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = a.shape
    step = 255.0 / (levels - 1)
    a += _bayer_offsets(h, w) * step
    idx = np.clip(np.round(a / step), 0, levels - 1).astype(np.uint8)
    out = Image.fromarray(idx, "P")
    ramp = [round(i * 255.0 / (levels - 1)) for i in range(levels)]
    out.putpalette([v for g in ramp for v in (g, g, g)])
    out.save(path, "PNG", optimize=True)


PDF_DPI = 300.0          # pixel -> PDF point mapping for scanned pages


def pdf_font():
    """A Latin+Arabic TrueType path for the PDF hidden-text layer,
    resolved cross-platform (reuses the render-font search)."""
    for p in ARABIC_FONTS + FONT_CANDIDATES:
        if os.path.exists(p) and p.lower().endswith(".ttf"):
            return p                    # reportlab TTFont needs a .ttf
    scanned = _scan_font_dirs()
    if scanned and scanned.lower().endswith(".ttf"):
        return scanned
    raise RuntimeError("no .ttf font found for the PDF text layer")


def save_output(img, path, fmt=None, blocks=None):
    """Write the rendered page.  fmt: {"kind": ..., "quality": int,
    "pdf": bool}.  Kinds: png (truecolor), png8 (web palette + pattern
    dither), png8g (8 shades of gray, pattern dither), png1 (1-bit black
    & white via 50% threshold — clean, like Photoshop's Bitmap mode),
    jpeg (quality 10-100).  With "pdf" the image (in that same format)
    is wrapped in a one-page PDF with an invisible, searchable text
    layer built from `blocks`."""
    if (fmt or {}).get("pdf"):
        save_pdf(img, path, fmt, blocks or [])
        return
    kind = (fmt or {}).get("kind", "png")
    if kind == "jpeg":
        img.save(path, "JPEG", quality=int(fmt.get("quality", 60)),
                 optimize=True)
    elif kind == "png8":
        save_png_optimized(img, path)
    elif kind == "png8g":
        save_png_gray(img, path, levels=8)
    elif kind == "png1":
        bw = img.convert("L").point(lambda v: 255 if v >= 128 else 0)
        bw.convert("1", dither=Image.Dither.NONE).save(path, "PNG",
                                                       optimize=True)
    else:
        img.save(path, "PNG")


RTL_CHAR_RE = re.compile(r"[֐-ࣿ]")


def _visual_rtl(line):
    """Logical -> visual order for an RTL line (LTR runs keep their
    internal order).  PDF text extractors bidi-flip visually ordered
    text back to logical, so writing visual order makes the hidden
    layer searchable with normal (logical) Arabic strings."""
    rev = line[::-1]
    return re.sub(r"[A-Za-z0-9]+(?: +[A-Za-z0-9]+)*",
                  lambda m: m.group()[::-1], rev)


def save_pdf(img, path, fmt, blocks):
    """One-page PDF: the rendered image (encoded per fmt["kind"]) plus an
    invisible text layer (PDF render mode 3) so the page is searchable.
    The hidden lines reuse the renderer's own wrap/position math, so
    search highlights land on the visible text.  The layer is written
    with reportlab (raw logical codepoints, no shaping — MuPDF-based
    insertion would store Arabic as unsearchable presentation forms)
    and merged onto the image page with PyMuPDF."""
    import io
    import fitz                      # PyMuPDF
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    W, H = img.size
    s = 72.0 / PDF_DPI
    pw, ph = W * s, H * s

    # ---------- invisible, searchable text layer
    try:
        pdfmetrics.getFont("overlay")
    except KeyError:
        pdfmetrics.registerFont(TTFont("overlay", pdf_font()))
    tbuf = io.BytesIO()
    c = rl_canvas.Canvas(tbuf, pagesize=(pw, ph))
    t = c.beginText()
    t.setTextRenderMode(3)           # invisible
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    rtl = is_rtl()
    for blk in blocks:
        if "en" not in blk or blk.get("deleted"):
            continue
        x0, y0, x1, y1 = blk.get("gui_bbox") or blk["bbox"]
        slots = [sg for ln in blk.get("lines", [])
                 for sg in ln.get("segs", [ln])]
        med_h = (sorted(sg["h"] for sg in slots)[len(slots) // 2]
                 if slots else MIN_FONT)
        size = int(blk.get("font_px") or default_font_px(blk, med_h))
        ls = float(blk.get("line_spacing") or 1.0)
        font = load_font(size)
        lines = wrap_text(draw, blk["en"], font, x1 - x0)
        align = blk.get("align") or ("right" if rtl else "left")
        direction = "rtl" if rtl else None
        ascent, descent = font.getmetrics()
        lh = size * ls
        t.setFont("overlay", size * s)
        y = y0 + (lh - (ascent + descent)) / 2 + ascent   # baseline, px
        for line in lines:
            if line.strip():
                x = x0
                if align == "right":
                    x = x1 - draw.textlength(line, font=font,
                                             direction=direction)
                out = (_visual_rtl(line) if RTL_CHAR_RE.search(line)
                       else line)
                t.setTextOrigin(x * s, ph - y * s)   # PDF y-axis is up
                t.textOut(out)
            y += lh
    c.drawText(t)
    c.showPage()
    c.save()

    # ---------- image page + merge
    ibuf = io.BytesIO()
    save_output(img, ibuf, {k: v for k, v in (fmt or {}).items()
                            if k != "pdf"})
    doc = fitz.open()
    page = doc.new_page(width=pw, height=ph)
    page.insert_image(fitz.Rect(0, 0, pw, ph), stream=ibuf.getvalue())
    overlay = fitz.open("pdf", tbuf.getvalue())
    page.show_pdf_page(page.rect, overlay, 0)
    doc.save(path, deflate=True, garbage=3)
    doc.close()


def process_page(src, out_dir, force=False, out_png=None, fmt=None):
    page_png, blocks, keepouts = prepare_page(src, out_dir, force)
    img = Image.open(page_png)
    set_scale(img.size[0])
    out_png = out_png or out_dir / f"{src.stem}_{TARGET_LANG}.png"
    log("  rendering...")
    save_output(render_page(img, blocks, keepouts), out_png, fmt,
                blocks=blocks)
    log(f"  wrote {out_png.name}")
    return out_png


# generated outputs: <stem>_jp.png conversions and <stem>_<lang>.png
# translations (_en, _ar, ...) must never be picked up as inputs
GENERATED_RE = re.compile(r"_[a-z]{2,3}$", re.I)


def collect_inputs(paths):
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.iterdir()
                            if f.suffix.lower() in IMAGE_EXTS
                            and not GENERATED_RE.search(f.stem))
        elif p.is_file():
            files.append(p)
        else:
            sys.exit(f"not found: {p}")
    return files


def main():
    global GCP_PROJECT
    ap = argparse.ArgumentParser(
        description="Translate scanned Japanese pages to English images.")
    ap.add_argument("inputs", nargs="+", help="image files or directories")
    ap.add_argument("-o", "--out", help="output directory")
    ap.add_argument("--project", help="GCP project for Cloud Translation API")
    ap.add_argument("--fixes", help="JSON file with document-specific OCR fixes")
    ap.add_argument("--force", action="store_true",
                    help="ignore cache, re-OCR and re-translate")
    ap.add_argument("--source", help="OCR language (default ja-JP; "
                                     "see ./ocr --list-langs)")
    ap.add_argument("--target", help="target language code (default en; "
                                     "see ./translate --list-langs)")
    ap.add_argument("--engine", choices=["auto", "apple", "google"],
                    help="translation backend (default auto: Apple offline "
                         "first, then Google)")
    ap.add_argument("--ocr", choices=["vision", "tesseract", "disabled"],
                    help="OCR engine (default: Vision on macOS 15+, else "
                         "Tesseract)")
    args = ap.parse_args()

    set_langs(args.source, args.target, args.engine,
              ocr_engine=args.ocr or default_ocr_engine())
    if args.project:
        GCP_PROJECT = args.project
    if args.fixes:
        load_fixes(args.fixes)
    if not OCR_BIN.exists():
        sys.exit(f"OCR helper missing — build it first:\n"
                 f"  cd '{TOOLS_DIR}' && swiftc -O -o ocr ocr.swift")

    files = collect_inputs(args.inputs)
    if not files:
        sys.exit("no input images found")
    for f in files:
        log(f.name)
        out_dir = Path(args.out) if args.out else f.parent
        process_page(f, out_dir, force=args.force)


if __name__ == "__main__":
    main()
