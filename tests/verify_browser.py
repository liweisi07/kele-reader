#!/usr/bin/env python3
"""Browser verification for kele using a stock headless Chromium.

NOTE: the Chromium in this sandbox (/opt/meta-chromium/chrome) refuses
to start without the Hatch WebGL domain policy (a signed policy file
the runtime's browser service generates per task), so this script
cannot run here as-is. The runnable equivalent in this environment is
tests/verify_browser_jsdom.js (jsdom + node-canvas, same checks).
Use this script on a machine with a normal Chromium binary.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "books")
PORT = 18098
BASE = "http://127.0.0.1:%d" % PORT
DATA_DIR = "/tmp/kele-browser-data"
CHROME = "/opt/meta-chromium/chrome"

CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append((label, ok, detail))
    print("%s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    return ok


def http_json(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def start_server():
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    os.makedirs(DATA_DIR, exist_ok=True)
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
            http_json("/api/version")
            return proc
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("server did not come up")


def chrome_dump(url, budget_ms=25000):
    """Load url in headless Chrome, return (dom, stderr)."""
    cmd = [
        CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--window-size=390,844",
        "--virtual-time-budget=%d" % budget_ms,
        "--enable-logging=stderr", "--v=0",
        "--dump-dom", url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=120)
    return proc.stdout, proc.stderr


def console_errors(stderr):
    """Real JS/network errors from Chrome's stderr log."""
    bad = []
    for line in stderr.splitlines():
        if "Uncaught" in line:
            bad.append(line.strip()[-200:])
        elif "Failed to load resource" in line:
            bad.append(line.strip()[-200:])
        elif "net::ERR_" in line and "favicon" not in line:
            bad.append(line.strip()[-200:])
    # Deduplicate, keep at most 3.
    seen = []
    for item in bad:
        if item not in seen:
            seen.append(item)
    return seen[:3]


def main():
    proc = start_server()
    try:
        books = http_json("/api/books")["books"]
        by_id = {b["id"]: b for b in books}
        big_pdf = [b for b in books
                   if b["title"] == "测试漫画-大文件"][0]
        epub = [b for b in books if b["format"] == "epub"][0]
        txt = [b for b in books
               if b["title"] == "测试文章"][0]
        azw3 = [b for b in books if b["format"] == "azw3"][0]

        # -- shelf -----------------------------------------------------
        dom, err = chrome_dump(BASE + "/")
        check("shelf lists big PDF", "测试漫画-大文件" in dom)
        check("shelf lists EPUB title", "测试小说：满月之下" in dom)
        check("shelf lists TXT", "测试文章" in dom)
        check("shelf lists AZW3", "sample-book" in dom)
        check("shelf lists all 6 books",
              dom.count('class="book"') >= 6,
              "(found %d)" % dom.count('class="book"'))
        errs = console_errors(err)
        check("shelf: no JS/network errors", not errs, str(errs))

        # -- PDF reader (46MB) -----------------------------------------
        dom, err = chrome_dump(
            BASE + "/read?book=" + big_pdf["id"], budget_ms=30000)
        check("PDF: pdf.js loaded 36 pages",
              'data-test-pdf="36"' in dom)
        check("PDF: page 1 rendered to canvas",
              'data-test-pdf-page1="1"' in dom
              and dom.count("<canvas") >= 1,
              "(canvases: %d)" % dom.count("<canvas"))
        check("PDF: position label 第 1 / 36 页",
              "第 1 / 36 页" in dom)
        errs = console_errors(err)
        check("PDF: no JS/network errors", not errs, str(errs))

        # -- EPUB reader ------------------------------------------------
        dom, err = chrome_dump(
            BASE + "/read?book=" + epub["id"], budget_ms=25000)
        check("EPUB: rendition relocated",
              'data-test-epub="1"' in dom)
        check("EPUB: position label set",
              re.search(r"第 \d+ / \d+ 页", dom) is not None)
        errs = console_errors(err)
        check("EPUB: no JS/network errors", not errs, str(errs))

        # -- TXT reader --------------------------------------------------
        dom, err = chrome_dump(
            BASE + "/read?book=" + txt["id"], budget_ms=15000)
        pages = re.search(r'data-test-txt="(\d+)"', dom)
        check("TXT: paginated",
              pages is not None and int(pages.group(1)) > 1,
              "(pages: %s)" % (pages.group(1) if pages else "?"))
        check("TXT: first page text present",
              "这是一个 TXT 测试文件" in dom)
        check("TXT: position label 第 1 /",
              "第 1 / " in dom)
        errs = console_errors(err)
        check("TXT: no JS/network errors", not errs, str(errs))

        # -- AZW3 reader --------------------------------------------------
        dom, err = chrome_dump(
            BASE + "/read?book=" + azw3["id"], budget_ms=25000)
        check("AZW3: iframe loaded extracted HTML",
              'data-test-azw3="1"' in dom)
        check("AZW3: position 已读 0%",
              "已读 0%" in dom)
        errs = console_errors(err)
        check("AZW3: no JS/network errors", not errs, str(errs))
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    failed = [c for c in CHECKS if not c[1]]
    print("RESULT: %d/%d passed" % (len(CHECKS) - len(failed),
                                    len(CHECKS)))
    if failed:
        print("FAILURES: %s" % [c[0] for c in failed])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
