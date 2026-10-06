import os
import sys

# inside the ADaPT repository: its adapt/ folder would shadow the installed adapt packages
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != REPO_ROOT]
