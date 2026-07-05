#!/usr/bin/env bash
# Populate copyparty/web/deps/ with the vendored web libraries (marked, easymde,
# prism, hls, fonts, ...) that copyparty does NOT commit to git -- it fetches them
# at build time. We extract them from a reference copyparty SFX, which is the
# canonical source of these brand-neutral JS/CSS libraries (exactly what
# scripts/make-sfx.sh does when web/deps is absent).
#
# Run this before a PyInstaller build so the markdown editor / syntax highlighting
# / rich audio player work in the frozen binary. Best-effort by design: if the
# download or extraction fails, the binary still builds -- just without those
# optional web assets (core file serving is unaffected).
#
# Usage: packaging/ci/fetch-webdeps.sh [reference-sfx-url]
set -euo pipefail

url="${1:-https://github.com/9001/copyparty/releases/latest/download/copyparty-sfx.py}"
py="${PYTHON:-python3}"
dest="copyparty/web/deps"

# Skip if deps already look populated (more than the couple of committed stubs).
if [ "$(find "$dest" -type f 2>/dev/null | wc -l)" -gt 6 ]; then
  echo "web/deps already populated; skipping fetch"
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$tmp/ref-sfx.py" "$url"; then
  echo "::warning::could not download reference SFX ($url); binary will lack optional web assets" >&2
  exit 0
fi

# Running the SFX self-extracts to a tempdir and prints "sfxdir: <path>".
sfxdir="$("$py" "$tmp/ref-sfx.py" --version 2>&1 | awk '/sfxdir:/{sub(/.*: /,"");print;exit}')" || true

if [ -n "${sfxdir:-}" ] && [ -d "$sfxdir/copyparty/web/deps" ]; then
  mkdir -p "$dest"
  cp -pR "$sfxdir/copyparty/web/deps/." "$dest/"
  echo "populated $dest ($(find "$dest" -type f | wc -l) files) from reference SFX"
else
  echo "::warning::reference SFX did not expose web/deps; binary will lack optional web assets" >&2
fi
