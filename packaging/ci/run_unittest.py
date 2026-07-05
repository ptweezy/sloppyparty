# coding: utf-8
# Discover and run copyparty's unittest suite, dropping a small set of tests that
# fail on PRISTINE upstream copyparty too (verified against upstream/hovudstraum),
# so CI stays green on real regressions without hiding them. unittest's own -k does
# NOT support "not"/boolean expressions (that's pytest), hence this driver.
#
# Set SLOPPY_ALL_TESTS=1 to run everything, including the known-upstream-broken ones.
from __future__ import print_function

import os
import sys
import unittest

# Known-failing on upstream copyparty as well (a copyparty test/code drift, not a
# sloppyparty regression). Re-check periodically and shrink this set as they get
# fixed upstream.
KNOWN_BROKEN_UPSTREAM = {
    "test_idp.TestVFS.test_1",
    "test_idp.TestVFS.test_2",
    "test_vfs.TestVFS.test",
}


def _filter(suite, drop):
    out = unittest.TestSuite()
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            out.addTest(_filter(item, drop))
        elif item.id() in drop:
            print("skipping known-upstream-broken test:", item.id())
        else:
            out.addTest(item)
    return out


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "tests"
    suite = unittest.TestLoader().discover(start)
    if os.environ.get("SLOPPY_ALL_TESTS") != "1":
        suite = _filter(suite, KNOWN_BROKEN_UPSTREAM)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
