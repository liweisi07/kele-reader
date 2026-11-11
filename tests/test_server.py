#!/usr/bin/env python3
"""Unit tests for kele app.py (no server needed).

Run: python3 tests/test_server.py
Uses tests/fixtures/books/ as the book folder and a temp data dir.
"""

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "books")

DATA_DIR = tempfile.mkdtemp(prefix="kele-test-")
os.environ["KELE_DATA_DIR"] = DATA_DIR
os.environ["KELE_PORT"] = "18099"

sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "app", "vendor"))

import app as appmod  # noqa: E402


class FakeHandler:
    """Minimal stand-in for BaseHTTPRequestHandler (Range tests)."""

    def __init__(self, headers=None, command="GET"):
        self.headers = headers or {}
        self.command = command
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}
        self._error = None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def send_error(self, code, message=None):
        self._error = (code, message)
        self.status = code


def scan():
    with appmod._config_lock:
        appmod._config["book_folder"] = FIXTURES
    books, error = appmod.scan_books(force=True)
    assert error == "", error
    return books


class TestScan(unittest.TestCase):
    def test_finds_all_books(self):
        books = scan()
        self.assertEqual(len(books), 8)
        formats = sorted(b["format"] for b in books)
        self.assertEqual(formats,
                         ["azw3", "epub", "mobi", "pdf", "pdf",
                          "txt", "txt", "txt"])

    def test_mobi_listed(self):
        books = scan()
        mobi = [b for b in books if b["format"] == "mobi"]
        self.assertEqual(len(mobi), 1)
        self.assertEqual(mobi[0]["title"], "sample-book-mobi")
        self.assertEqual(mobi[0]["folder"], "小说")

    def test_subfolders(self):
        books = scan()
        folders = sorted({b["folder"] for b in books})
        self.assertEqual(folders, ["小说", "漫画"])

    def test_epub_metadata_from_opf(self):
        books = scan()
        epub = [b for b in books if b["format"] == "epub"][0]
        self.assertEqual(epub["title"], "测试小说：满月之下")
        self.assertEqual(epub["author"], "测试作者")

    def test_ids_stable(self):
        first = {b["id"]: b["rel_path"] for b in scan()}
        second = {b["id"]: b["rel_path"] for b in scan()}
        self.assertEqual(first, second)

    def test_big_pdf_over_20mb(self):
        books = scan()
        big = [b for b in books
               if b["title"] == "测试漫画-大文件"][0]
        self.assertGreater(big["size"], 20 * 1024 * 1024)

    def test_missing_folder_error(self):
        with appmod._config_lock:
            appmod._config["book_folder"] = "/nonexistent/kele"
        books, error = appmod.scan_books(force=True)
        self.assertEqual(books, [])
        self.assertIn("不存在", error)

    def test_folder_status(self):
        with appmod._config_lock:
            appmod._config["book_folder"] = FIXTURES
        readable, error = appmod.folder_status()
        self.assertTrue(readable)
        self.assertEqual(error, "")


class TestRange(unittest.TestCase):
    def setUp(self):
        self.books = scan()
        self.big = [b for b in self.books
                    if b["title"] == "测试漫画-大文件"][0]

    def _serve(self, range_hdr):
        handler = FakeHandler(
            {"Range": range_hdr} if range_hdr else {})
        appmod.send_file_range(handler, self.big["_path"],
                               "application/pdf")
        return handler

    def test_full_200(self):
        handler = self._serve(None)
        self.assertEqual(handler.status, 200)
        self.assertEqual(int(handler.sent_headers["Content-Length"]),
                         self.big["size"])
        self.assertEqual(handler.sent_headers["Accept-Ranges"], "bytes")
        self.assertEqual(len(handler.wfile.getvalue()), self.big["size"])

    def test_first_kb_206(self):
        handler = self._serve("bytes=0-1023")
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.sent_headers["Content-Range"],
                         "bytes 0-1023/%d" % self.big["size"])
        self.assertEqual(len(handler.wfile.getvalue()), 1024)
        self.assertEqual(handler.wfile.getvalue()[:4], b"%PDF")

    def test_open_ended_range_past_20mb(self):
        handler = self._serve("bytes=20000000-")
        self.assertEqual(handler.status, 206)
        self.assertEqual(
            handler.sent_headers["Content-Range"],
            "bytes 20000000-%d/%d"
            % (self.big["size"] - 1, self.big["size"]))
        self.assertEqual(len(handler.wfile.getvalue()),
                         self.big["size"] - 20000000)

    def test_suffix_range(self):
        handler = self._serve("bytes=-100")
        self.assertEqual(handler.status, 206)
        self.assertEqual(len(handler.wfile.getvalue()), 100)

    def test_out_of_range_416(self):
        handler = self._serve("bytes=%d-" % (self.big["size"] + 100))
        self.assertEqual(handler.status, 416)
        self.assertEqual(handler.sent_headers["Content-Range"],
                         "bytes */%d" % self.big["size"])

    def test_overlong_end_clamps_to_eof(self):
        # RFC 7233: bytes=0-<past EOF> clamps to the last byte (206),
        # it is NOT a 416. The TXT reader asks for 0-204799 on a
        # 169,994-byte file; a 416 here made TXT unreadable.
        txt = [b for b in self.books if b["title"] == "测试文章"][0]
        handler = FakeHandler({"Range": "bytes=0-204799"})
        appmod.send_file_range(handler, txt["_path"], "text/plain")
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.sent_headers["Content-Range"],
                         "bytes 0-%d/%d" % (txt["size"] - 1, txt["size"]))
        self.assertEqual(len(handler.wfile.getvalue()), txt["size"])

    def test_reversed_range_416(self):
        handler = self._serve("bytes=100-50")
        self.assertEqual(handler.status, 416)

    def test_head_no_body(self):
        handler = FakeHandler({}, command="HEAD")
        appmod.send_file_range(handler, self.big["_path"],
                               "application/pdf")
        self.assertEqual(handler.status, 200)
        self.assertEqual(len(handler.wfile.getvalue()), 0)


class TestProgress(unittest.TestCase):
    def test_round_trip_and_persistence(self):
        appmod.set_progress("abc123", 7, 36)
        appmod.set_progress("def456", "epubcfi(/6/2!)", 12)
        got = appmod.get_progress_copy()["books"]
        self.assertEqual(got["abc123"]["loc"], 7)
        self.assertEqual(got["abc123"]["total"], 36)
        self.assertEqual(got["def456"]["loc"], "epubcfi(/6/2!)")
        # Reloaded from disk by a fresh load.
        with open(appmod.PROGRESS_PATH, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        self.assertEqual(disk["books"]["abc123"]["loc"], 7)


class TestCovers(unittest.TestCase):
    def test_epub_embedded_cover(self):
        books = scan()
        epub = [b for b in books if b["format"] == "epub"][0]
        path = appmod.extract_epub_cover(epub)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(8), b"\x89PNG\r\n\x1a\n")

    def test_detect_image_ext(self):
        self.assertEqual(appmod._detect_image_ext(b"\xff\xd8\xffxxx"),
                         ".jpg")
        self.assertEqual(
            appmod._detect_image_ext(b"\x89PNG\r\n\x1a\nxxx"), ".png")
        self.assertIsNone(appmod._detect_image_ext(b"hello"))


class TestSafeJoin(unittest.TestCase):
    def test_escape_rejected(self):
        root = "/tmp/kele-root"
        self.assertIsNone(appmod._safe_join(root, "..", "etc", "passwd"))
        self.assertIsNone(appmod._safe_join(root, "a", "..", "..", "x"))
        self.assertEqual(
            appmod._safe_join(root, "a", "b.png"),
            "/tmp/kele-root/a/b.png")


class TestAzw3(unittest.TestCase):
    def setUp(self):
        self.books = scan()
        self.azw3 = [b for b in self.books
                     if b["format"] == "azw3"][0]
        self.mobi = [b for b in self.books
                     if b["format"] == "mobi"][0]

    def test_extract_real_sample(self):
        meta = appmod.ensure_azw3(self.azw3)
        self.assertEqual(meta["kind"], "epub")
        self.assertEqual(meta["res_root"], "epub")
        index = os.path.join(appmod.AZW3_DIR, self.azw3["id"],
                             "index.html")
        self.assertTrue(os.path.isfile(index))
        with open(index, "r", encoding="utf-8") as fh:
            text = fh.read()
        # All three chapters present, in spine order.
        self.assertIn("Choosing the paper", text)
        self.assertIn("Folding the classic boat", text)
        self.assertIn("Launching and floating", text)
        self.assertLess(text.index("Choosing the paper"),
                        text.index("Folding the classic boat"))
        # Image rewritten to a res/ URL.
        self.assertIn("/api/azw3/%s/res/OEBPS/Images/image00014.png"
                      % self.azw3["id"], text)
        # Internal chapter link became an in-page anchor, not a res URL.
        self.assertNotIn("res/OEBPS/Text/part0001.xhtml", text)
        self.assertIn('href="#ch1"', text)

    def test_extract_mobi_sample(self):
        # MOBI 走同一条解包管线（mobi.extract + EPUB/HTML 分支）。
        meta = appmod.ensure_azw3(self.mobi)
        self.assertEqual(meta["kind"], "epub")
        index = os.path.join(appmod.AZW3_DIR, self.mobi["id"],
                             "index.html")
        self.assertTrue(os.path.isfile(index))
        with open(index, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Choosing the paper", text)
        self.assertIn("Folding the classic boat", text)
        self.assertIn("/api/azw3/%s/res/" % self.mobi["id"], text)

    def test_extract_is_cached(self):
        first = appmod.ensure_azw3(self.azw3)
        index = os.path.join(appmod.AZW3_DIR, self.azw3["id"],
                             "index.html")
        mtime = os.path.getmtime(index)
        second = appmod.ensure_azw3(self.azw3)
        self.assertEqual(first, second)
        self.assertEqual(mtime, os.path.getmtime(index))

    def test_bad_magic_rejected(self):
        bad = os.path.join(DATA_DIR, "bad.azw3")
        with open(bad, "wb") as fh:
            fh.write(b"X" * 200)
        book = dict(self.azw3)
        book["_path"] = bad
        book["id"] = "badbadbadbadbad1"
        with self.assertRaises(RuntimeError) as ctx:
            appmod.ensure_azw3(book)
        self.assertIn("不是有效的 AZW3", str(ctx.exception))

    def test_rewrite_refs(self):
        chapter_map = {"OEBPS/Text/part0001.xhtml": "kele-ch-ch1"}
        out = appmod._rewrite_refs(
            '<img src="../Images/a.png">'
            '<a href="part0001.xhtml#ch1">next</a>'
            '<a href="https://example.com/x">ext</a>',
            "OEBPS/Text", "deadbeef00000001",
            chapter_map=chapter_map)
        self.assertIn(
            'src="/api/azw3/deadbeef00000001/res/OEBPS/Images/a.png"',
            out)
        self.assertIn('href="#ch1"', out)
        self.assertIn('href="https://example.com/x"', out)


class TestEpubHtml(unittest.TestCase):
    def test_build_from_unzipped_epub(self):
        import zipfile
        books = scan()
        epub = [b for b in books if b["format"] == "epub"][0]
        tmp = tempfile.mkdtemp(prefix="kele-epub-")
        try:
            with zipfile.ZipFile(epub["_path"]) as zf:
                zf.extractall(tmp)
            out = os.path.join(tmp, "out.html")
            appmod.build_epub_html(tmp, out, "deadbeef00000002")
            with open(out, "r", encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("第一章 月升", text)
            self.assertIn("第二章 月落", text)
            self.assertIn(
                "/api/azw3/deadbeef00000002/res/OEBPS/Images/cover.png",
                text)
            # No scripts survive, no external stylesheet links.
            self.assertNotIn("<script", text.lower())
            self.assertNotIn("<link", text.lower())
            # Images lazy-load so a big book does not fetch them all.
            self.assertIn('loading="lazy"', text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_with_epub_prefix(self):
        import zipfile
        books = scan()
        epub = [b for b in books if b["format"] == "epub"][0]
        tmp = tempfile.mkdtemp(prefix="kele-epub-")
        try:
            with zipfile.ZipFile(epub["_path"]) as zf:
                zf.extractall(tmp)
            out = os.path.join(tmp, "out.html")
            appmod.build_epub_html(tmp, out, "deadbeef00000003",
                                   url_prefix="/api/epub")
            with open(out, "r", encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn(
                "/api/epub/deadbeef00000003/res/OEBPS/Images/cover.png",
                text)
            self.assertNotIn("/api/azw3/", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_manga_images_fit_width(self):
        # Issue 2: manga EPUBs whose own CSS fixes image sizes (e.g.
        # width:1200px / height:100vh) used to clip pages to a partial
        # view. The base CSS must come AFTER the EPUB's own CSS and
        # force images to fit width with height:auto.
        import zipfile
        books = scan()
        epub = [b for b in books if b["format"] == "epub"][0]
        tmp = tempfile.mkdtemp(prefix="kele-epub-")
        try:
            with zipfile.ZipFile(epub["_path"]) as zf:
                zf.extractall(tmp)
            out = os.path.join(tmp, "out.html")
            appmod.build_epub_html(tmp, out, "deadbeef00000004",
                                   url_prefix="/api/epub")
            with open(out, "r", encoding="utf-8") as fh:
                text = fh.read()
            # EPUB's own CSS (font-size:18px) must precede the base CSS.
            idx_epub = text.find("font-size:18px")
            idx_base = text.find("max-width:100%!important")
            self.assertGreater(idx_epub, 0)
            self.assertGreater(idx_base, idx_epub)
            self.assertIn("height:auto!important", text)
            self.assertIn("display:block", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEpub(unittest.TestCase):
    def setUp(self):
        self.books = scan()
        self.epub = [b for b in self.books
                     if b["format"] == "epub"][0]

    def test_extract_real_epub(self):
        meta = appmod.ensure_epub(self.epub)
        self.assertEqual(meta["kind"], "epub")
        self.assertEqual(meta["res_root"], "epub")
        index = os.path.join(appmod.EPUB_DIR, self.epub["id"],
                             "index.html")
        self.assertTrue(os.path.isfile(index))
        with open(index, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("第一章 月升", text)
        self.assertIn("第二章 月落", text)
        # Resources point at the EPUB endpoint, not AZW3.
        self.assertIn("/api/epub/%s/res/OEBPS/Images/cover.png"
                      % self.epub["id"], text)
        self.assertNotIn("/api/azw3/", text)
        self.assertIn('loading="lazy"', text)

    def test_extract_is_cached(self):
        first = appmod.ensure_epub(self.epub)
        index = os.path.join(appmod.EPUB_DIR, self.epub["id"],
                             "index.html")
        mtime = os.path.getmtime(index)
        second = appmod.ensure_epub(self.epub)
        self.assertEqual(first, second)
        self.assertEqual(mtime, os.path.getmtime(index))

    def test_bad_file_rejected(self):
        bad = os.path.join(DATA_DIR, "bad.epub")
        with open(bad, "wb") as fh:
            fh.write(b"X" * 200)
        book = dict(self.epub)
        book["_path"] = bad
        book["id"] = "badbadbadbadbad3"
        with self.assertRaises(RuntimeError) as ctx:
            appmod.ensure_epub(book)
        self.assertIn("打不开", str(ctx.exception))

    def test_not_an_epub_rejected(self):
        # A valid zip that is not an EPUB (no META-INF/container.xml).
        bad = os.path.join(DATA_DIR, "notepub.epub")
        import zipfile
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("hello.txt", "hello")
        book = dict(self.epub)
        book["_path"] = bad
        book["id"] = "badbadbadbadbad4"
        with self.assertRaises(RuntimeError) as ctx:
            appmod.ensure_epub(book)
        self.assertIn("不是有效的 EPUB", str(ctx.exception))


class TestAuth(unittest.TestCase):
    def setUp(self):
        import copy
        self._saved_config = copy.deepcopy(appmod._config)
        self._saved_sessions = copy.deepcopy(appmod._sessions)
        # 从干净状态开始：config 里没有 auth（像 load_config 没跑过），
        # get_auth() 应回落到内存态默认 admin/admin。
        with appmod._config_lock:
            appmod._config.pop("auth", None)
        with appmod._sessions_lock:
            appmod._sessions.clear()

    def tearDown(self):
        import copy
        with appmod._config_lock:
            appmod._config.clear()
            appmod._config.update(copy.deepcopy(self._saved_config))
        with appmod._sessions_lock:
            appmod._sessions.clear()
            appmod._sessions.update(copy.deepcopy(self._saved_sessions))

    def test_default_admin_login(self):
        self.assertTrue(appmod.verify_login("admin", "admin"))

    def test_wrong_password_rejected(self):
        self.assertFalse(appmod.verify_login("admin", "wrong"))
        self.assertFalse(appmod.verify_login("other", "admin"))
        self.assertFalse(appmod.verify_login("admin", ""))
        self.assertFalse(appmod.verify_login("", "admin"))
        self.assertFalse(appmod.verify_login("admin", 123))

    def test_password_hash_is_salted(self):
        a1 = appmod._new_auth("admin", "secret")
        a2 = appmod._new_auth("admin", "secret")
        # 同一密码，两次盐不同、哈希不同；不存明文。
        self.assertNotEqual(a1["salt"], a2["salt"])
        self.assertNotEqual(a1["password_hash"], a2["password_hash"])
        self.assertNotIn("secret", json.dumps(a1))
        self.assertEqual(len(a1["password_hash"]), 64)  # sha256 hex
        self.assertEqual(len(a1["salt"]), 32)  # 16 字节 hex
        # 不是无盐 sha256（证明加了盐）。
        self.assertNotEqual(
            a1["password_hash"],
            hashlib.sha256(b"secret").hexdigest())

    def test_session_create_validate_destroy(self):
        token = appmod.create_session("admin")
        self.assertTrue(token)
        self.assertEqual(appmod.validate_session(token), "admin")
        self.assertIsNone(appmod.validate_session("bogus"))
        self.assertIsNone(appmod.validate_session(""))
        appmod.destroy_session(token)
        self.assertIsNone(appmod.validate_session(token))

    def test_session_expires(self):
        token = appmod.create_session("admin")
        with appmod._sessions_lock:
            appmod._sessions[token]["expires"] = int(time.time()) - 1
        self.assertIsNone(appmod.validate_session(token))
        # 过期会话被清掉。
        with appmod._sessions_lock:
            self.assertNotIn(token, appmod._sessions)

    def test_destroy_other_sessions_keeps_current(self):
        t1 = appmod.create_session("admin")
        t2 = appmod.create_session("admin")
        appmod.destroy_other_sessions(t1)
        self.assertEqual(appmod.validate_session(t1), "admin")
        self.assertIsNone(appmod.validate_session(t2))

    def test_load_config_seeds_default_auth(self):
        # 模拟从旧版升级：config 只有 book_folder，没有 auth。
        with open(appmod.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump({"book_folder": FIXTURES}, fh)
        with appmod._config_lock:
            appmod._config.clear()
            appmod._config["book_folder"] = ""
        appmod.load_config()
        auth = appmod._config.get("auth")
        self.assertTrue(appmod._valid_auth(auth))
        self.assertEqual(auth["username"], "admin")
        self.assertTrue(appmod.verify_login("admin", "admin"))
        self.assertEqual(appmod._config.get("book_folder"), FIXTURES)
        # 已存盘，且是加盐哈希。
        with open(appmod.CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertTrue(appmod._valid_auth(saved.get("auth")))
        self.assertNotEqual(
            saved["auth"]["password_hash"],
            hashlib.sha256(b"admin").hexdigest())

    def test_change_password_preserves_config_and_progress(self):
        # 改密码只动 auth 段：书库与进度必须原样保留。
        with appmod._config_lock:
            appmod._config["book_folder"] = FIXTURES
        appmod.set_progress("f9be17429ddccce8", 12, 36)
        appmod.set_auth("admin", "newpass123")
        self.assertTrue(appmod.verify_login("admin", "newpass123"))
        self.assertFalse(appmod.verify_login("admin", "admin"))
        self.assertEqual(appmod._config.get("book_folder"), FIXTURES)
        with open(appmod.PROGRESS_PATH, encoding="utf-8") as fh:
            prog = json.load(fh)
        self.assertEqual(prog["books"]["f9be17429ddccce8"]["loc"], 12)
        with open(appmod.CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved.get("book_folder"), FIXTURES)
        self.assertNotIn("newpass123", json.dumps(saved))


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
