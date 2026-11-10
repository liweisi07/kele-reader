#!/usr/bin/env python3
"""满月阅读 manyue：飞牛 FnOS 原生电子书/漫画阅读应用。

书架 + 阅读器，跑在 NAS 上，手机/电脑用浏览器在局域网内打开。
支持 PDF（漫画主力）、EPUB、TXT、AZW3 四种格式，**不设文件大小
上限**：大文件靠 HTTP Range 流式读取，浏览器只拉需要的部分，
不会把整个文件读进内存。

  * PDF：浏览器端 PDF.js 渲染，竖向连续滚动、按需渲染页面。
  * EPUB：浏览器端 epub.js 渲染。
  * TXT：按 Range 分块拉取，客户端分页。
  * AZW3：服务端用 vendored 的 `mobi` 包（KindleUnpack 的 fork，
    GPL-3.0，见 app/vendor/mobi-LICENSE）解包成 EPUB/HTML，再拼
    成一页可滚动的 HTML 给阅读器里的 iframe；带 DRM 的打不开，
    会明确报错。
  * 阅读进度存在本机 data/progress.json，换设备打开同一本书
    也能接着看；封面：EPUB 用内嵌封面，PDF 由浏览器渲染第 1 页
    后回传缓存，其余用标题色块。
  * PWA：manifest + service worker，手机浏览器“添加到主屏幕”
    后可全屏打开，像原生应用一样。

只用 Python 标准库（AZW3 解包用的 mobi 包已随应用一起打包）。
设置页与阅读页只应在局域网内访问，不要把端口映射到公网。
"""

import hashlib
import html
import io
import json
import os
import posixpath
import re
import shutil
import sys
import threading
import time
import urllib.parse
import warnings
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

warnings.filterwarnings("ignore", category=DeprecationWarning)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(APP_DIR, "vendor")
STATIC_DIR = os.path.join(APP_DIR, "static")
sys.path.insert(0, VENDOR_DIR)
sys.path.insert(0, APP_DIR)

VERSION = "1.0.0"
APPNAME = "manyue"
DEFAULT_PORT = 18090
BOOK_EXTS = {".pdf": "pdf", ".epub": "epub", ".txt": "txt", ".azw3": "azw3"}
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "epub": "application/epub+zip",
    "txt": "text/plain; charset=utf-8",
    "azw3": "application/vnd.amazon.mobi8-ebook",
}
MAX_JSON_BODY = 64 * 1024
MAX_COVER_BODY = 2 * 1024 * 1024
SCAN_TTL = 300  # 书库扫描缓存 5 分钟，改动用“重新扫描”


# --------------------------------------------------------------------------
# Paths / environment
# --------------------------------------------------------------------------

def resolve_data_dir():
    for key in ("MANYUE_DATA_DIR", "TRIM_PKGVAR"):
        value = os.environ.get(key)
        if value:
            return value
    return os.path.join(APP_DIR, "data")


DATA_DIR = resolve_data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
COVERS_DIR = os.path.join(DATA_DIR, "covers")
AZW3_DIR = os.path.join(DATA_DIR, "azw3")


def resolve_port():
    for key in ("MANYUE_PORT", "wizard_port", "TRIM_SERVICE_PORT"):
        value = os.environ.get(key)
        if value:
            try:
                port = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= port <= 65535:
                return port
    return DEFAULT_PORT


# --------------------------------------------------------------------------
# Config / progress (atomic JSON files in the data dir)
# --------------------------------------------------------------------------

_config_lock = threading.Lock()
_config = {"book_folder": ""}

_progress_lock = threading.Lock()
_progress = {"books": {}}


def _atomic_write(path, data_bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data_bytes)
    os.replace(tmp, path)


def load_config():
    global _config
    existed = os.path.isfile(CONFIG_PATH)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(
                data.get("book_folder"), str):
            with _config_lock:
                _config["book_folder"] = data["book_folder"].strip()
    except (OSError, ValueError):
        pass
    if not existed and not _config.get("book_folder"):
        # First run: seed the book folder from the install wizard.
        folder = (os.environ.get("wizard_book_folder") or "").strip()
        if folder:
            with _config_lock:
                _config["book_folder"] = folder
            save_config()


def save_config():
    with _config_lock:
        payload = json.dumps(_config, ensure_ascii=False, indent=2)
    _atomic_write(CONFIG_PATH, payload.encode("utf-8"))


def get_config_copy():
    with _config_lock:
        return dict(_config)


def load_progress():
    global _progress
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("books"), dict):
            with _progress_lock:
                _progress = data
    except (OSError, ValueError):
        pass


def save_progress():
    with _progress_lock:
        payload = json.dumps(_progress, ensure_ascii=False, indent=2)
    _atomic_write(PROGRESS_PATH, payload.encode("utf-8"))


def get_progress_copy():
    with _progress_lock:
        return json.loads(json.dumps(_progress, ensure_ascii=False))


def set_progress(book_id, loc, total):
    with _progress_lock:
        _progress.setdefault("books", {})[book_id] = {
            "loc": loc,
            "total": total,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    save_progress()


# --------------------------------------------------------------------------
# Library scan
# --------------------------------------------------------------------------

def book_id_for(rel_path):
    """Stable id from the path relative to the book folder."""
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


def _fmt_size(size):
    if size >= 1024 * 1024:
        return "%.1f MB" % (size / 1024.0 / 1024.0)
    if size >= 1024:
        return "%.0f KB" % (size / 1024.0)
    return "%d B" % size


def _epub_meta(path):
    """(title, author) from an EPUB's OPF; ('', '') on any failure."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            container = None
            for name in names:
                if name.lower().endswith("meta-inf/container.xml"):
                    container = name
                    break
            if not container:
                return "", ""
            import xml.etree.ElementTree as ET
            root = ET.fromstring(zf.read(container))
            ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rootfile = root.find(".//c:rootfile", ns)
            if rootfile is None:
                return "", ""
            opf_path = rootfile.get("full-path")
            opf = ET.fromstring(zf.read(opf_path))
            dc = "{http://purl.org/dc/elements/1.1/}"
            title = ""
            author = ""
            title_el = opf.find(".//%stitle" % dc)
            if title_el is not None and title_el.text:
                title = title_el.text.strip()
            creator_el = opf.find(".//%screator" % dc)
            if creator_el is not None and creator_el.text:
                author = creator_el.text.strip()
            return title, author
    except Exception:  # noqa: BLE001 - metadata is best-effort
        return "", ""


def _cover_exists(book_id):
    for ext in (".jpg", ".png"):
        if os.path.isfile(os.path.join(COVERS_DIR, book_id + ext)):
            return True
    return False


_scan_lock = threading.Lock()
_scan_cache = {"folder": None, "at": 0.0, "books": [], "error": ""}
_epub_meta_cache = {}
_no_cover = set()


def scan_books(force=False):
    """Scan the book folder. Returns (books, error). Cached SCAN_TTL."""
    cfg = get_config_copy()
    folder = (cfg.get("book_folder") or "").strip()
    now = time.time()
    with _scan_lock:
        if (not force and _scan_cache["folder"] == folder
                and now - _scan_cache["at"] < SCAN_TTL):
            return list(_scan_cache["books"]), _scan_cache["error"]
    books = []
    error = ""
    if not folder:
        error = "未设置书库文件夹"
    elif not os.path.isdir(folder):
        error = "书库文件夹不存在：%s" % folder
    else:
        try:
            os.listdir(folder)
        except OSError as exc:
            error = "书库文件夹打不开（%s）。请在飞牛文件管理里给 manyue 账号该文件夹的读取权限。" % exc
    if not error:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                if name.startswith("."):
                    continue
                ext = os.path.splitext(name)[1].lower()
                fmt = BOOK_EXTS.get(ext)
                if not fmt:
                    continue
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, folder)
                book_id = book_id_for(rel)
                title = os.path.splitext(name)[0]
                author = ""
                if fmt == "epub":
                    key = (full, st.st_mtime, st.st_size)
                    cached = _epub_meta_cache.get(key)
                    if cached is None:
                        cached = _epub_meta(full)
                        _epub_meta_cache[key] = cached
                    if cached[0]:
                        title = cached[0]
                    author = cached[1]
                books.append({
                    "id": book_id,
                    "title": title,
                    "author": author,
                    "format": fmt,
                    "size": st.st_size,
                    "size_text": _fmt_size(st.st_size),
                    "rel_path": rel.replace(os.sep, "/"),
                    "folder": os.path.dirname(rel).replace(os.sep, "/"),
                    "mtime": time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(st.st_mtime)),
                    "_path": full,
                })
        books.sort(key=lambda b: (b["folder"].lower(),
                                  b["title"].lower()))
    with _scan_lock:
        _scan_cache["folder"] = folder
        _scan_cache["at"] = now
        _scan_cache["books"] = books
        _scan_cache["error"] = error
    return list(books), error


def find_book(book_id):
    """Book dict (with _path) for an id, or None."""
    books, _err = scan_books()
    for book in books:
        if book["id"] == book_id:
            return book
    return None


def public_book(book):
    """Book dict safe to send to the client (no local path).

    cover_url is computed here (not at scan time) so a cover uploaded
    after the scan still shows up right away despite the scan cache.
    """
    out = {k: v for k, v in book.items() if not k.startswith("_")}
    out["cover_url"] = ("/api/cover/%s" % book["id"]
                        if _cover_exists(book["id"]) else None)
    return out


def folder_status():
    """(readable, error) for the configured book folder."""
    cfg = get_config_copy()
    folder = (cfg.get("book_folder") or "").strip()
    if not folder:
        return False, "未设置书库文件夹"
    if not os.path.isdir(folder):
        return False, "书库文件夹不存在：%s" % folder
    try:
        os.listdir(folder)
    except OSError as exc:
        return False, ("书库文件夹打不开（%s）。请在飞牛文件管理里给 "
                       "manyue 账号该文件夹的读取权限。" % exc)
    return True, ""


# --------------------------------------------------------------------------
# Covers (EPUB embedded cover, extracted lazily; PDF covers are uploaded
# by the browser after it renders page 1 with PDF.js)
# --------------------------------------------------------------------------

def _detect_image_ext(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return None


def extract_epub_cover(book):
    """Extract an EPUB's embedded cover to the covers dir. Path or None."""
    key = (book["id"], book["_path"])
    if key in _no_cover:
        return None
    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(book["_path"]) as zf:
            names = zf.namelist()
            container = None
            for name in names:
                if name.lower().endswith("meta-inf/container.xml"):
                    container = name
                    break
            if not container:
                _no_cover.add(key)
                return None
            root = ET.fromstring(zf.read(container))
            ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
            rootfile = root.find(".//c:rootfile", ns)
            if rootfile is None:
                _no_cover.add(key)
                return None
            opf_path = rootfile.get("full-path")
            opf = ET.fromstring(zf.read(opf_path))
            opf_ns = "{http://www.idpf.org/2007/opf}"
            opf_dir = posixpath.dirname(opf_path)
            cover_id = None
            for meta in opf.findall(".//%smeta" % opf_ns):
                if meta.get("name") == "cover":
                    cover_id = meta.get("content")
                    break
            cover_href = None
            for item in opf.findall(".//%smanifest/%sitem" % (opf_ns, opf_ns)):
                props = (item.get("properties") or "").split()
                if item.get("id") == cover_id or "cover-image" in props:
                    cover_href = item.get("href")
                    break
            if not cover_href:
                _no_cover.add(key)
                return None
            member = posixpath.normpath(
                posixpath.join(opf_dir, cover_href)) if opf_dir else \
                posixpath.normpath(cover_href)
            if member.startswith(".."):
                _no_cover.add(key)
                return None
            data = zf.read(member)
            ext = _detect_image_ext(data)
            if not ext:
                _no_cover.add(key)
                return None
            os.makedirs(COVERS_DIR, exist_ok=True)
            out = os.path.join(COVERS_DIR, book["id"] + ext)
            _atomic_write(out, data)
            return out
    except Exception:  # noqa: BLE001 - no cover is not an error
        _no_cover.add(key)
        return None


def cover_path_for(book):
    for ext in (".jpg", ".png"):
        path = os.path.join(COVERS_DIR, book["id"] + ext)
        if os.path.isfile(path):
            return path
    if book["format"] == "epub":
        return extract_epub_cover(book)
    return None


# --------------------------------------------------------------------------
# File serving with HTTP Range (the no-size-limit core: the browser only
# pulls the bytes it needs; a 300 MB PDF is never read whole)
# --------------------------------------------------------------------------

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def send_file_range(handler, path, content_type):
    """Serve path honoring a single Range request. 200 / 206 / 416."""
    try:
        size = os.path.getsize(path)
    except OSError:
        handler.send_error(404, "File not found")
        return
    range_hdr = (handler.headers.get("Range") or "").strip()
    start, end, status = 0, size - 1, 200
    if range_hdr:
        match = _RANGE_RE.match(range_hdr)
        if match:
            first, last = match.group(1), match.group(2)
            if first == "" and last != "":
                # Suffix range: last N bytes.
                start = max(0, size - int(last))
            elif first != "":
                start = int(first)
                end = int(last) if last != "" else size - 1
                if end >= size:
                    # RFC 7233: over-long end clamps to the last byte;
                    # only a start past EOF (or end < start) is 416.
                    end = size - 1
            if start >= size or end < start:
                handler.send_response(416)
                handler.send_header("Content-Range",
                                    "bytes */%d" % size)
                handler.send_header("Accept-Ranges", "bytes")
                handler.end_headers()
                return
            status = 206
    length = end - start + 1
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(length))
    if status == 206:
        handler.send_header("Content-Range",
                            "bytes %d-%d/%d" % (start, end, size))
    handler.end_headers()
    if handler.command == "HEAD":
        return
    with open(path, "rb") as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            chunk = fh.read(min(65536, remaining))
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            remaining -= len(chunk)


def send_bytes(handler, data, content_type, status=200):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def send_json(handler, obj, status=200):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    send_bytes(handler, data, "application/json; charset=utf-8", status)


def send_text(handler, text, content_type="text/html; charset=utf-8",
              status=200):
    send_bytes(handler, text.encode("utf-8"), content_type, status)


# --------------------------------------------------------------------------
# AZW3: extract with the vendored `mobi` package, then serve as one
# scrollable HTML page (KF8 -> EPUB -> concatenated chapters; MOBI7 ->
# the unpacked HTML with a <base> tag). Cached under data/azw3/<id>/.
# --------------------------------------------------------------------------

_azw3_lock = threading.Lock()

RES_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".css": "text/css", ".html": "text/html; charset=utf-8",
    ".xhtml": "application/xhtml+xml; charset=utf-8",
    ".xml": "application/xml", ".ncx": "application/xml",
    ".ttf": "font/ttf", ".otf": "font/otf",
    ".woff": "font/woff", ".woff2": "font/woff2",
}

EPUB_BASE_CSS = (
    "html{font-size:18px}"
    "body{font-size:1rem;line-height:1.8;color:#262626;"
    "background:#f7f3ea;max-width:720px;margin:0 auto;"
    "padding:16px 18px;font-family:system-ui,-apple-system,"
    "\"PingFang SC\",\"Microsoft YaHei\",sans-serif}"
    "img,svg{max-width:100%;height:auto}"
    "h1,h2,h3{line-height:1.4}"
    "p{margin:.6em 0}"
    "a{color:#1677ff}"
    "table{border-collapse:collapse}"
    "td,th{border:1px solid #d9d9d9;padding:4px 8px}"
)

_ATTR_RE = re.compile(
    r"""((?:src|href|xlink:href)\s*=\s*)(["'])([^"']*)\2""")
_URL_RE = re.compile(r"""url\(\s*(["']?)([^"'\)]+)\1\s*\)""")
_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_HEAD_RE = re.compile(
    r"<head\b[^>]*>.*?</head\s*>", re.IGNORECASE | re.DOTALL)
_BODY_RE = re.compile(
    r"<body[^>]*>(.*)</body\s*>", re.IGNORECASE | re.DOTALL)


def _resolve_ref(base_dir, ref):
    """Resolve a relative ref against base_dir (posix, inside the book).

    Returns the normalized in-book path, or None for external links,
    anchors, absolute paths and escapes above the book root.
    """
    if not ref:
        return None
    low = ref.lower()
    if low.startswith(("#", "http:", "https:", "mailto:", "data:", "/")):
        return None
    joined = posixpath.normpath(posixpath.join(base_dir, ref))
    if joined.startswith("..") or joined == "..":
        return None
    return joined


def _res_url(book_id, in_book_path):
    return "/api/azw3/%s/res/%s" % (
        book_id, urllib.parse.quote(in_book_path))


def _rewrite_refs(text, base_dir, book_id, chapter_map=None):
    """Rewrite src/href/xlink:href/url(...) in a chapter to res/ URLs.

    Internal links to another spine chapter become in-page anchors
    (``#manyue-ch-<itemid>`` or the original ``#fragment``), so the
    concatenated page stays in one piece instead of navigating the
    reader away to a raw chapter file.
    """
    chapter_map = chapter_map or {}

    def _split_ref(ref):
        if "#" in ref:
            base, frag = ref.split("#", 1)
            return base, frag
        return ref, None

    def _attr(match):
        prefix, quote, ref = match.group(1), match.group(2), match.group(3)
        attr = prefix.strip().split("=")[0].strip().lower()
        base, frag = _split_ref(ref)
        if not base:
            return match.group(0)
        target = _resolve_ref(base_dir, base)
        if target is None:
            return match.group(0)
        if attr == "href" and target in chapter_map:
            anchor = ("#" + frag) if frag else (
                "#" + chapter_map[target])
            return "%s%s%s%s" % (prefix, quote, anchor, quote)
        url = _res_url(book_id, target)
        if frag:
            url += "#" + frag
        return "%s%s%s%s" % (prefix, quote, url, quote)

    def _url(match):
        quote, ref = match.group(1), match.group(2)
        target = _resolve_ref(base_dir, ref)
        if target is None:
            return match.group(0)
        return "url(%s%s%s)" % (
            quote, _res_url(book_id, target), quote)

    text = _SCRIPT_RE.sub("", text)
    text = _LINK_RE.sub("", text)
    text = _ATTR_RE.sub(_attr, text)
    text = _URL_RE.sub(_url, text)
    return text


def _opf_info(epub_dir):
    """(opf_dir, manifest, spine, title) for an unzipped EPUB dir."""
    import xml.etree.ElementTree as ET
    with open(os.path.join(epub_dir, "META-INF", "container.xml"),
              "rb") as fh:
        container = ET.fromstring(fh.read())
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = container.find(".//c:rootfile", ns)
    opf_rel = rootfile.get("full-path")
    with open(os.path.join(epub_dir, *opf_rel.split("/")), "rb") as fh:
        opf = ET.fromstring(fh.read())
    opf_ns = "{http://www.idpf.org/2007/opf}"
    manifest = {}
    for item in opf.findall(".//%smanifest/%sitem" % (opf_ns, opf_ns)):
        manifest[item.get("id")] = (item.get("href"),
                                     item.get("media-type"))
    spine = [ref.get("idref") for ref in
             opf.findall(".//%sspine/%sitemref" % (opf_ns, opf_ns))]
    title = ""
    dc = "{http://purl.org/dc/elements/1.1/}"
    title_el = opf.find(".//%stitle" % dc)
    if title_el is not None and title_el.text:
        title = title_el.text.strip()
    return posixpath.dirname(opf_rel), manifest, spine, title


def build_epub_html(epub_dir, out_path, book_id):
    """Concatenate an unzipped EPUB's spine into one HTML page."""
    opf_dir, manifest, spine, title = _opf_info(epub_dir)
    css_parts = []
    for _item_id, (href, mtype) in manifest.items():
        if mtype != "text/css":
            continue
        css_rel = posixpath.join(opf_dir, href) if opf_dir else href
        css_full = os.path.join(epub_dir, *css_rel.split("/"))
        try:
            with open(css_full, "r", encoding="utf-8",
                      errors="replace") as fh:
                css_text = fh.read()
        except OSError:
            continue
        css_dir = posixpath.dirname(css_rel)
        css_text = _rewrite_refs(css_text, css_dir, book_id)
        css_parts.append(css_text)
    sections = []
    chapter_map = {}
    for item_id in spine:
        entry = manifest.get(item_id)
        if not entry:
            continue
        href, mtype = entry
        if mtype not in ("application/xhtml+xml", "text/html"):
            continue
        chap_rel = posixpath.join(opf_dir, href) if opf_dir else href
        chapter_map[chap_rel] = "manyue-ch-" + item_id
    for item_id in spine:
        entry = manifest.get(item_id)
        if not entry:
            continue
        href, mtype = entry
        if mtype not in ("application/xhtml+xml", "text/html"):
            continue
        chap_rel = posixpath.join(opf_dir, href) if opf_dir else href
        chap_full = os.path.join(epub_dir, *chap_rel.split("/"))
        try:
            with open(chap_full, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        body_match = _BODY_RE.search(text)
        if body_match:
            inner = body_match.group(1)
        else:
            inner = _HEAD_RE.sub("", text)
        chap_dir = posixpath.dirname(chap_rel)
        inner = _rewrite_refs(inner, chap_dir, book_id,
                              chapter_map=chapter_map)
        sections.append(
            '<section id="%s" data-chapter="%s">%s</section>'
            % (chapter_map[chap_rel], html.escape(item_id), inner))
    if not sections:
        raise RuntimeError("解包出的 AZW3 没有可读章节，文件可能已损坏")
    page = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\"><title>%s</title>"
        "<style>%s\n%s</style></head><body>%s</body></html>"
        % (html.escape(title), EPUB_BASE_CSS, "\n".join(css_parts),
           "\n".join(sections))
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)


def _inject_base_meta(text, book_id, res_base):
    """Add viewport + <base> + reading CSS to unpacked MOBI7 HTML."""
    head = (
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\">"
        "<base href=\"/api/azw3/%s/res/%s\">"
        % (book_id, res_base)
    )
    style = (
        "<style>html{font-size:18px}body{font-size:1rem;line-height:1.8;"
        "max-width:720px;margin:0 auto;padding:16px 18px}"
        "img{max-width:100%%;height:auto}</style>"
    )
    match = re.search(r"<head[^>]*>", text, re.IGNORECASE)
    if match:
        pos = match.end()
        return text[:pos] + head + style + text[pos:]
    return head + style + text


def azw3_error_page(message):
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\"><title>打不开</title>"
        "<style>body{font-family:system-ui,-apple-system,"
        "\"PingFang SC\",\"Microsoft YaHei\",sans-serif;max-width:560px;"
        "margin:40px auto;padding:0 20px;color:#262626;line-height:1.7}"
        "h1{font-size:18px}</style></head>"
        "<body data-azw3-error=\"1\"><h1>这本 AZW3 打不开</h1>"
        "<p>%s</p><p>带 DRM 的 Kindle 书需要先解除 DRM 才能读；"
        "也可以换成 EPUB 或 PDF 放进书库。</p></body></html>"
        % html.escape(message)
    )


def ensure_azw3(book):
    """Extract an AZW3 into data/azw3/<id>/ (cached). Returns meta.

    Raises RuntimeError with a user-facing Chinese message when the
    file cannot be read (DRM, corruption, print-replica PDF).
    """
    with _azw3_lock:
        cdir = os.path.join(AZW3_DIR, book["id"])
        meta_path = os.path.join(cdir, "meta.json")
        st = os.stat(book["_path"])
        meta = None
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = None
        if (meta and meta.get("src_mtime") == st.st_mtime
                and meta.get("src_size") == st.st_size
                and os.path.isfile(os.path.join(cdir, "index.html"))):
            return meta
        shutil.rmtree(cdir, ignore_errors=True)
        os.makedirs(cdir, exist_ok=True)
        # Pre-validate: a real MOBI/AZW3 file carries BOOKMOBI at bytes
        # 60-68. mobi.extract() only raises on header corruption; a file
        # with a broken magic would otherwise produce confusing output.
        with open(book["_path"], "rb") as fh:
            header = fh.read(68)
        if len(header) < 68 or header[60:68] != b"BOOKMOBI":
            raise RuntimeError("这不是有效的 AZW3/MOBI 文件")
        import mobi
        tmpdir, main_path = mobi.extract(book["_path"])
        try:
            for item in os.listdir(tmpdir):
                src = os.path.join(tmpdir, item)
                dst = os.path.join(cdir, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            main_rel = os.path.relpath(main_path, tmpdir).replace(
                os.sep, "/")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        meta = {"src_mtime": st.st_mtime, "src_size": st.st_size,
                "title": book["title"]}
        if main_rel.lower().endswith(".epub"):
            epub_src = os.path.join(cdir, *main_rel.split("/"))
            epub_dir = os.path.join(cdir, "epub")
            with zipfile.ZipFile(epub_src) as zf:
                zf.extractall(epub_dir)
            build_epub_html(epub_dir, os.path.join(cdir, "index.html"),
                            book["id"])
            meta["kind"] = "epub"
            meta["res_root"] = "epub"
        elif main_rel.lower().endswith((".html", ".htm", ".xhtml")):
            main_full = os.path.join(cdir, *main_rel.split("/"))
            with open(main_full, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
            base_dir = posixpath.dirname(main_rel)
            res_base = (base_dir + "/") if base_dir else ""
            text = _inject_base_meta(text, book["id"], res_base)
            with open(os.path.join(cdir, "index.html"), "w",
                      encoding="utf-8") as fh:
                fh.write(text)
            meta["kind"] = "html"
            meta["res_root"] = ""
        elif main_rel.lower().endswith(".pdf"):
            shutil.rmtree(cdir, ignore_errors=True)
            raise RuntimeError("这本 AZW3 是扫描版 PDF 格式，暂不支持阅读")
        else:
            shutil.rmtree(cdir, ignore_errors=True)
            raise RuntimeError("无法识别的 AZW3 内容：%s" % main_rel)
        _atomic_write(
            meta_path,
            json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        return meta


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

import tempfile

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_ICON_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")
_BOOK_RE = re.compile(r"^[0-9a-f]{16}$")


def _safe_join(root, *parts):
    """Join parts under root; returns None when the result escapes."""
    path = os.path.join(root, *parts)
    path = os.path.normpath(path)
    if os.path.commonpath([root, path]) != root:
        return None
    return path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence access log
        pass

    # -- helpers ---------------------------------------------------------
    def _read_body(self, limit):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            raise ValueError("请求体过大或为空")
        return self.rfile.read(length)

    def _read_json(self, limit=MAX_JSON_BODY):
        try:
            raw = self._read_body(limit)
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("请求不是有效的 JSON：%s" % exc)
        if not isinstance(data, dict):
            raise ValueError("请求 JSON 必须是一个对象")
        return data

    def _serve_static(self, rel_path, content_type=None):
        path = _safe_join(STATIC_DIR, *rel_path.split("/"))
        if not path or not os.path.isfile(path):
            self.send_error(404, "Not found")
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = content_type or STATIC_TYPES.get(ext,
                                                 "application/octet-stream")
        send_file_range(self, path, ctype)

    # -- routing ---------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if self.command == "POST":
                self._handle_post(path)
            else:
                self._handle_get(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - never crash a request
            sys.stderr.write("manyue: request %s %s failed: %r\n"
                             % (self.command, self.path, exc))
            try:
                send_json(self, {"ok": False, "error": str(exc)},
                          status=500)
            except Exception:  # noqa: BLE001 - client already gone
                pass

    def _handle_get(self, path):
        if path == "/":
            self._serve_static("index.html")
        elif path == "/read":
            self._serve_static("reader.html")
        elif path == "/manifest.json":
            self._serve_static("manifest.webmanifest",
                               "application/manifest+json")
        elif path == "/sw.js":
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Service-Worker-Allowed", "/")
            with open(os.path.join(STATIC_DIR, "sw.js"), "rb") as fh:
                data = fh.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path.startswith("/icons/"):
            name = path[len("/icons/"):]
            if not _ICON_RE.match(name):
                self.send_error(404, "Not found")
                return
            self._serve_static("icons/" + name, "image/png")
        elif path == "/api/version":
            send_json(self, {"version": VERSION, "app": APPNAME})
        elif path == "/api/books":
            books, error = scan_books()
            send_json(self, {
                "books": [public_book(b) for b in books],
                "folder": get_config_copy().get("book_folder") or "",
                "count": len(books),
                "error": error,
            })
        elif path == "/api/progress":
            send_json(self, {"progress": get_progress_copy()["books"]})
        elif path == "/api/settings":
            readable, error = folder_status()
            send_json(self, {
                "book_folder": get_config_copy().get("book_folder") or "",
                "readable": readable,
                "error": error,
            })
        elif path.startswith("/api/file/"):
            book_id = path[len("/api/file/"):]
            self._serve_book_file(book_id)
        elif path.startswith("/api/cover/"):
            book_id = path[len("/api/cover/"):]
            self._serve_cover(book_id)
        elif path.startswith("/api/azw3/"):
            self._serve_azw3(path)
        else:
            self.send_error(404, "Not found")

    def _handle_post(self, path):
        if path == "/api/rescan":
            books, error = scan_books(force=True)
            send_json(self, {"ok": True, "count": len(books),
                             "error": error})
        elif path == "/api/progress":
            data = self._read_json()
            book_id = data.get("book")
            if not isinstance(book_id, str) or not book_id:
                send_json(self, {"ok": False, "error": "缺少 book"},
                          status=400)
                return
            loc = data.get("loc")
            if loc is None or isinstance(loc, (dict, list, bool)):
                send_json(self, {"ok": False, "error": "loc 无效"},
                          status=400)
                return
            total = data.get("total")
            if total is not None and not isinstance(total, (int, float)):
                total = None
            set_progress(book_id, loc, total)
            send_json(self, {"ok": True})
        elif path == "/api/settings":
            data = self._read_json()
            folder = (data.get("book_folder") or "").strip()
            if folder and not folder.startswith("/"):
                send_json(self, {"ok": False,
                                 "error": "请填写绝对路径，如 /vol1/1000/漫画"},
                          status=400)
                return
            if folder and not os.path.isdir(folder):
                send_json(self, {"ok": False,
                                 "error": "文件夹不存在：%s" % folder},
                          status=400)
                return
            with _config_lock:
                _config["book_folder"] = folder
            save_config()
            books, error = scan_books(force=True)
            readable, status_error = folder_status()
            send_json(self, {"ok": True, "readable": readable,
                             "error": status_error or error,
                             "count": len(books)})
        elif path.startswith("/api/cover/"):
            book_id = path[len("/api/cover/"):]
            self._upload_cover(book_id)
        else:
            self.send_error(404, "Not found")

    # -- book file / cover / azw3 ---------------------------------------
    def _serve_book_file(self, book_id):
        if not _BOOK_RE.match(book_id or ""):
            self.send_error(404, "Book not found")
            return
        book = find_book(book_id)
        if not book:
            self.send_error(404, "Book not found")
            return
        send_file_range(self, book["_path"],
                        CONTENT_TYPES.get(book["format"],
                                          "application/octet-stream"))

    def _serve_cover(self, book_id):
        if not _BOOK_RE.match(book_id or ""):
            self.send_error(404, "Book not found")
            return
        book = find_book(book_id)
        if not book:
            self.send_error(404, "Book not found")
            return
        path = cover_path_for(book)
        if not path:
            self.send_error(404, "No cover")
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = ("image/jpeg" if ext == ".jpg" else "image/png")
        send_file_range(self, path, ctype)

    def _upload_cover(self, book_id):
        if not _BOOK_RE.match(book_id or ""):
            send_json(self, {"ok": False, "error": "找不到这本书"},
                      status=404)
            return
        book = find_book(book_id)
        if not book:
            send_json(self, {"ok": False, "error": "找不到这本书"},
                      status=404)
            return
        data = self._read_json(limit=MAX_COVER_BODY)
        image = data.get("image") or ""
        match = re.match(
            r"^data:image/(jpeg|png);base64,([A-Za-z0-9+/=]+)$", image)
        if not match:
            send_json(self, {"ok": False, "error": "封面数据无效"},
                      status=400)
            return
        kind = match.group(1)
        try:
            import base64
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception:  # noqa: BLE001 - bad base64
            send_json(self, {"ok": False, "error": "封面数据无效"},
                      status=400)
            return
        ext = _detect_image_ext(raw)
        if ext is None or (kind == "jpeg") != (ext == ".jpg"):
            send_json(self, {"ok": False, "error": "封面不是图片"},
                      status=400)
            return
        os.makedirs(COVERS_DIR, exist_ok=True)
        for old_ext in (".jpg", ".png"):
            if old_ext != ext:
                try:
                    os.remove(os.path.join(COVERS_DIR,
                                           book_id + old_ext))
                except OSError:
                    pass
        _atomic_write(os.path.join(COVERS_DIR, book_id + ext), raw)
        send_json(self, {"ok": True})

    def _serve_azw3(self, path):
        # /api/azw3/<id>/            -> extracted HTML page
        # /api/azw3/<id>/res/<path>  -> extracted resource
        rest = path[len("/api/azw3/"):]
        parts = [p for p in rest.split("/") if p != ""]
        if not parts or not _BOOK_RE.match(parts[0]):
            self.send_error(404, "Book not found")
            return
        book_id = parts[0]
        book = find_book(book_id)
        if not book or book["format"] != "azw3":
            self.send_error(404, "AZW3 not found")
            return
        if len(parts) >= 2 and parts[1] == "res":
            self._serve_azw3_res(book, parts[2:])
            return
        try:
            meta = ensure_azw3(book)
        except Exception as exc:  # noqa: BLE001 - show readable page
            send_text(self, azw3_error_page(str(exc)))
            return
        index_path = os.path.join(AZW3_DIR, book_id, "index.html")
        send_file_range(self, index_path, "text/html; charset=utf-8")

    def _serve_azw3_res(self, book, rel_parts):
        if not rel_parts:
            self.send_error(404, "Resource not found")
            return
        try:
            meta = ensure_azw3(book)
        except Exception as exc:  # noqa: BLE001 - show readable page
            send_text(self, azw3_error_page(str(exc)))
            return
        root = os.path.join(AZW3_DIR, book["id"], meta.get("res_root", ""))
        decoded = [urllib.parse.unquote(p) for p in rel_parts]
        path = _safe_join(root, *decoded)
        if not path or not os.path.isfile(path):
            self.send_error(404, "Resource not found")
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = RES_TYPES.get(ext, "application/octet-stream")
        send_file_range(self, path, ctype)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(COVERS_DIR, exist_ok=True)
    tmpdir = os.path.join(DATA_DIR, "tmp")
    os.makedirs(tmpdir, exist_ok=True)
    # Keep mobi's temp files on the data volume, not on a small /tmp.
    tempfile.tempdir = tmpdir
    load_config()
    load_progress()
    port = resolve_port()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    cfg = get_config_copy()
    print("manyue %s listening on 0.0.0.0:%d data=%s books=%s"
          % (VERSION, port, DATA_DIR,
             cfg.get("book_folder") or "(未设置)"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
