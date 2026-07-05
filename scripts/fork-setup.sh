#!/usr/bin/env bash
# Run ONCE after cloning sloppyparty.
#
# Registers the custom "ours" merge driver referenced by .gitattributes.
# .git/config is NOT included in a clone, so this must be run in every working
# copy. Without it, `merge=ours` paths silently fall back to a normal 3-way
# merge and will CONFLICT on upstream pulls (with no warning that the driver
# was missing).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# Pull in the tracked driver definition via a relative include.
# include.path is resolved relative to .git/config (which lives in .git/),
# so one ../ points at the repo root. (../../ would be wrong.)
git config --local include.path ../.gitconfig-fork

if [ "$(git config --get merge.ours.driver)" = "true" ]; then
  echo "sloppyparty: merge driver 'ours' registered."
else
  echo "sloppyparty: FAILED to register merge driver 'ours'." >&2
  exit 1
fi
# (merge=union and the *.gif binary rule are git built-ins; no registration needed.)
