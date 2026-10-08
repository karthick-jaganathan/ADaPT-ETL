import os
import sys

# inside the StreamWright repository: its streamwright/ folder would shadow the installed streamwright packages
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != REPO_ROOT]
