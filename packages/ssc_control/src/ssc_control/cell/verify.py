"""Whether a lazy resource the deployer says it made is there to use (GA-12 step 0.4).

A run that ends well is the deployer's word, not the cell's. On 2026-10-09 a run ended well
while the cell's agent had never been told about the database, the resource was marked ``ready``,
which is final, and the deploy that had waited for it failed. So before ``create`` writes
``ready`` it asks the cell, through its agent, the way the first use will:

- ``database``: the agent reads a database's usage, one statement run on the instance. An agent
  with no instance, or an instance that does not answer, cannot.
- ``connections``: the agent asks the data gateway for a connection's schema as an environment
  that does not exist. A gateway that is running, admits the agent and has read the access
  snapshot refuses that with a 403. An agent with no gateway address, a gateway that does not
  admit it, or one that cannot answer, gives something else.
- ``egress``: the agent names the proxy's address, which it needs to make a credential. Every
  cell's agent has that address, so this shows only that the agent can be asked. Whether the
  proxy itself answers only the deployer can see: its run does not end well until the proxy
  passes its health check (``infra/README.md``, "The cell deployer").

No probe names a real app or changes anything in the cell. ``unverified`` gives the reason a
resource is not there, short enough for ``ssc.cell_resource.last_error``, or None.
"""

from http import HTTPStatus
from typing import Final

from ssc_contracts.cells import CellResource
from ssc_control.runtime.app_databases import AppDatabaseError
from ssc_control.runtime.cell_datagw import CellSchemaError, SchemaRefusal
from ssc_control.runtime.cell_egress import CellEgressError
from ssc_control.runtime.cells import CellUnavailableError, OrgCell
from ssc_control.worker_ports import Ports
from ssc_shared.runtime import SERVICE_PREFIX, RuntimeDriverError

PROBE_ENVIRONMENT: Final = "env_" + "0" * 20
"""No environment has this id: ids are random, and the probes only read."""
PROBE_SERVICE: Final = SERVICE_PREFIX + "0" * 20
PROBE_CONNECTION: Final = "ssc-probe"


async def unverified(ports: Ports, org_id: str, resource: CellResource) -> str | None:
    """Why ``resource`` is not there to use in ``org_id``'s cell, or None when the cell answered
    as one that has it. A cell that cannot be asked has not answered."""
    if ports.cells is None:
        return "no cell is configured, so nothing could be asked"
    try:
        cell = await ports.cells.for_org(org_id)
        match resource:
            case CellResource.DATABASE:
                return await _database(cell)
            case CellResource.CONNECTIONS:
                return await _connections(cell)
            case CellResource.EGRESS:
                return await _egress(cell)
    except CellUnavailableError:
        return "the org's cell is not configured, so nothing could be asked"
    except RuntimeDriverError as exc:
        return f"the cell agent could not be asked: {exc}"


async def _database(cell: OrgCell) -> str | None:
    if cell.app_databases is None:
        return "no app database port is configured, so the instance could not be asked"
    try:
        await cell.app_databases.usage(PROBE_SERVICE)
    except AppDatabaseError as exc:
        return f"the instance did not answer the agent: {exc}"
    return None


async def _connections(cell: OrgCell) -> str | None:
    if cell.datagw is None:
        return "no data gateway port is configured, so the gateway could not be asked"
    try:
        await cell.datagw.schema(PROBE_CONNECTION, PROBE_ENVIRONMENT)
    except SchemaRefusal as exc:
        if exc.status == HTTPStatus.FORBIDDEN:
            return None
        return f"the data gateway did not answer as one in use: {exc}"
    except CellSchemaError as exc:
        return f"the agent could not ask the data gateway: {exc}"
    return None


async def _egress(cell: OrgCell) -> str | None:
    if cell.egress is None:
        return "no egress port is configured, so the agent could not be asked"
    try:
        info = await cell.egress.info()
    except CellEgressError as exc:
        return f"the agent could not say where the proxy is: {exc}"
    return None if info.proxy_address else "the agent names no proxy address"
