import os
import sys

import pytest

# inside the StreamWright repository: its streamwright/ folder would shadow the installed streamwright packages
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != REPO_ROOT]


@pytest.fixture
def api():
    """A local fake HTTP API (streamwright.core.runtime.testing.FakeApi)."""
    from streamwright.core.runtime.testing import FakeApi
    with FakeApi() as server:
        yield server
