import os
import sys

import pytest

# inside the ADaPT repository: its adapt/ folder would shadow the installed adapt packages
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != REPO_ROOT]


@pytest.fixture
def api():
    """A local fake HTTP API (adapt.core.runtime.testing.FakeApi)."""
    from adapt.core.runtime.testing import FakeApi
    with FakeApi() as server:
        yield server
