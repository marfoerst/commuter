"""Point DATA_DIR at a throwaway temp dir before any app module imports.

app.config creates DATA_DIR at import time (default /app/data, which isn't
writable on a dev box), so this must run before the first app import. pytest
imports conftest first, which makes this the right place.
"""

import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="commuter-test-"))
