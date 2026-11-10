#!/usr/bin/env python3
"""Build kele-reader-<version>-arm64.fpk from this project directory.

Same container format as the cf-bestip apps (verified against fnpack
output): a gzip tar whose members sit at the package root, with an
inner app.tgz (gzip tar of the app/ contents, no app/ prefix), and
the manifest's last line `checksum=<md5 of app.tgz bytes>` (fnpack's
mechanism; there is no separate manifest.checksum member).

Tarballs are built deterministically (mtime/uid/gid zeroed, sorted
member lists, no stray files) so rebuilds are byte-identical.
The canonical build is scripts/build_fnpack.sh (official fnpack tool);
this script is a pure-Python fallback producing the same layout.
"""

import hashlib
import io
import os
import sys
import tarfile

ROOT = os.path.join(os.path.dirname(__file__), "..")
ROOT = os.path.normpath(ROOT)

OUTER_FILES = [
    ("manifest", 0o644),
    ("ICON.PNG", 0o644),
    ("ICON_256.PNG", 0o644),
    ("LICENSE", 0o644),
    ("cmd/", None, 0o755),
    ("cmd/main", 0o755),
    ("cmd/install_callback", 0o755),
    ("cmd/uninstall_init", 0o755),
    ("cmd/config_callback", 0o755),
    ("cmd/upgrade_callback", 0o755),
    ("config/", None, 0o755),
    ("config/privilege", 0o644),
    ("config/resource", 0o644),
    ("wizard/", None, 0o755),
    ("wizard/install", 0o644),
    ("wizard/config", 0o644),
    ("wizard/upgrade", 0o644),
    ("wizard/uninstall", 0o644),
    ("cmd/install_init", 0o755),
    ("cmd/uninstall_callback", 0o755),
    ("cmd/upgrade_init", 0o755),
]


def parse_manifest(path):
    fields = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def build_tar(members):
    """members: list of (arcname, filesystem path or None, mode).

    A None path emits a directory entry (arcname must end with "/").
    USTAR format: fnpack/Go tooling emits USTAR.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz",
                      format=tarfile.USTAR_FORMAT) as tf:
        for arcname, fs_path, mode in members:
            if fs_path is None:
                info = tarfile.TarInfo(name=arcname)
                info.type = tarfile.DIRTYPE
                info.size = 0
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = mode
                tf.addfile(info)
                continue
            with open(fs_path, "rb") as fh:
                data = fh.read()
            info = tarfile.TarInfo(name=arcname)
            info.size = len(data)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def collect_app_members():
    """All files under app/ (sorted, no __pycache__ / *.pyc)."""
    app_root = os.path.join(ROOT, "app")
    members = []
    dirs_seen = set()
    for dirpath, dirnames, filenames in os.walk(app_root):
        dirnames[:] = sorted(
            d for d in dirnames if d != "__pycache__")
        for dirname in sorted(dirnames):
            rel = os.path.relpath(
                os.path.join(dirpath, dirname), app_root) + "/"
            if rel not in dirs_seen:
                dirs_seen.add(rel)
                members.append((rel, None, 0o755))
        for filename in sorted(filenames):
            if filename.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, app_root)
            members.append((rel, full, 0o644))
    members.sort(key=lambda m: m[0])
    return members


def main():
    manifest = parse_manifest(os.path.join(ROOT, "manifest"))
    appname = manifest.get("appname", "kele")
    version = manifest.get("version", "1.0.0")
    print("appname=%s version=%s platform=%s"
          % (appname, version, manifest.get("platform")))

    # 1. Inner app.tgz (contents of app/, no app/ prefix).
    app_members = collect_app_members()
    missing = [m for m in app_members
               if m[1] is not None and not os.path.isfile(m[1])]
    if missing:
        print("missing app files: %s" % missing, file=sys.stderr)
        return 1
    app_tgz = build_tar(app_members)
    print("app.tgz: %d bytes, %d members" % (len(app_tgz), len(app_members)))

    # 2. checksum = MD5 of app.tgz bytes; appended as the manifest's
    # last line (fnpack's mechanism).
    checksum = hashlib.md5(app_tgz).hexdigest()
    stage = os.path.join(ROOT, "dist", "stage")
    os.makedirs(stage, exist_ok=True)
    with open(os.path.join(ROOT, "manifest"), "r",
              encoding="utf-8") as fh:
        manifest_text = fh.read().rstrip("\n") + "\n"
    manifest_text += "checksum=%s\n" % checksum
    staged_manifest = os.path.join(stage, "manifest")
    with open(staged_manifest, "w", encoding="utf-8") as fh:
        fh.write(manifest_text)
    with open(os.path.join(stage, "app.tgz"), "wb") as fh:
        fh.write(app_tgz)

    def stage_path(name):
        if name == "manifest":
            return staged_manifest
        if name == "app.tgz":
            return os.path.join(stage, "app.tgz")
        return os.path.join(ROOT, name)

    outer_members = []
    for entry in OUTER_FILES:
        rel, mode = entry[0], entry[-1]
        if rel.endswith("/"):
            outer_members.append((rel, None, mode))
            continue
        fs_path = stage_path(rel)
        if not os.path.isfile(fs_path):
            print("missing outer file: %s" % fs_path, file=sys.stderr)
            return 1
        outer_members.append((rel, fs_path, mode))
    outer_members.append(("app.tgz", os.path.join(stage, "app.tgz"), 0o644))

    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out_name = "kele-reader-%s-arm64.fpk" % version
    out_path = os.path.join(out_dir, out_name)
    fpk_bytes = build_tar(outer_members)
    with open(out_path, "wb") as fh:
        fh.write(fpk_bytes)
    print("wrote %s (%d bytes)" % (out_path, len(fpk_bytes)))
    print("manifest.checksum: %s" % checksum)
    return 0


if __name__ == "__main__":
    sys.exit(main())
