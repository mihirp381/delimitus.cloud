import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from edge_world import world  # noqa: E402

__all__ = ["world"]
