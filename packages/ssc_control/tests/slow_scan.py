"""A stand-in for ``ssc_control.deploy.bundles._inspect_path`` that outlives the scan budget.

It lives outside the test modules so the scan's worker process can import it by name (pytest's
``pythonpath`` setting puts this folder on ``sys.path``, which the worker inherits).
"""

import time


def sleeping_inspect(path: str, limits: object) -> None:
    del path, limits
    time.sleep(60)
