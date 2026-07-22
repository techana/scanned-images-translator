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
import re
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

STATE = {"files": [], "out": None,   # out: forced output dir (else input dir)
         "save_dir": None,           # folder chosen via "Save current as…"
         "save_paths": {}}           # page idx -> exact path chosen for it
LOCK = threading.Lock()              # serializes OCR/translate + cache writes

_lang_lists = None                   # cached (ocr_langs, translate_langs)


def lang_lists():
    global _lang_lists
    if _lang_lists is None:
        try:
            ocr = json.loads(subprocess.run(
                [str(tp.OCR_BIN), "--list-langs"],
                capture_output=True, text=True, timeout=30).stdout)
        except Exception:
            ocr = ["ja-JP", "en-US"]
        try:
            tr = json.loads(subprocess.run(
                [str(tp.TRANSLATE_BIN), "--list-langs"],
                capture_output=True, text=True, timeout=30).stdout)
        except Exception:
            tr = ["en", "ar", "ja", "de", "es", "fr", "it", "ko", "pt",
                  "ru", "zh"]
        _lang_lists = (ocr, tr)
    return _lang_lists


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {"ocr_lang": "ja-JP", "target": "en", "engine": "auto"}


def apply_settings(s):
    tp.set_langs(s.get("ocr_lang"), s.get("target"), s.get("engine"))


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


def choose_path(kind):
    """Native macOS open dialog via osascript; None if cancelled."""
    if kind == "folder":
        expr = 'choose folder with prompt "Choose a folder of scanned pages"'
    else:
        expr = 'choose file with prompt "Choose a scanned page image"'
    try:
        out = subprocess.run(["osascript", "-e", f"POSIX path of ({expr})"],
                             capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def choose_save_path(default_name):
    """Native macOS save dialog; None if cancelled."""
    name = default_name.replace('"', '\\"')
    expr = (f'POSIX path of (choose file name with prompt '
            f'"Save translated page as" default name "{name}")')
    try:
        out = subprocess.run(["osascript", "-e", expr],
                             capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


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
            "font_px": blk.get("font_px") or max(tp.MIN_FONT,
                                                 int(med_h * 0.88)),
            "line_spacing": blk.get("line_spacing") or 1.0,
            "color": blk.get("font_color") or "#141414",
            "bg": blk.get("bg_color") or "#ffffff",
            "patches": patches,
            # geometry-edited blocks lost their auto erase patches for good
            "edited": ("gui_bbox" in blk or "font_px" in blk
                       or "line_spacing" in blk),
        })
    return {"index": idx, "total": len(STATE["files"]), "name": src.name,
            "width": w, "height": h, "blocks": out_blocks,
            "rtl": tp.is_rtl()}


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
    """Bake <stem>_<target>.png for one page, honoring GUI edits
    (process_page renders GUI-edited blocks exactly as shown).
    After a "Save current as…", saves keep going to the chosen place:
    the exact path for that page, the chosen folder for other pages.
    Returns the written Path."""
    src = STATE["files"][idx]
    out_png = STATE["save_paths"].get(idx)
    if not out_png and STATE["save_dir"]:
        out_png = STATE["save_dir"] / f"{src.stem}_{tp.TARGET_LANG}.png"
    with LOCK:
        return tp.process_page(src, out_dir_for(src), out_png=out_png)


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
                ocr_langs, tr_langs = lang_lists()
                s.update(ocr_langs=ocr_langs, translate_langs=tr_langs)
                self._send(200, s)
            elif parts[:2] == ["api", "applestatus"]:
                qs = parse_qs(url.query)
                src = qs.get("source", [""])[0]
                tgt = qs.get("target", [""])[0]
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
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "render"):
                out = render_png(int(parts[2]))
                self._send(200, {"file": out.name, "dir": str(out.parent)})
            elif (parts[:2] == ["api", "page"] and len(parts) == 4
                    and parts[3] == "render_as"):
                src = STATE["files"][int(parts[2])]
                path = choose_save_path(f"{src.stem}_{tp.TARGET_LANG}.png")
                if not path:
                    self._send(200, {"cancelled": True})
                    return
                if not re.search(r"\.(png|jpe?g|tiff?|bmp)$", path, re.I):
                    path += ".png"
                idx = int(parts[2])
                STATE["save_paths"][idx] = Path(path)
                STATE["save_dir"] = Path(path).parent
                with LOCK:
                    out = tp.process_page(src, out_dir_for(src),
                                          out_png=Path(path))
                self._send(200, {"file": out.name, "dir": str(out.parent)})
            elif parts[:2] == ["api", "render_all"]:
                for i in range(len(STATE["files"])):
                    render_png(i)          # OCRs/translates unseen pages too
                self._send(200, {"count": len(STATE["files"])})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def api_open(self, body):
        path = body.get("path")
        if not path:
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
    if not tp.OCR_BIN.exists():
        sys.exit(f"OCR helper missing — build it first:\n"
                 f"  cd '{tp.TOOLS_DIR}' && swiftc -O -o ocr ocr.swift")

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
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
