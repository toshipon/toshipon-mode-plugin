"""Entry point for `python -I`. Isolated mode keeps the working directory, the PYTHON*
variables and the user site-packages out of sys.path, so files in the user's repository are
never imported as code. Only this runtime directory is added.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sre_harness.cli import main  # noqa: E402

sys.exit(main())
