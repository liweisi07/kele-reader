#!/bin/sh
# Build with the official fnpack tool (canonical .fpk format).
# fnpack packs everything in the project directory, so build from a clean
# copy: no dist/, no __pycache__. Output is renamed to the versioned name.
#
# Usage: build_fnpack.sh [arm|x86]  (default arm)
# The target platform is written into the temp copy's manifest before
# fnpack runs, and into the output filename (arm -> arm64, x86 -> x86_64).
# The app code is pure Python, so the inner app.tgz is identical for
# every platform; only the manifest platform line differs.
set -e
PLATFORM="${1:-arm}"
case "$PLATFORM" in
    arm) SUFFIX=arm64 ;;
    x86) SUFFIX=x86_64 ;;
    *) echo "unknown platform: $PLATFORM (want arm or x86)" >&2; exit 1 ;;
esac
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP=$(grep '^appname=' "$ROOT/manifest" | cut -d= -f2 | tr -d ' ')
VER=$(grep '^version=' "$ROOT/manifest" | cut -d= -f2 | tr -d ' ')
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT INT TERM
cp -r "$ROOT/app" "$ROOT/cmd" "$ROOT/config" "$ROOT/wizard" "$ROOT/manifest" "$ROOT/ICON.PNG" "$ROOT/ICON_256.PNG" "$ROOT/LICENSE" "$TMP/"
find "$TMP" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
find "$TMP" -name "*.pyc" -delete 2>/dev/null || true
# Override the platform in the temp copy's manifest (repo manifest
# keeps platform=arm as the default).
sed -i -E "s/^platform[[:space:]]*=.*/platform=$PLATFORM/" "$TMP/manifest"
cd "$TMP"
~/workspace/fnpack build -d .
mkdir -p "$ROOT/dist"
cp "$TMP/$APP.fpk" "$ROOT/dist/kele-reader-$VER-$SUFFIX.fpk"
echo "wrote $ROOT/dist/kele-reader-$VER-$SUFFIX.fpk"
