#!/usr/bin/env python3
"""End-to-end verification for manyue: start the real server as a
subprocess and exercise every endpoint with real HTTP requests.

Run: python3 tests/verify_e2e.py
Exits 0 only when every check passes.
"""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "books")
PORT = 18099
BASE = "http://127.0.0.1:%d" % PORT
DATA_DIR = "/tmp/manyue-e2e-data"

CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, ok, detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    return ok


def http(path, method="GET", body=None, headers=None, raw=False):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            return resp.status, dict(resp.headers), data
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def http_json(path, method="GET", body=None):
    headers = {}
    raw = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    status, hdrs, data = http(path, method, raw, headers)
    try:
        return status, hdrs, json.loads(data.decode("utf-8"))
    except ValueError:
        return status, hdrs, {"_raw": data[:200]}


def start_server(wipe=True):
    if wipe:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "config.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"book_folder": FIXTURES}, fh)
    env = dict(os.environ)
    env["MANYUE_DATA_DIR"] = DATA_DIR
    env["MANYUE_PORT"] = str(PORT)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app", "app.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            status, _h, _d = http("/api/version")
            if status == 200:
                return proc
        except Exception:
            pass
        time.sleep(0.3)
    out = proc.stdout.read() if proc.stdout else ""
    raise RuntimeError("server did not come up: %s" % out)


def stop_server(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    proc = start_server()
    try:
        run_checks(proc)
    finally:
        stop_server(proc)
    failed = [c for c in CHECKS if not c[1]]
    print("RESULT: %d/%d passed" % (len(CHECKS) - len(failed),
                                    len(CHECKS)))
    if failed:
        print("FAILURES: %s" % [c[0] for c in failed])
        return 1
    return 0


def run_checks(proc):
    # -- pages ---------------------------------------------------------
    status, _h, data = http("/")
    check("GET / bookshelf 200", status == 200
          and "满月阅读".encode("utf-8") in data, "(%d)" % status)
    status, _h, data = http("/read")
    check("GET /read reader 200", status == 200
          and "book-title".encode("utf-8") in data, "(%d)" % status)
    status, _h, data = http("/api/version")
    check("GET /api/version", status == 200
          and json.loads(data).get("version") == "1.0.0",
          "(%d)" % status)

    # -- PWA -----------------------------------------------------------
    status, _h, data = http("/manifest.json")
    manifest = json.loads(data) if status == 200 else {}
    check("GET /manifest.json valid PWA manifest",
          status == 200 and manifest.get("name") == "满月阅读"
          and manifest.get("display") == "standalone"
          and len(manifest.get("icons", [])) == 2, "(%d)" % status)
    status, _h, data = http("/sw.js")
    check("GET /sw.js service worker", status == 200
          and b"manyue-v1" in data, "(%d)" % status)
    for icon in ("icon-192.png", "icon-512.png"):
        status, _h, data = http("/icons/" + icon)
        check("GET /icons/%s" % icon, status == 200
              and data[:8] == b"\x89PNG\r\n\x1a\n", "(%d)" % status)
    for static in ("pdf.min.js", "pdf.worker.min.js", "epub.min.js",
                   "jszip.min.js", "style.css", "shelf.js", "reader.js"):
        status, _h, _d = http("/static/" + static)
        check("GET /static/%s" % static, status == 200, "(%d)" % status)

    # -- library -------------------------------------------------------
    status, _h, data = http_json("/api/books")
    books = data.get("books", [])
    by_fmt = {}
    for book in books:
        by_fmt.setdefault(book["format"], []).append(book)
    check("GET /api/books 6 books, 4 formats",
          status == 200 and len(books) == 6
          and sorted(by_fmt) == ["azw3", "epub", "pdf", "txt"],
          "(%d books: %s)" % (len(books), sorted(by_fmt)))
    big_pdf = [b for b in by_fmt.get("pdf", [])
               if b["title"] == "测试漫画-大文件"][0]
    small_pdf = [b for b in by_fmt.get("pdf", [])
                 if b["title"] == "测试漫画-小"][0]
    epub = by_fmt["epub"][0]
    txt = by_fmt["txt"][0]
    azw3 = by_fmt["azw3"][0]
    check("big PDF >20MB in listing",
          big_pdf["size"] > 20 * 1024 * 1024,
          "(%s)" % big_pdf["size_text"])

    # -- Range serving on the 46MB PDF ---------------------------------
    status, hdrs, data = http(
        "/api/file/" + big_pdf["id"], headers={"Range": "bytes=0-1023"})
    check("Range bytes=0-1023 -> 206 + Content-Range",
          status == 206 and len(data) == 1024
          and hdrs.get("Content-Range", "").startswith("bytes 0-1023/")
          and data[:4] == b"%PDF", "(%d, %s)" % (
              status, hdrs.get("Content-Range")))
    status, hdrs, data = http(
        "/api/file/" + big_pdf["id"],
        headers={"Range": "bytes=20000000-20000999"})
    check("Range past 20MB -> 206, 1000 bytes",
          status == 206 and len(data) == 1000, "(%d)" % status)
    status, hdrs, data = http(
        "/api/file/" + big_pdf["id"],
        headers={"Range": "bytes=%d-" % (big_pdf["size"] + 10)})
    check("Range out of bounds -> 416",
          status == 416, "(%d)" % status)
    status, hdrs, data = http("/api/file/" + big_pdf["id"])
    check("full download of 46MB PDF -> 200, exact size",
          status == 200 and len(data) == big_pdf["size"]
          and data[:4] == b"%PDF",
          "(%d bytes)" % len(data))
    status, hdrs, data = http("/api/file/" + epub["id"])
    check("EPUB download -> 200 epub+zip",
          status == 200
          and hdrs.get("Content-Type") == "application/epub+zip",
          "(%d, %s)" % (status, hdrs.get("Content-Type")))
    status, hdrs, data = http(
        "/api/file/" + txt["id"], headers={"Range": "bytes=0-99"})
    check("TXT Range -> 206",
          status == 206 and len(data) == 100, "(%d)" % status)

    # -- covers --------------------------------------------------------
    status, hdrs, data = http("/api/cover/" + epub["id"])
    check("EPUB embedded cover -> 200 PNG",
          status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n",
          "(%d)" % status)
    status, _h, _d = http("/api/cover/" + small_pdf["id"])
    check("PDF without uploaded cover -> 404",
          status == 404, "(%d)" % status)
    # Upload a cover for the small PDF (1x1 red JPEG from PIL).
    from PIL import Image
    import io as _io
    buf = _io.BytesIO()
    Image.new("RGB", (4, 6), (200, 30, 30)).save(buf, "JPEG")
    jpeg_b64 = base64.b64encode(buf.getvalue()).decode()
    status, _h, data = http_json(
        "/api/cover/" + small_pdf["id"], method="POST",
        body={"image": "data:image/jpeg;base64," + jpeg_b64})
    check("POST cover upload -> ok",
          status == 200 and data.get("ok") is True, "(%s)" % data)
    status, hdrs, data = http("/api/cover/" + small_pdf["id"])
    check("uploaded cover readable back -> 200 JPEG",
          status == 200 and data[:3] == b"\xff\xd8\xff", "(%d)" % status)
    status, _h, data = http_json("/api/books")
    check("cover_url appears after upload",
          any(b["id"] == small_pdf["id"] and b.get("cover_url")
              for b in data.get("books", [])))

    # -- progress + restart persistence --------------------------------
    status, _h, data = http_json(
        "/api/progress", method="POST",
        body={"book": big_pdf["id"], "loc": 12, "total": 36})
    check("POST progress -> ok", status == 200
          and data.get("ok") is True, "(%s)" % data)
    status, _h, data = http_json("/api/progress")
    check("GET progress returns saved loc",
          data.get("progress", {}).get(big_pdf["id"], {}).get("loc") == 12,
          "(%s)" % data)
    status, _h, data = http_json(
        "/api/progress", method="POST",
        body={"book": epub["id"], "loc": "epubcfi(/6/4!/2/2)",
              "total": 3})
    check("POST EPUB CFI progress -> ok", status == 200
          and data.get("ok") is True)
    # Restart the server WITHOUT wiping the data dir: progress must
    # survive (the wipe above only happens on a fresh start).
    stop_server(proc)
    proc2 = start_server(wipe=False)
    try:
        status, _h, data = http_json("/api/progress")
        prog = data.get("progress", {})
        check("progress survives restart (PDF + EPUB)",
              prog.get(big_pdf["id"], {}).get("loc") == 12
              and prog.get(epub["id"], {}).get("loc")
              == "epubcfi(/6/4!/2/2)", "(%s)" % prog)
    finally:
        stop_server(proc2)
    # NOTE: proc was stopped above; restart again (no wipe, so the
    # uploaded cover and progress stay) for remaining checks.
    proc3 = start_server(wipe=False)
    try:
        run_checks_after_restart(proc3, big_pdf, epub, txt, azw3)
    finally:
        stop_server(proc3)


def run_checks_after_restart(proc, big_pdf, epub, txt, azw3):
    # -- settings / rescan ---------------------------------------------
    status, _h, data = http_json("/api/settings")
    check("GET /api/settings folder+readable",
          data.get("readable") is True
          and data.get("book_folder", "").endswith("books"),
          "(%s)" % data)
    status, _h, data = http_json(
        "/api/settings", method="POST",
        body={"book_folder": "/nonexistent/manyue"})
    check("POST settings bad path -> 400, not saved",
          status == 400, "(%d)" % status)
    status, _h, data = http_json("/api/settings")
    check("bad settings did not overwrite folder",
          data.get("book_folder", "").endswith("books"))
    status, _h, data = http_json("/api/rescan", method="POST")
    check("POST /api/rescan -> 6 books",
          status == 200 and data.get("count") == 6, "(%s)" % data)

    # -- AZW3 (real sample) -------------------------------------------
    status, _h, data = http("/api/azw3/" + azw3["id"] + "/")
    text = data.decode("utf-8", "ignore")
    check("AZW3 page -> 200, chapters + res URLs",
          status == 200 and "Choosing the paper" in text
          and "Folding the classic boat" in text
          and ("/api/azw3/%s/res/" % azw3["id"]) in text,
          "(%d, %d bytes)" % (status, len(data)))
    check("AZW3 page has no <script>",
          "<script" not in text.lower())
    status, hdrs, data = http(
        "/api/azw3/%s/res/OEBPS/Images/image00014.png" % azw3["id"])
    check("AZW3 res image -> 200 PNG",
          status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n",
          "(%d)" % status)
    status, _h, _d = http(
        "/api/azw3/%s/res/../../app.py" % azw3["id"])
    check("AZW3 res path traversal blocked",
          status in (400, 404), "(%d)" % status)
    status, _h, _d = http("/api/azw3/%s/res/" % azw3["id"])
    check("AZW3 res empty path -> 404", status == 404, "(%d)" % status)

    # -- misc ----------------------------------------------------------
    status, _h, _d = http("/api/file/0000000000000000")
    check("unknown book id -> 404", status == 404, "(%d)" % status)
    status, _h, _d = http("/no-such-page")
    check("unknown route -> 404", status == 404, "(%d)" % status)
    status, _h, data = http_json(
        "/api/progress", method="POST",
        body={"book": "x", "loc": {"bad": True}})
    check("bad progress loc -> 400", status == 400, "(%d)" % status)


if __name__ == "__main__":
    sys.exit(main())
