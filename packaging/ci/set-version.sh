#!/usr/bin/env bash
# Patch copyparty/__version__.py to a fork release version X.Y.Z, WITHOUT committing.
#
# copyparty hardcodes its version in this file (there is no setuptools_scm), so CI
# rewrites it transiently right before building the SFX / binary / wheel -- the
# same trick yacron2 uses with SETUPTOOLS_SCM_PRETEND_VERSION. Nothing is committed
# back, so there is no release loop and a re-run rebuilds cleanly.
#
# Usage: packaging/ci/set-version.sh X.Y.Z
set -euo pipefail

ver="${1:?usage: set-version.sh X.Y.Z}"
if ! printf '%s' "$ver" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "error: version must be X.Y.Z (digits only, no leading 'v'): '$ver'" >&2
  exit 1
fi

IFS=. read -r A B C <<<"$ver"
f="copyparty/__version__.py"
# UTC build date, no leading zeros (matches the "(YYYY, M, D)" tuple shape).
today="$(date -u +'%Y, %-m, %-d')"

# Rewrite the two tuples in place. The rest of the file (S_VERSION etc.) derives
# from VERSION, so we only touch these two lines.
sed -i -E \
  -e "s/^VERSION = \(.*/VERSION = ($A, $B, $C)/" \
  -e "s/^BUILD_DT = \(.*/BUILD_DT = ($today)/" \
  "$f"

echo "set version -> $ver"
grep -E '^(VERSION|BUILD_DT) =' "$f"
