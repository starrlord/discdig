"""Entry point for the packaged Windows build.

PyInstaller executes its entry script as a top-level module, so the relative
``from .cli import main`` in ``discdig/__main__.py`` has no parent package to
resolve against and fails at startup.  This launcher uses an absolute import
instead; it is not used when running from source.
"""

import sys

from discdig.cli import main

if __name__ == "__main__":
    sys.exit(main())
