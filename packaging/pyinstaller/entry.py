# PyInstaller entry point for the sloppyparty portable binary.
#
# copyparty's real entry is copyparty/__main__.py:main(). PyInstaller wants a
# plain script to freeze, so this just calls it. Kept trivial on purpose -- all
# the bundling logic lives in sloppyparty.spec next to this file.
from copyparty.__main__ import main

if __name__ == "__main__":
    main()
