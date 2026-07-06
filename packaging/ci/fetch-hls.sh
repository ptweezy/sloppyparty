#!/usr/bin/env bash
# Ensure <dest>/hls.light.js.gz exists -- the fork's on-the-fly video transcoder
# (hls.js). It is NOT part of upstream copyparty webdeps, so the upstream-sourced
# fetch-webdeps.sh / make-sfx "dl-wd" paths cannot supply it. We fetch hls.js from
# npm (same registry and pinned version as scripts/deps-docker/Dockerfile) and
# gzip it, producing exactly what the Docker webdeps build would (the server
# serves foo.js.gz transparently for a request to foo.js).
#
# Best-effort by design: on any download/extract failure it warns and exits 0
# (core file serving is unaffected; only in-browser transcoding needs this).
#
# Usage: fetch-hls.sh [dest-dir]   (default dest: copyparty/web/deps)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
dest="${1:-copyparty/web/deps}"
out="$dest/hls.light.js.gz"

if [ -e "$out" ]; then
  echo "hls.light.js.gz already present; skipping hls fetch"
  exit 0
fi

# keep the version in lockstep with the Docker webdeps build (single source of truth)
ver="$(grep -oE 'ver_hlsjs=[0-9.]+' "$here/../../scripts/deps-docker/Dockerfile" 2>/dev/null | head -n1 | cut -d= -f2 || true)"
[ -n "${ver:-}" ] || ver=1.6.16

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 \
    -o "$tmp/hls.tgz" "https://registry.npmjs.org/hls.js/-/hls.js-${ver}.tgz"; then
  echo "::warning::could not download hls.js ${ver}; build will lack the video transcoder" >&2
  exit 0
fi

if tar -C "$tmp" --strip-components=1 -xzf "$tmp/hls.tgz" package/dist/hls.light.min.js 2>/dev/null \
    && [ -s "$tmp/dist/hls.light.min.js" ]; then
  mkdir -p "$dest"
  gzip -9 -c "$tmp/dist/hls.light.min.js" > "$out"
  echo "fetched hls.light.js.gz (hls.js ${ver}, $(wc -c < "$out" | tr -d ' ') bytes)"
else
  echo "::warning::hls.js ${ver} tarball missing dist/hls.light.min.js; build will lack the video transcoder" >&2
fi
