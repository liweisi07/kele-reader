#!/usr/bin/env python3
"""End-to-end verification for kele: start the real server as a
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
DATA_DIR = "/tmp/kele-e2e-data"

CHECKS = []

COOKIE = None  # "kele_session=<token>"，当前阶段登录后的会话


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def check(label, ok, detail=""):
    CHECKS.append((label, ok, detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    return ok


def http(path, method="GET", body=None, headers=None, raw=False,
         base=BASE, no_redirect=False):
    hdrs = dict(headers or {})
    if COOKIE:
        hdrs["Cookie"] = COOKIE
    req = urllib.request.Request(
        base + path, method=method,
        data=body, headers=hdrs)
    opener = _NO_REDIRECT_OPENER if no_redirect \
        else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=60) as resp:
            data = resp.read()
            return resp.status, dict(resp.headers), data
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def login(base=BASE, username="admin", password="admin"):
    """登录并把会话 cookie 存进全局 COOKIE；返回 (status, data)。"""
    global COOKIE
    status, hdrs, data = http_json(
        "/api/login", method="POST",
        body={"username": username, "password": password}, base=base)
    set_cookie = hdrs.get("Set-Cookie") or ""
    token = None
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("kele_session="):
            token = part[len("kele_session="):]
            break
    if status == 200 and token:
        COOKIE = "kele_session=" + token
    return status, data


def http_json(path, method="GET", body=None, base=BASE):
    headers = {}
    raw = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    status, hdrs, data = http(path, method, raw, headers, base=base)
    try:
        return status, hdrs, json.loads(data.decode("utf-8"))
    except ValueError:
        return status, hdrs, {"_raw": data[:200]}


def start_server(wipe=True):
    if wipe:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    # 重启（wipe=False）时保留已有 config（含账号哈希），只在全新
    # 启动时写默认配置；否则重启会把改过的密码重置回默认。
    if wipe or not os.path.isfile(os.path.join(DATA_DIR, "config.json")):
        with open(os.path.join(DATA_DIR, "config.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"book_folder": FIXTURES}, fh)
    env = dict(os.environ)
    env["KELE_DATA_DIR"] = DATA_DIR
    env["KELE_PORT"] = str(PORT)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app", "app.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            # /api/version 要登录；用公开的 /login 判断服务已起。
            status, _h, _d = http("/login")
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
    run_large_epub_phase()
    failed = [c for c in CHECKS if not c[1]]
    print("RESULT: %d/%d passed" % (len(CHECKS) - len(failed),
                                    len(CHECKS)))
    if failed:
        print("FAILURES: %s" % [c[0] for c in failed])
        return 1
    return 0


def run_checks(proc):
    # -- auth ----------------------------------------------------------
    # 未登录：页面 302 到登录页，接口 401；登录页本身可达。
    status, _h, data = http("/login")
    check("GET /login 200 + 登录表单", status == 200
          and "login-btn".encode("utf-8") in data, "(%d)" % status)
    status, hdrs, _d = http("/", no_redirect=True)
    check("unauthenticated GET / -> 302 to /login",
          status == 302
          and (hdrs.get("Location") or "").startswith("/login"),
          "(%d %s)" % (status, hdrs.get("Location")))
    status, hdrs, _d = http("/read", no_redirect=True)
    check("unauthenticated GET /read -> 302 to /login",
          status == 302
          and (hdrs.get("Location") or "").startswith("/login"),
          "(%d)" % status)
    status, _h, _d = http_json("/api/books")
    check("unauthenticated GET /api/books -> 401",
          status == 401, "(%d)" % status)
    status, _h, _d = http_json("/api/version")
    check("unauthenticated GET /api/version -> 401",
          status == 401, "(%d)" % status)
    status, _h, data = http_json(
        "/api/login", method="POST",
        body={"username": "admin", "password": "wrong"})
    check("wrong password -> 401", status == 401, "(%d)" % status)
    status, data = login()
    check("login admin/admin -> 200 + cookie",
          status == 200 and data.get("ok") is True
          and COOKIE is not None, "(%d)" % status)
    # 登录后 /login 应跳回书架（已登录不必再登录）。
    status, hdrs, _d = http("/login", no_redirect=True)
    check("authed GET /login -> 302 to /",
          status == 302 and hdrs.get("Location") == "/",
          "(%d %s)" % (status, hdrs.get("Location")))

    # -- pages ---------------------------------------------------------
    status, _h, data = http("/")
    check("GET / bookshelf 200", status == 200
          and "可乐阅读".encode("utf-8") in data, "(%d)" % status)
    status, _h, data = http("/read")
    check("GET /read reader 200", status == 200
          and "book-title".encode("utf-8") in data, "(%d)" % status)
    # 1.0.3: reader has prev/next chapter buttons (same folder, title order)
    check("GET /read has chapter nav buttons", status == 200
          and b"prev-chapter-btn" in data
          and b"next-chapter-btn" in data
          and b"chapter-label" in data, "(%d)" % status)
    status, _h, data = http("/api/version")
    # 版本号从仓库 manifest 读，不写死，否则每次升版都要改测试。
    expected_version = ""
    try:
        with open(os.path.join(ROOT, "manifest"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("version="):
                    expected_version = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    check("GET /api/version", status == 200
          and json.loads(data).get("version") == expected_version,
          "(%d, want %s)" % (status, expected_version))

    # -- PWA -----------------------------------------------------------
    status, _h, data = http("/manifest.json")
    manifest = json.loads(data) if status == 200 else {}
    check("GET /manifest.json valid PWA manifest",
          status == 200 and manifest.get("name") == "可乐阅读"
          and manifest.get("display") == "standalone"
          and len(manifest.get("icons", [])) == 2, "(%d)" % status)
    status, _h, data = http("/sw.js")
    check("GET /sw.js service worker", status == 200
          and b"kele-v1" in data, "(%d)" % status)
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
    check("GET /api/books 8 books, 5 formats",
          status == 200 and len(books) == 8
          and sorted(by_fmt) == ["azw3", "epub", "mobi", "pdf", "txt"],
          "(%d books: %s)" % (len(books), sorted(by_fmt)))
    big_pdf = [b for b in by_fmt.get("pdf", [])
               if b["title"] == "测试漫画-大文件"][0]
    small_pdf = [b for b in by_fmt.get("pdf", [])
                 if b["title"] == "测试漫画-小"][0]
    epub = by_fmt["epub"][0]
    txt = by_fmt["txt"][0]
    azw3 = by_fmt["azw3"][0]
    mobi = by_fmt["mobi"][0]
    check("MOBI listed with format mobi",
          mobi["title"] == "sample-book-mobi"
          and mobi["format"] == "mobi",
          "(%s)" % mobi)
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
        run_checks_after_restart(proc3, big_pdf, epub, txt, azw3, mobi)
    finally:
        stop_server(proc3)


def run_checks_after_restart(proc, big_pdf, epub, txt, azw3, mobi):
    # -- settings / rescan ---------------------------------------------
    status, _h, data = http_json("/api/settings")
    check("GET /api/settings folder+readable",
          data.get("readable") is True
          and data.get("book_folder", "").endswith("books"),
          "(%s)" % data)
    status, _h, data = http_json(
        "/api/settings", method="POST",
        body={"book_folder": "/nonexistent/kele"})
    check("POST settings bad path -> 400, not saved",
          status == 400, "(%d)" % status)
    status, _h, data = http_json("/api/settings")
    check("bad settings did not overwrite folder",
          data.get("book_folder", "").endswith("books"))
    status, _h, data = http_json("/api/rescan", method="POST")
    check("POST /api/rescan -> 8 books",
          status == 200 and data.get("count") == 8, "(%s)" % data)

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

    # -- MOBI (1.0.5): same extraction pipeline as AZW3 ---------------
    status, _h, data = http("/api/azw3/" + mobi["id"] + "/")
    text = data.decode("utf-8", "ignore")
    check("MOBI page -> 200, chapters + res URLs",
          status == 200 and "Choosing the paper" in text
          and "Folding the classic boat" in text
          and ("/api/azw3/%s/res/" % mobi["id"]) in text,
          "(%d, %d bytes)" % (status, len(data)))
    check("MOBI page has no <script>",
          "<script" not in text.lower())

    # -- EPUB (server-side extraction, 1.0.1) -------------------------
    status, _h, data = http("/api/epub/" + epub["id"] + "/")
    text = data.decode("utf-8", "ignore")
    check("EPUB page -> 200, chapters + res URLs",
          status == 200 and "第一章" in text
          and "第二章" in text
          and ("/api/epub/%s/res/" % epub["id"]) in text
          and "/api/azw3/" not in text,
          "(%d, %d bytes)" % (status, len(data)))
    check("EPUB page has no <script>",
          "<script" not in text.lower())
    check("EPUB page images lazy-load",
          'loading="lazy"' in text)
    status, hdrs, data = http(
        "/api/epub/%s/res/OEBPS/Images/cover.png" % epub["id"])
    check("EPUB res image -> 200 PNG",
          status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n",
          "(%d)" % status)
    status, _h, _d = http(
        "/api/epub/%s/res/../../app.py" % epub["id"])
    check("EPUB res path traversal blocked",
          status in (400, 404), "(%d)" % status)
    status, _h, _d = http("/api/epub/%s/res/" % epub["id"])
    check("EPUB res empty path -> 404", status == 404, "(%d)" % status)
    status, _h, data = http_json(
        "/api/progress", method="POST",
        body={"book": epub["id"], "loc": 0.5, "total": 1})
    check("POST EPUB fraction progress -> ok", status == 200
          and data.get("ok") is True, "(%s)" % data)
    status, _h, data = http_json("/api/progress")
    check("GET EPUB fraction progress returns 0.5",
          data.get("progress", {}).get(epub["id"], {}).get("loc") == 0.5,
          "(%s)" % data)

    # -- misc ----------------------------------------------------------
    status, _h, _d = http("/api/file/0000000000000000")
    check("unknown book id -> 404", status == 404, "(%d)" % status)
    status, _h, _d = http("/no-such-page")
    check("unknown route -> 404", status == 404, "(%d)" % status)
    status, _h, data = http_json(
        "/api/progress", method="POST",
        body={"book": "x", "loc": {"bad": True}})
    check("bad progress loc -> 400", status == 400, "(%d)" % status)
    run_auth_flow_checks(big_pdf)


def run_auth_flow_checks(big_pdf):
    """改密码 + 退出登录：旧密码失效、新密码能登、书库与进度不丢、
    退出后需重新登录；最后把密码改回默认 admin/admin。"""
    global COOKIE
    # 改之前先记下书库与进度。
    status, _h, data = http_json("/api/settings")
    folder_before = data.get("book_folder")
    status, _h, data = http_json("/api/progress")
    pdf_loc_before = data.get("progress", {}).get(
        big_pdf["id"], {}).get("loc")
    check("auth flow: baseline progress present",
          pdf_loc_before == 12, "(%s)" % pdf_loc_before)
    # 当前密码不对，拒绝。
    status, _h, data = http_json(
        "/api/change-password", method="POST",
        body={"current_password": "wrong",
              "new_username": "", "new_password": "x"})
    check("change password with wrong current -> 401",
          status == 401, "(%d)" % status)
    # 改密码（账号不变）。
    status, _h, data = http_json(
        "/api/change-password", method="POST",
        body={"current_password": "admin",
              "new_username": "", "new_password": "newpass123"})
    check("change password -> 200", status == 200
          and data.get("ok") is True, "(%d)" % status)
    # 旧密码失效，新密码能登。
    status, _h, _d = http_json(
        "/api/login", method="POST",
        body={"username": "admin", "password": "admin"})
    check("old password dead after change -> 401",
          status == 401, "(%d)" % status)
    status, data = login(password="newpass123")
    check("new password works -> 200", status == 200
          and data.get("ok") is True, "(%d)" % status)
    # 书库配置与进度没丢。
    status, _h, data = http_json("/api/settings")
    check("book_folder preserved after password change",
          data.get("book_folder") == folder_before,
          "(%s)" % data.get("book_folder"))
    status, _h, data = http_json("/api/progress")
    check("progress preserved after password change",
          data.get("progress", {}).get(big_pdf["id"], {}).get("loc")
          == pdf_loc_before, "(%s)" % data)
    # 退出登录：当前会话失效，未登录调 API 被挡。
    old_cookie = COOKIE
    status, hdrs, data = http_json("/api/logout", method="POST")
    check("logout -> 200 + clears cookie", status == 200
          and data.get("ok") is True
          and "Max-Age=0" in (hdrs.get("Set-Cookie") or ""),
          "(%d)" % status)
    COOKIE = old_cookie  # 故意用已失效的会话再试一次
    status, _h, _d = http_json("/api/books")
    check("after logout API -> 401", status == 401, "(%d)" % status)
    status, hdrs, _d = http("/", no_redirect=True)
    check("after logout page -> 302 to /login",
          status == 302
          and (hdrs.get("Location") or "").startswith("/login"),
          "(%d)" % status)
    # 恢复：用新密码登录，把密码改回默认 admin/admin。
    status, data = login(password="newpass123")
    check("re-login with new password -> 200", status == 200
          and data.get("ok") is True, "(%d)" % status)
    status, _h, data = http_json(
        "/api/change-password", method="POST",
        body={"current_password": "newpass123",
              "new_username": "", "new_password": "admin"})
    check("change password back to admin -> 200", status == 200
          and data.get("ok") is True, "(%d)" % status)
    status, data = login()
    check("login admin/admin restored -> 200", status == 200
          and data.get("ok") is True, "(%d)" % status)


LARGE_PORT = 18097
LARGE_BASE = "http://127.0.0.1:%d" % LARGE_PORT
LARGE_DATA = "/tmp/kele-e2e-large-data"
LARGE_BOOKS = "/tmp/kele-e2e-large-books"


def make_large_epub(path, chapters=60):
    """Build a large image-heavy EPUB (~40-60MB) for the speed test.

    Each chapter has a big noise JPEG (manga-like, incompressible),
    so the file is genuinely large. The extracted HTML stays small
    because images are lazy-loaded. Generated into a temp dir, not
    committed to the repo.
    """
    from PIL import Image, ImageDraw
    import zipfile

    tmpdir = os.path.join(os.path.dirname(path), ".tmplargeepub")
    os.makedirs(os.path.join(tmpdir, "META-INF"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "OEBPS", "Text"), exist_ok=True)
    os.makedirs(os.path.join(tmpdir, "OEBPS", "Images"), exist_ok=True)

    def write(rel, text):
        full = os.path.join(tmpdir, rel)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    write("mimetype", "application/epub+zip")
    write("META-INF/container.xml", """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""")
    manifest_items = []
    spine_items = []
    for i in range(chapters):
        img_path = os.path.join(tmpdir, "OEBPS", "Images",
                                "p%03d.jpg" % i)
        img = Image.effect_noise((900, 1300), 100 + i).convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.rectangle([60, 60, 840, 200], fill=(255, 255, 255))
        draw.text((80, 100), "BIG EPUB CHAPTER %d" % (i + 1),
                  fill=(0, 0, 0))
        img.save(img_path, "JPEG", quality=85)
        write("OEBPS/Text/ch%03d.xhtml" % i, """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第 %d 话</title></head>
<body><h1>第 %d 话 大开本</h1><p>这是大 EPUB 的第 %d 章，用来验证服务端解包速度。</p>
<img src="../Images/p%03d.jpg" alt="第 %d 话"/></body></html>
""" % (i + 1, i + 1, i + 1, i, i + 1))
        manifest_items.append(
            '<item id="ch%d" href="Text/ch%03d.xhtml" '
            'media-type="application/xhtml+xml"/>' % (i, i))
        manifest_items.append(
            '<item id="img%d" href="Images/p%03d.jpg" '
            'media-type="image/jpeg"/>' % (i, i))
        spine_items.append('<itemref idref="ch%d"/>' % i)
    write("OEBPS/content.opf", """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookID" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>大开本测试（共%d话）</dc:title>
    <dc:creator>测试</dc:creator>
    <dc:language>zh-CN</dc:language>
    <dc:identifier id="BookID">urn:uuid:kele-test-large-epub</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    %s
  </manifest>
  <spine toc="ncx">
    %s
  </spine>
</package>
""" % (chapters, "\n    ".join(manifest_items),
       "\n    ".join(spine_items)))
    write("OEBPS/toc.ncx", """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:kele-test-large-epub"/></head>
  <docTitle><text>大开本测试</text></docTitle>
  <navMap></navMap>
</ncx>
""")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(tmpdir, "mimetype"), "mimetype",
                 compress_type=zipfile.ZIP_STORED)
        for dirpath, _dirs, files in os.walk(tmpdir):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, tmpdir)
                if rel == "mimetype":
                    continue
                zf.write(full, rel)
    shutil.rmtree(tmpdir, ignore_errors=True)
    size = os.path.getsize(path)
    print("wrote %s (%d bytes, %d chapters)" % (path, size, chapters))
    return size


def run_large_epub_phase():
    """Separate server with a single large EPUB: verify server-side
    extraction opens it fast (first extract + cached), and the browser
    only fetches a small HTML page, not the whole file."""
    books_dir = os.path.join(LARGE_BOOKS, "大书")
    os.makedirs(books_dir, exist_ok=True)
    epub_path = os.path.join(books_dir, "大开本测试.epub")
    if not os.path.isfile(epub_path):
        make_large_epub(epub_path, chapters=60)
    size = os.path.getsize(epub_path)
    check("large EPUB fixture is >40MB", size > 40 * 1024 * 1024,
          "(%d bytes)" % size)
    shutil.rmtree(LARGE_DATA, ignore_errors=True)
    os.makedirs(LARGE_DATA, exist_ok=True)
    with open(os.path.join(LARGE_DATA, "config.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"book_folder": LARGE_BOOKS}, fh)
    env = dict(os.environ)
    env["KELE_DATA_DIR"] = LARGE_DATA
    env["KELE_PORT"] = str(LARGE_PORT)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app", "app.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        deadline = time.time() + 15
        up = False
        while time.time() < deadline:
            try:
                # /api/version 要登录；用公开的 /login 判断服务已起。
                status, _h, _d = http("/login", base=LARGE_BASE)
                if status == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)
        check("large-EPUB server came up", up)
        if not up:
            return
        status, data = login(base=LARGE_BASE)
        check("large-EPUB server login admin/admin",
              status == 200 and data.get("ok") is True,
              "(%d)" % status)
        if status != 200:
            return
        status, _h, data = http_json("/api/books", base=LARGE_BASE)
        books = data.get("books", [])
        check("large EPUB listed (1 book, folder 大书)",
              len(books) == 1 and books[0]["format"] == "epub"
              and books[0]["folder"] == "大书",
              "(%s)" % books)
        if len(books) != 1:
            return
        book_id = books[0]["id"]
        # First open: triggers server-side extraction (unzip + build).
        t0 = time.time()
        status, _h, data = http("/api/epub/" + book_id + "/",
                                base=LARGE_BASE)
        t_first = time.time() - t0
        text = data.decode("utf-8", "ignore")
        check("large EPUB first open -> 200, fast",
              status == 200 and t_first < 30,
              "(%.2fs, %d bytes)" % (t_first, len(data)))
        check("large EPUB first open has chapters + res URLs",
              "第 1 话" in text and "第 60 话" in text
              and ("/api/epub/%s/res/" % book_id) in text)
        check("large EPUB page is small (not the whole file)",
              len(data) < 1024 * 1024,
              "(%d bytes vs %d byte file)" % (len(data), size))
        # Cached open: must be much faster.
        t0 = time.time()
        status, _h, data = http("/api/epub/" + book_id + "/",
                                base=LARGE_BASE)
        t_cached = time.time() - t0
        check("large EPUB cached open -> 200, <2s",
              status == 200 and t_cached < 2,
              "(%.3fs)" % t_cached)
        status, _hdrs, data = http(
            "/api/epub/%s/res/OEBPS/Images/p000.jpg" % book_id,
            base=LARGE_BASE)
        check("large EPUB res image -> 200 JPEG",
              status == 200 and data[:3] == b"\xff\xd8\xff",
              "(%d)" % status)
    finally:
        stop_server(proc)
        shutil.rmtree(LARGE_BOOKS, ignore_errors=True)
        shutil.rmtree(LARGE_DATA, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
