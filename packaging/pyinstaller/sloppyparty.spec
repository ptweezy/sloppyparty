# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the sloppyparty portable binary -- one self-contained
# executable with no Python required on the user's machine. Build from the repo
# root (any OS/arch) with:
#
#     pyinstaller packaging/pyinstaller/sloppyparty.spec
#
# copyparty officially supports frozen mode (copyparty/__init__.py sets
# EXE = getattr(sys, "frozen", False)) and resolves its web/res assets from the
# package dir, so bundling copyparty/web + copyparty/res makes the frozen server
# serve the full UI. NOTE: copyparty/web/deps/ (marked, easymde, prism, hls,
# fonts) is NOT committed to git -- run packaging/ci/fetch-webdeps.sh before this
# build to populate it, or the binary ships without the markdown editor / syntax
# highlighting / rich audio player (core file serving still works).
#
# The binary bundles copyparty + Jinja2 (required) plus the PURE-PYTHON optional
# features (FTP/TFTP/audio-tags) so it is capable out of the box while still
# building on every CPU arch. C-extension extras (Pillow thumbnails, argon2,
# pyzmq, paramiko) are intentionally left out: they'd need per-arch wheels and
# would break the exotic QEMU cross-builds. Users who need those use the Docker
# image or `pip install sloppyparty[all]`.

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is injected by PyInstaller = the directory of this .spec file.
ROOT = os.path.dirname(os.path.dirname(SPECPATH))  # packaging/pyinstaller -> repo root

datas = [
    (os.path.join(ROOT, "copyparty", "web"), "copyparty/web"),
    (os.path.join(ROOT, "copyparty", "res"), "copyparty/res"),
]
binaries = []
hiddenimports = collect_submodules("copyparty")

# Jinja2/MarkupSafe are required; the rest are pure-Python optional features that
# copyparty imports lazily, so PyInstaller can't see them without help. collect_all
# is guarded so a missing dep just yields a slightly less capable binary instead of
# failing the build.
for mod in ("jinja2", "markupsafe", "pyftpdlib", "partftpy", "mutagen"):
    try:
        d, b, h = collect_all(mod)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as ex:  # noqa: BLE001  (best-effort optional bundling)
        print("sloppyparty.spec: skipping optional %r (%s)" % (mod, ex))

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim heavy modules copyparty never needs frozen -- keeps the binary small
    # and avoids dragging optional C-extensions into the cross-arch builds.
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "numpy", "PIL", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="sloppyparty",
    debug=False,
    bootloader_ignore_signals=False,
    # strip=False for portability: several cross/QEMU + Windows runners lack a
    # working `strip`, and the size win is small for a pure-Python bundle.
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
