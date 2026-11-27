"""Run codegraph-dot from a source checkout, without installing the package.

Usage: python3 scripts/visualise.py report.json | sfdp -Tsvg -o codegraph.svg
Installed, the same is `codegraph-dot report.json`; see `--help` for the options.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from codegraph.dot import main  # noqa: E402

if __name__ == "__main__":
    main()
