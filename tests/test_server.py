#!/usr/bin/env python3
"""Unit tests for manyue app.py (no server needed).

Run: python3 tests/test_server.py
Uses tests/fixtures/books/ as the book folder and a temp data dir.
"""

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "books")

DATA_DIR = tempfile.mkdtemp(prefix="manyue-test-")
os.environ["MANYUE_DATA_DIR"] = DATA_DIR
os.environ["MANYUE_PORT"] = "18099"

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
        self.assertEqual(len(books), 6)
        formats = sorted(b["format"] for b in books)
        self.assertEqual(formats,
                         ["azw3", "epub", "pdf", "pdf", "txt", "txt"])

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
            appmod._config["book_folder"] = "/nonexistent/manyue"
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
        root = "/tmp/manyue-root"
        self.assertIsNone(appmod._safe_join(root, "..", "etc", "passwd"))
        self.assertIsNone(appmod._safe_join(root, "a", "..", "..", "x"))
        self.assertEqual(
            appmod._safe_join(root, "a", "b.png"),
            "/tmp/manyue-root/a/b.png")


class TestAzw3(unittest.TestCase):
    def setUp(self):
        self.books = scan()
        self.azw3 = [b for b in self.books
                     if b["format"] == "azw3"][0]

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
        chapter_map = {"OEBPS/Text/part0001.xhtml": "manyue-ch-ch1"}
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
        tmp = tempfile.mkdtemp(prefix="manyue-epub-")
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
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
