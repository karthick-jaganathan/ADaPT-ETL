import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The repo's adapt/ folder has no __init__.py, so with the repo root on sys.path
# (e.g. `python -m pytest`) it shadows the installed adapt.* packages.
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != REPO_ROOT]


@pytest.fixture
def api():
    """A local fake HTTP API (adapt.core.runtime.testing.FakeApi)."""
    from adapt.core.runtime.testing import FakeApi
    with FakeApi() as server:
        yield server
