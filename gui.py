#!/usr/bin/env python3
"""Browser GUI for the images translator.

Serves a single-page editor at http://localhost:<port>:
  - Open button (native macOS dialog) to load one image or a folder of scans
  - prev/next arrows + "current / total" page counter
  - the English translation drawn as a live overlay on the original scan,
    with a 0-100% opacity slider
  - per-block editing: double-click to edit text, drag edges to resize the
    block, font-size dropdown in the top bar for the selected block

Edits are saved back into the page's cache JSON (<outdir>/.cache/<stem>.json),
but ONLY for blocks the user actually touched:
  blk["en"]       - edited translation
  blk["gui_bbox"] - block rectangle as resized in the GUI
  blk["font_px"]  - font size chosen in the GUI (image pixels)
render_page() renders such blocks GUI-style (wrapped into the rectangle at
that font, white backing following the text), so Files > "Save current page"
(⌘S) / "Save all pages" — and any later CLI run — bake <stem>_en.png exactly
as edited.

Usage:
  python3 gui.py [--port 8877] [--project GCP_ID] [--fixes FILE] [-o OUTDIR]
                 [path ...]        # optional: preload images/folder on start

Pages are OCR'd + translated lazily the first time they are viewed (cached
afterwards), so opening a big folder is instant.
"""

import argparse
import json
import os
import re
import shutil
import threading
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from PIL import Image

import translate_pages as tp

HTML_PATH = Path(__file__).resolve().parent / "gui.html"
SETTINGS_FILE = Path(__file__).resolve().parent / "gui-settings.json"

SERVER = None                        # set in main(); used by /api/quit

STATE = {"files": [], "out": None,   # out: forced output dir (else input dir)
         "save_dir": None,           # folder chosen via a save dialog
         "save_paths": {},           # page idx -> exact path chosen for it
         "format_key": None,         # image format chosen at first save
         "save_pdf": False}          # wrap pages in searchable PDFs

# frontend format key -> (save_output fmt dict, file extension)
FORMAT_MAP = {
    "png":    ({"kind": "png"}, ".png"),
    "png8":   ({"kind": "png8"}, ".png"),
    "png8g":  ({"kind": "png8g"}, ".png"),
    "png1":   ({"kind": "png1"}, ".png"),
    "jpeg60": ({"kind": "jpeg", "quality": 60}, ".jpg"),
    "jpeg30": ({"kind": "jpeg", "quality": 30}, ".jpg"),
    "jpeg10": ({"kind": "jpeg", "quality": 10}, ".jpg"),
}
LOCK = threading.Lock()              # serializes OCR/translate + cache writes

_lang_lists = None       # cached (vision_langs, tess_langs, translate_langs)

# Source-language choices when Vision can't be queried (non-macOS). ocr_lang
# is always BCP-47; Tesseract maps it via tp.tess_lang.
STATIC_OCR_LANGS = [
    "en-US", "ja-JP", "ar-SA", "zh-Hans", "zh-Hant", "ko-KR", "fr-FR",
    "de-DE", "es-ES", "it-IT", "pt-BR", "ru-RU", "uk-UA", "th-TH",
    "vi-VT", "tr-TR", "id-ID", "cs-CZ", "da-DK", "nl-NL", "no-NO",
    "pl-PL", "ro-RO", "sv-SE", "hi-IN", "el-GR", "hu-HU", "fi-FI"]


def lang_lists():
    """(vision_langs, tesseract_langs, translate_langs).  Vision langs are
    BCP-47 strings; Tesseract langs are the models installed on this
    machine as {code, iso} objects (iso drives the display name)."""
    global _lang_lists
    if _lang_lists is None:
        try:
            vision = json.loads(subprocess.run(
                [str(tp.OCR_BIN), "--list-langs"],
                capture_output=True, text=True, timeout=30).stdout)
        except Exception:
            vision = STATIC_OCR_LANGS   # no Vision (older macOS / Linux)
        tess = [{"code": c, "iso": tp.TESS_TO_ISO.get(c)}
                for c in tp.tesseract_installed_langs()]
        try:
            tr = json.loads(subprocess.run(
                [str(tp.TRANSLATE_BIN), "--list-langs"],
                capture_output=True, text=True, timeout=30).stdout)
        except Exception:
            tr = ["en", "ar", "ja", "de", "es", "fr", "it", "ko", "pt",
                  "ru", "zh"]
        _lang_lists = (vision, tess, tr)
    return _lang_lists


def load_settings():
    try:
        s = json.loads(SETTINGS_FILE.read_text())
    except Exception:
        s = {"ocr_lang": "ja-JP", "target": "en", "engine": "auto"}
    # migrate the old boolean `ocr` flag; default engine is platform-aware
    if "ocr_engine" not in s:
        s["ocr_engine"] = (tp.default_ocr_engine() if s.get("ocr", True)
                           else "disabled")
    s.pop("ocr", None)
    return s


def apply_settings(s):
    tp.set_langs(s.get("ocr_lang"), s.get("target"), s.get("engine"),
                 ocr_engine=s.get("ocr_engine") or tp.default_ocr_engine())


# ------------------------------------------------------------- helpers

def out_dir_for(src):
    return STATE["out"] or src.parent


def collect(path):
    """All translatable images under path (file or dir), like the CLI."""
    p = Path(path).expanduser()
    if p.is_dir():
        return sorted(f for f in p.iterdir()
                      if f.suffix.lower() in tp.IMAGE_EXTS
                      and not tp.GENERATED_RE.search(f.stem))
    if p.is_file() and p.suffix.lower() in tp.IMAGE_EXTS:
        return [p]
    return []


def dialog_available():
    """Is a native file-dialog mechanism present on this OS?  When not
    (e.g. a headless Linux without zenity/kdialog), the frontend falls
    back to prompting for a path."""
    if sys.platform == "darwin":
        return bool(shutil.which("osascript"))
    if os.name == "nt":
        return bool(shutil.which("powershell") or shutil.which("pwsh"))
    return bool(shutil.which("zenity") or shutil.which("kdialog"))


def _run(args):
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() \
        else None


def _win_dialog(mode, default_name=""):
    ps = {
        "openfile": ("Add-Type -AssemblyName System.Windows.Forms;"
                     "$d=New-Object System.Windows.Forms.OpenFileDialog;"
                     "if($d.ShowDialog() -eq 'OK'){$d.FileName}"),
        "folder": ("Add-Type -AssemblyName System.Windows.Forms;"
                   "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                   "if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}"),
        "savefile": ("Add-Type -AssemblyName System.Windows.Forms;"
                     "$d=New-Object System.Windows.Forms.SaveFileDialog;"
                     f"$d.FileName='{default_name}';"
                     "if($d.ShowDialog() -eq 'OK'){$d.FileName}"),
    }[mode]
    exe = shutil.which("powershell") or shutil.which("pwsh")
    return _run([exe, "-NoProfile", "-STA", "-Command", ps]) if exe else None


def choose_path(kind):
    """Native open dialog (folder / image file); None if cancelled or no
    dialog tool is available.  macOS: osascript, Windows: PowerShell,
    Linux: zenity or kdialog."""
    if sys.platform == "darwin":
        expr = ('choose folder with prompt "Choose a folder of scanned pages"'
                if kind == "folder"
                else 'choose file with prompt "Choose a scanned page image"')
        return _run(["osascript", "-e", f"POSIX path of ({expr})"])
    if os.name == "nt":
        return _win_dialog("folder" if kind == "folder" else "openfile")
    if shutil.which("zenity"):
        args = ["zenity", "--file-selection", "--title=Choose scanned pages"]
        if kind == "folder":
            args.append("--directory")
        return _run(args)
    if shutil.which("kdialog"):
        flag = "--getexistingdirectory" if kind == "folder" \
            else "--getopenfilename"
        return _run(["kdialog", flag, os.path.expanduser("~")])
    return None


def choose_save_path(default_name):
    """Native save dialog; None if cancelled or no dialog tool available."""
    if sys.platform == "darwin":
        name = default_name.replace('"', '\\"')
        return _run(["osascript", "-e",
                     f'POSIX path of (choose file name with prompt '
                     f'"Save translated page as" default name "{name}")'])
    if os.name == "nt":
        return _win_dialog("savefile", default_name)
    home = os.path.expanduser("~")
    if shutil.which("zenity"):
        return _run(["zenity", "--file-selection", "--save",
                     "--confirm-overwrite",
                     "--filename=" + os.path.join(home, default_name)])
    if shutil.which("kdialog"):
        return _run(["kdialog", "--getsavefilename",
                     os.path.join(home, default_name)])
    return None


def png_for(src):
    return src if src.suffix.lower() == ".png" \
        else out_dir_for(src) / f"{src.stem}_jp.png"


def page_json(idx):
    """Everything the frontend needs to show one page."""
    src = STATE["files"][idx]
    with LOCK:      # first view of a page runs OCR + translation
        page_png, blocks, keepouts = tp.prepare_page(src, out_dir_for(src))
    w, h = Image.open(page_png).size
    tp.set_scale(w)
    out_blocks = []
    for i, blk in enumerate(blocks):
        if "en" not in blk or blk.get("deleted"):
            continue                      # non-Japanese / deleted: raw pixels
        slots = [s for ln in blk["lines"] for s in ln.get("segs", [ln])]
        med_h = (sorted(s["h"] for s in slots)[len(slots) // 2]
                 if slots else 0)         # GUI-created blocks have no lines
        # white patches erase the original Japanese, like render_page()
        patches = [[s["x"] - tp.PAD, s["y"] - tp.PAD_V,
                    s["w"] + 2 * tp.PAD, s["h"] + 2 * tp.PAD_V]
                   for s in slots]
        out_blocks.append({
            "id": i,
            "en": blk["en"],
            "bbox": blk.get("gui_bbox") or list(blk["bbox"]),
            "font_px": blk.get("font_px") or tp.default_font_px(blk, med_h),
            "line_spacing": blk.get("line_spacing") or 1.0,
            "color": blk.get("font_color") or "#141414",
            "bg": blk.get("bg_color") or "#ffffff",
            "align": blk.get("align") or ("right" if tp.is_rtl() else "left"),
            "bold": bool(blk.get("bold")),
            "patches": patches,
            # geometry-edited blocks lost their auto erase patches for good
            "edited": ("gui_bbox" in blk or "font_px" in blk
                       or "line_spacing" in blk),
        })
    return {"index": idx, "total": len(STATE["files"]), "name": src.name,
            "width": w, "height": h, "blocks": out_blocks,
            "user_patches": tp.load_user_patches(src, out_dir_for(src)),
            "rtl": tp.is_rtl()}


def save_user_patches(idx, patches):
    """Replace the page's patch-rectangle list in its cache."""
    src = STATE["files"][idx]
    cache = tp.cache_path(src, out_dir_for(src))
    with LOCK:
        data = json.loads(cache.read_text())
        data["user_patches"] = [
            {"bbox": [int(v) for v in p["bbox"]],
             "color": str(p.get("color") or "#ffffff")} for p in patches]
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def add_block(idx, fields):
    """Append a GUI-created text block to the page's cache; returns its id.
    Such blocks have no OCR lines — they render GUI-style only."""
    src = STATE["files"][idx]
    cache = out_dir_for(src) / ".cache" / f"{src.stem}.json"
    with LOCK:
        data = json.loads(cache.read_text())
        bbox = [int(v) for v in fields["bbox"]]
        blk = {"lines": [], "bbox": bbox, "text": "", "gui_created": True,
               "en": str(fields.get("en") or "Text"),
               "gui_bbox": bbox,
               "font_px": int(fields.get("font_px") or 60)}
        data["blocks"].append(blk)
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        return len(data["blocks"]) - 1


def render_png(idx):
    """Bake one page, honoring GUI edits, to the location and format
    chosen at the first save (exact path for that page if one was picked,
    else the chosen folder).  Returns the written Path, or None when no
    save target was chosen yet (caller shows the save dialog)."""
    src = STATE["files"][idx]
    fmt, ext = FORMAT_MAP[STATE["format_key"] or "png"]
    if STATE["save_pdf"]:
        fmt, ext = dict(fmt, pdf=True), ".pdf"
    out_png = STATE["save_paths"].get(idx)
    if not out_png and STATE["save_dir"]:
        out_png = STATE["save_dir"] / f"{src.stem}_{tp.TARGET_LANG}{ext}"
    if not out_png:
        return None
    with LOCK:
        return tp.process_page(src, out_dir_for(src), out_png=out_png,
                               fmt=fmt)


def save_edits(idx, edits):
    """Merge GUI edits into the page's cache JSON.  Each edit carries only
    the fields the user changed — a text-only edit must NOT set
    gui_bbox/font_px, or the block would needlessly switch from the CLI's
    slot layout to GUI-style rendering."""
    src = STATE["files"][idx]
    cache = out_dir_for(src) / ".cache" / f"{src.stem}.json"
    with LOCK:
        data = json.loads(cache.read_text())
        for e in edits:
            blk = data["blocks"][e["id"]]
            if "en" in e:
                # trailing whitespace/newlines silently inflate the text
                # backing; leading spaces stay (deliberate indents)
                blk["en"] = str(e["en"]).rstrip()
            if "bbox" in e:
                blk["gui_bbox"] = [int(v) for v in e["bbox"]]
            if "font_px" in e:
                blk["font_px"] = int(e["font_px"])
            if "line_spacing" in e:
                blk["line_spacing"] = round(float(e["line_spacing"]), 1)
            if "color" in e:
                blk["font_color"] = str(e["color"])
            if "bg" in e:
                blk["bg_color"] = str(e["bg"])
            if "align" in e:
                blk["align"] = str(e["align"])
            if "bold" in e:
                blk["bold"] = bool(e["bold"])
            if "deleted" in e:
                blk["deleted"] = bool(e["deleted"])
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=1))


# -------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):     # quiet request log
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if url.path == "/":
                self._send(200, HTML_PATH.read_text(), "text/html; charset=utf-8")
            elif parts[:2] == ["api", "open"]:
                # /api/open?path=... : open without the native dialog
                path = unquote(parse_qs(url.query).get("path", [""])[0])
                self.api_open({"path": path})
            elif parts[:2] == ["api", "settings"]:
                s = load_settings()
                vision_langs, tess_langs, tr_langs = lang_lists()
                s.update(vision_langs=vision_langs, tesseract_langs=tess_langs,
                         translate_langs=tr_langs)
                self._send(200, s)
            elif parts[:2] == ["api", "tessstatus"]:
                # is the source language's Tesseract model installed?
                lang = unquote(parse_qs(url.query).get("lang", [""])[0])
                code = tp.to_tess_code(lang)
                installed = tp.tesseract_installed_langs()
                self._send(200, {
                    "code": code,
                    "ok": bool(installed) and code in installed,
                    "available": bool(installed)})
            elif parts[:2] == ["api", "applestatus"]:
                qs = parse_qs(url.query)
                src = qs.get("source", [""])[0]
                tgt = qs.get("target", [""])[0]
                # the Apple helper is macOS-only; report cleanly elsewhere
                if not tp.TRANSLATE_BIN.exists():
                    self._send(200, {"status": "unavailable"})
                    return
                try:
                    out = subprocess.run(
                        [str(tp.TRANSLATE_BIN), "--status", src, tgt],
                        capture_output=True, text=True, timeout=30)
                    self._send(200, {"status": out.stdout.strip() or "unknown"})
                except Exception as e:
                    self._send(200, {"status": "unknown", "error": str(e)})
            elif parts[:2] == ["api", "page"] and len(parts) == 3:
                idx = int(parts[2])
                if not 0 <= idx < len(STATE["files"]):
                    self._send(404, {"error": "no such page"})
                else:
                    self._send(200, page_json(idx))
            elif parts[:1] == ["img"] and len(parts) == 2:
                png = png_for(STATE["files"][int(parts[1])])
                self._send(200, png.read_bytes(), "image/png")
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            if parts[:2] == ["api", "open"]:
                self.api_open(self._json_body())
            elif parts[:2] == ["api", "settings"]:
                body = self._json_body()
                s = load_settings()
                s.update({k: body[k] for k in ("ocr_lang", "target", "engine")
                          if body.get(k)})
                if body.get("ocr_engine"):
                    s["ocr_engine"] = body["ocr_engine"]
                # Tesseract needs the source language's traineddata; block
                # the save with a helpful message if it's missing.
                if s.get("ocr_engine") == "tesseract":
                    code = tp.to_tess_code(s.get("ocr_lang") or "eng")
                    installed = tp.tesseract_installed_langs()
                    if not installed:
                        self._send(200, {"error":
                            "Tesseract is not installed or not on PATH. "
                            "Install it (macOS: brew install tesseract)."})
                        return
                    if code not in installed:
                        self._send(200, {"error":
                            f"Tesseract has no language model \"{code}\" for "
                            f"the source language. Install it — macOS: "
                            f"brew install tesseract-lang; Linux: "
                            f"apt install tesseract-ocr-{code}; or download "
                            f"{code}.traineddata into your tessdata folder."})
                        return
                SETTINGS_FILE.write_text(json.dumps(s, indent=1))
                apply_settings(s)
                self._send(200, {"ok": True})
            elif parts[:2] == ["api", "appleprepare"]:
                body = self._json_body()
                # visible macOS dialog; runs detached, user approves there
                subprocess.Popen([str(tp.TRANSLATE_BIN), "--prepare",
                                  body["source"], body["target"]])
                self._send(200, {"ok": True})
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "save"):
                save_edits(int(parts[2]), self._json_body()["edits"])
                self._send(200, {"ok": True})
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "add"):
                self._send(200, {"id": add_block(int(parts[2]),
                                                 self._json_body())})
            elif parts[:2] == ["api", "quit"]:
                # the double-click launcher has no Dock icon or console, so
                # the UI needs a way to stop the server
                self._send(200, {"ok": True})
                if SERVER is not None:
                    threading.Timer(0.3, SERVER.shutdown).start()
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "patches"):
                save_user_patches(int(parts[2]),
                                  self._json_body().get("patches", []))
                self._send(200, {"ok": True})
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "refresh"):
                # re-process from scratch: discard cache + edits, re-run
                # OCR and translation as on first load
                src = STATE["files"][int(parts[2])]
                with LOCK:
                    tp.prepare_page(src, out_dir_for(src), force=True)
                self._send(200, {"ok": True})
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "render"):
                out = render_png(int(parts[2]))
                if out is None:      # first save: frontend runs the dialog
                    self._send(200, {"need_dialog": True})
                else:
                    self._send(200, {"file": out.name,
                                     "dir": str(out.parent)})
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "render_as"):
                idx = int(parts[2])
                src = STATE["files"][idx]
                body = self._json_body()
                key = body.get("format") or "png"
                pdf = bool(body.get("pdf"))
                fmt, ext = FORMAT_MAP[key]
                if pdf:
                    fmt, ext = dict(fmt, pdf=True), ".pdf"
                path = body.get("path")   # frontend-provided (no-dialog OS)
                if not path:
                    if not dialog_available():
                        self._send(200, {"no_dialog": True, "ext": ext,
                                         "suggested": f"{src.stem}_"
                                         f"{tp.TARGET_LANG}{ext}"})
                        return
                    path = choose_save_path(f"{src.stem}_{tp.TARGET_LANG}{ext}")
                if not path:
                    self._send(200, {"cancelled": True})
                    return
                # the format decides the encoding — force a matching extension
                path = re.sub(r"\.(png|jpe?g|tiff?|bmp|pdf)$", "", path,
                              flags=re.I) + ext
                STATE["save_paths"][idx] = Path(path)
                STATE["save_dir"] = Path(path).parent
                STATE["format_key"] = key
                STATE["save_pdf"] = pdf
                with LOCK:
                    out = tp.process_page(src, out_dir_for(src),
                                          out_png=Path(path), fmt=fmt)
                self._send(200, {"file": out.name, "dir": str(out.parent)})
            elif parts[:2] == ["api", "render_all"]:
                body = self._json_body()
                key = body.get("format") or "png"
                pdf = bool(body.get("pdf"))
                fmt, ext = FORMAT_MAP[key]
                if pdf:
                    fmt, ext = dict(fmt, pdf=True), ".pdf"
                folder = body.get("folder")   # frontend-provided (no-dialog)
                if not folder:
                    if not dialog_available():
                        self._send(200, {"no_dialog": True})
                        return
                    folder = choose_path("folder")
                if not folder:
                    self._send(200, {"cancelled": True})
                    return
                folder = Path(folder)
                STATE["save_dir"] = folder
                STATE["save_paths"] = {}
                STATE["format_key"] = key
                STATE["save_pdf"] = pdf
                for src in STATE["files"]:
                    with LOCK:   # OCRs/translates never-viewed pages too
                        tp.process_page(
                            src, out_dir_for(src), fmt=fmt,
                            out_png=folder /
                            f"{src.stem}_{tp.TARGET_LANG}{ext}")
                self._send(200, {"count": len(STATE["files"]),
                                 "dir": str(folder)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def api_open(self, body):
        path = body.get("path")
        if not path:
            if not dialog_available():
                self._send(200, {"no_dialog": True})
                return
            path = choose_path(body.get("kind", "file"))
            if not path:
                self._send(200, {"cancelled": True})
                return
        files = collect(path)
        if not files:
            self._send(200, {"error": f"no images found in {path}"})
            return
        STATE["files"] = files
        STATE["save_dir"] = None      # new document: back to default saves
        STATE["save_paths"] = {}
        STATE["format_key"] = None
        STATE["save_pdf"] = False
        self._send(200, {"total": len(files)})


def main():
    ap = argparse.ArgumentParser(description="GUI for the images translator.")
    ap.add_argument("inputs", nargs="*", help="image files or a directory to preload")
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("-o", "--out", help="output directory (default: next to inputs)")
    ap.add_argument("--project", help="GCP project for Cloud Translation API")
    ap.add_argument("--fixes", help="JSON file with document-specific OCR fixes")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't auto-open the page in the default browser")
    args = ap.parse_args()

    apply_settings(load_settings())
    if args.project:
        tp.GCP_PROJECT = args.project
    if args.fixes:
        tp.load_fixes(args.fixes)
    if args.out:
        STATE["out"] = Path(args.out)
    for p in args.inputs:
        STATE["files"] += collect(p)
    if tp.OCR_ENGINE == "vision" and not tp.OCR_BIN.exists():
        sys.exit(f"OCR helper missing — build it first:\n"
                 f"  cd '{tp.TOOLS_DIR}' && swiftc -O -o ocr ocr.swift\n"
                 f"(or choose Tesseract / Disable in Settings)")

    global SERVER
    srv = SERVER = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}/"
    tp.log(f"images translator GUI: {url}")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
