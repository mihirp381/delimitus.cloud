"""``/v1``: the API people, the command line, the GitHub Action and the console use.

One module per resource; each owns a prefix-less router that this package mounts under ``/v1``
in a fixed order, so the order of paths in ``openapi.json`` stays stable. Sharing-rule semantics
belong to SSC-021, running a deployment to SSC-016/SSC-017; both keep these shapes.
"""

from fastapi import APIRouter

from ssc_control.api.routes.v1.access import router as access_router
from ssc_control.api.routes.v1.agent_policy import router as agent_policy_router
from ssc_control.api.routes.v1.approvals import router as approvals_router
from ssc_control.api.routes.v1.apps import router as apps_router
from ssc_control.api.routes.v1.audit import router as audit_router
from ssc_control.api.routes.v1.bundles import router as bundles_router
from ssc_control.api.routes.v1.cell import router as cell_router
from ssc_control.api.routes.v1.databases import router as databases_router
from ssc_control.api.routes.v1.deployments import router as deployments_router
from ssc_control.api.routes.v1.grants import router as grants_router
from ssc_control.api.routes.v1.groups import router as groups_router
from ssc_control.api.routes.v1.inventory import router as inventory_router
from ssc_control.api.routes.v1.logins import router as logins_router
from ssc_control.api.routes.v1.logs import router as logs_router
from ssc_control.api.routes.v1.promote import router as promote_router
from ssc_control.api.routes.v1.schedules import router as schedules_router
from ssc_control.api.routes.v1.secrets import router as secrets_router
from ssc_control.api.routes.v1.usage import router as usage_router
from ssc_control.api.routes.v1.users import router as users_router
from ssc_control.api.routes.v1.whoami import router as whoami_router

router = APIRouter(prefix="/v1", tags=["v1"])
router.include_router(whoami_router)
router.include_router(apps_router)
router.include_router(grants_router)
router.include_router(access_router)
router.include_router(deployments_router)
router.include_router(audit_router)
router.include_router(approvals_router)
router.include_router(bundles_router)
router.include_router(users_router)
router.include_router(groups_router)
router.include_router(inventory_router)
router.include_router(schedules_router)
router.include_router(promote_router)
router.include_router(logins_router)
router.include_router(cell_router)
router.include_router(secrets_router)
router.include_router(databases_router)
router.include_router(logs_router)
router.include_router(usage_router)
router.include_router(agent_policy_router)
