#!/usr/bin/env bash
# Patch copyparty/__version__.py to a fork release version X.Y.Z, WITHOUT committing.
#
# copyparty hardcodes its version in this file (there is no setuptools_scm), so CI
# rewrites it transiently right before building the SFX / binary / wheel -- the
# same trick yacron2 uses with SETUPTOOLS_SCM_PRETEND_VERSION. Nothing is committed
# back, so there is no release loop and a re-run rebuilds cleanly.
#
# The edit is done in Python (not `sed -i`) because `sed -i` is not portable:
# GNU sed (Linux, git-bash) and BSD sed (macOS) disagree on the -i syntax, which
# made the macOS binary jobs fail. Python is identical on every runner.
#
# Usage: packaging/ci/set-version.sh X.Y.Z
set -euo pipefail

ver="${1:?usage: set-version.sh X.Y.Z}"
if ! printf '%s' "$ver" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "error: version must be X.Y.Z (digits only, no leading 'v'): '$ver'" >&2
  exit 1
fi

# Windows runners expose `python`, not `python3`; prefer $PYTHON, then python3, then python.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi

"$PY" - "$ver" <<'PY'
import datetime
import re
import sys

ver = sys.argv[1]
a, b, c = ver.split(".")
path = "copyparty/__version__.py"
today = datetime.datetime.now(datetime.timezone.utc)

with open(path, encoding="utf-8") as f:
    src = f.read()

src = re.sub(r"^VERSION = \(.*", "VERSION = (%s, %s, %s)" % (a, b, c), src, count=1, flags=re.M)
src = re.sub(
    r"^BUILD_DT = \(.*",
    "BUILD_DT = (%d, %d, %d)" % (today.year, today.month, today.day),
    src,
    count=1,
    flags=re.M,
)

with open(path, "w", encoding="utf-8") as f:
    f.write(src)

print("set version ->", ver)
PY

grep -E '^(VERSION|BUILD_DT) =' copyparty/__version__.py
