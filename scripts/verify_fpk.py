#!/usr/bin/env python3
"""Verify a built kele-reader .fpk against the documented fnOS format.

Checks (each printed PASS/FAIL):
  1. Outer gzip-tar members are package-root entries, no top-level dir.
  2. All required outer members exist.
  3. The manifest's checksum line equals the MD5 of the app.tgz
     member bytes (fnpack's mechanism; no manifest.checksum member).
  4. manifest parses and declares appname / version / platform=<expected>
     / service_port=18090 (expected platform from --platform, default arm).
  5. app.py VERSION matches the manifest version.
  6. Inner app.tgz members are the app/ contents (no app/ prefix) and
     include all app files.
  7. All Python files in app.tgz compile.
  8. All JSON files in the package parse.
  9. Icons are RGBA PNGs (color type 6).
 10. The outer LICENSE names the manifest's display_name / appname /
     version (the installer shows it as the agreement text).
"""

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile

REQUIRED_OUTER = {
    "manifest", "ICON.PNG", "ICON_256.PNG", "LICENSE",
    "app.tgz",
    "cmd/main", "cmd/install_callback", "cmd/uninstall_init",
    "cmd/config_callback", "cmd/upgrade_callback",
    "config/privilege", "config/resource",
    "wizard/install", "wizard/config",
    "wizard/upgrade", "wizard/uninstall",
    "cmd/install_init", "cmd/uninstall_callback", "cmd/upgrade_init",
}

REQUIRED_INNER = {
    "app.py",
    "ui/config", "ui/images/icon_64.png", "ui/images/icon_256.png",
    "static/index.html", "static/reader.html",
    "static/style.css", "static/shelf.js", "static/reader.js",
    "static/pdf.min.js", "static/pdf.worker.min.js",
    "static/epub.min.js", "static/jszip.min.js",
    "static/manifest.webmanifest", "static/sw.js",
    "static/icons/icon-192.png", "static/icons/icon-512.png",
    "vendor/mobi/__init__.py", "vendor/mobi/extract.py",
    "vendor/imghdr.py", "vendor/loguru.py",
}


def check(label, ok, detail=""):
    print("%s %s %s" % ("PASS" if ok else "FAIL", label, detail))
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--platform", default="arm",
                        choices=["arm", "x86"],
                        help="expected manifest platform (default: arm)")
    args = parser.parse_args()
    path = args.path
    platform = args.platform
    all_ok = True
    with open(path, "rb") as fh:
        outer_bytes = fh.read()
    outer = tarfile.open(fileobj=io.BytesIO(outer_bytes), mode="r:gz")
    members = outer.getmembers()
    names = [m.name for m in members]
    all_ok &= check(
        "outer members at package root (no top-level dir)",
        all("/" not in n or not n.startswith("kele")
            for n in names) and not any(
                n.startswith("kele-reader-") for n in names),
        "(%d members)" % len(names),
    )
    all_ok &= check(
        "required outer members present",
        REQUIRED_OUTER.issubset(set(names)),
        "missing: %s" % sorted(REQUIRED_OUTER - set(names)),
    )
    blob = {}
    for m in members:
        if m.isfile():
            blob[m.name] = outer.extractfile(m).read()
    app_tgz = blob.get("app.tgz", b"")
    manifest = {}
    for line in blob.get("manifest", b"").decode("utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            manifest[key.strip()] = value.strip()
    actual = hashlib.md5(app_tgz).hexdigest()
    all_ok &= check(
        "manifest checksum line == md5(app.tgz)",
        manifest.get("checksum") == actual,
        "(%s)" % manifest.get("checksum"),
    )
    all_ok &= check(
        "no stale manifest.checksum member (fnpack does not emit one)",
        "manifest.checksum" not in names,
    )
    all_ok &= check(
        "manifest fields",
        manifest.get("platform") == platform
        and manifest.get("service_port") == "18090"
        and manifest.get("appname") == "kele"
        and manifest.get("source") == "thirdparty",
        str(manifest),
    )
    inner = tarfile.open(fileobj=io.BytesIO(app_tgz), mode="r:gz")
    inner_names = [m.name for m in inner.getmembers()]
    all_ok &= check(
        "inner app.tgz members (app/ contents, no app/ prefix)",
        REQUIRED_INNER.issubset(set(inner_names))
        and not any(n.startswith("app/") for n in inner_names),
        "missing: %s" % sorted(REQUIRED_INNER - set(inner_names)),
    )
    inner_blob = {}
    for m in inner.getmembers():
        if m.isfile():
            inner_blob[m.name] = inner.extractfile(m).read()
    app_py = inner_blob.get("app.py", b"").decode("utf-8", "ignore")
    version_match = re.search(r'^VERSION = "([^"]+)"', app_py, re.M)
    all_ok &= check(
        "app.py VERSION == manifest version",
        version_match is not None
        and version_match.group(1) == manifest.get("version"),
        "(app.py %s, manifest %s)" % (
            version_match.group(1) if version_match else "?",
            manifest.get("version")),
    )
    py_ok = True
    for m in inner.getmembers():
        if m.name.endswith(".py"):
            src = inner.extractfile(m).read()
            try:
                compile(src, m.name, "exec")
            except SyntaxError as exc:
                py_ok = False
                print("  syntax error in %s: %s" % (m.name, exc))
    all_ok &= check("python files in package compile", py_ok)
    json_ok = True
    for name in ("config/privilege", "config/resource",
                 "wizard/install", "wizard/config",
                 "wizard/upgrade", "wizard/uninstall"):
        try:
            json.loads(blob[name].decode("utf-8"))
        except (KeyError, ValueError) as exc:
            json_ok = False
            print("  bad json %s: %s" % (name, exc))
    for m in inner.getmembers():
        if m.name in ("ui/config", "static/manifest.webmanifest"):
            try:
                json.loads(inner.extractfile(m).read().decode("utf-8"))
            except ValueError as exc:
                json_ok = False
                print("  bad json %s: %s" % (m.name, exc))
    all_ok &= check("json files parse", json_ok)

    # Icons must be RGBA PNGs (color type 6): fnOS App Center rejects
    # packages whose ICON files are RGB ("应用包不符合系统要求").
    def _png_color_type(data):
        if len(data) < 30 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return data[25]

    def _png_size(data):
        import struct
        if len(data) < 26 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        w, h = struct.unpack(">II", data[16:24])
        return (w, h)

    icon_ok = True
    for name, data in (("ICON.PNG", blob.get("ICON.PNG")),
                       ("ICON_256.PNG", blob.get("ICON_256.PNG"))):
        if _png_color_type(data or b"") != 6:
            icon_ok = False
            print("  non-RGBA outer icon %s" % name)
    for name in ("ui/images/icon_64.png", "ui/images/icon_256.png",
                 "static/icons/icon-192.png", "static/icons/icon-512.png"):
        if _png_color_type(inner_blob.get(name, b"")) != 6:
            icon_ok = False
            print("  non-RGBA inner icon %s" % name)
    for name, want in (("static/icons/icon-192.png", (192, 192)),
                       ("static/icons/icon-512.png", (512, 512))):
        got = _png_size(inner_blob.get(name, b""))
        if got != want:
            icon_ok = False
            print("  wrong PWA icon size %s: %s != %s" % (name, got, want))
    all_ok &= check("icons are RGBA PNGs (PWA icons exact size)", icon_ok)

    # The installer shows the outer LICENSE as the agreement text, so it
    # must name the current app and version. (1.0.0-1.0.3 shipped a
    # stale "满月阅读 manyue 1.0.0" LICENSE because nothing checked it.)
    license_text = blob.get("LICENSE", b"").decode("utf-8", "ignore")
    all_ok &= check(
        "LICENSE names current app and version",
        manifest.get("display_name") in license_text
        and manifest.get("appname") in license_text
        and manifest.get("version") in license_text,
        "(first line: %s)" % license_text.splitlines()[0]
        if license_text.splitlines() else "(empty)",
    )
    print("RESULT: %s" % ("ALL CHECKS PASSED" if all_ok else "FAILURES"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
