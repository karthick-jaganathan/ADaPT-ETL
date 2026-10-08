import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The repo's streamwright/ folder has no __init__.py, so with the repo root on sys.path
# (e.g. `python -m pytest`) it shadows the installed streamwright.* packages.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != REPO_ROOT]


@pytest.fixture
def api():
    """A local fake HTTP API (streamwright.core.runtime.testing.FakeApi)."""
    from streamwright.core.runtime.testing import FakeApi
    with FakeApi() as server:
        yield server
