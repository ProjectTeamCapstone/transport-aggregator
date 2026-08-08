import os
import sys

# Ensure the project root and `src` are importable in the serverless runtime.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from src.api import app  # noqa: E402
