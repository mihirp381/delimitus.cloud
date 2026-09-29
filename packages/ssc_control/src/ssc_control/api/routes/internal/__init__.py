"""``/internal/v1``: what cell services and the directory sync call. Workload and operator
credentials only.

One module per resource, each a prefix-less router mounted here in a fixed order: the cell's
heartbeat (which acknowledges access snapshots) and the directory (SSC-021). Deployment progress
arrives with SSC-016.
"""

from fastapi import APIRouter

from ssc_control.api.routes.internal.directory import router as directory_router
from ssc_control.api.routes.internal.heartbeat import router as heartbeat_router

router = APIRouter(prefix="/internal/v1", tags=["internal"])
router.include_router(heartbeat_router)
router.include_router(directory_router)
