"""The production gate the deploy path runs: approvals, fed by release manifests (SSC-016).

``ApprovalsProdGate`` asks ``ManifestCapabilities`` what a release reaches, and that reads the
manifest stored with the release's bundle (``runtime.specs.release_manifest``). A release with no
stored manifest is ``refused``. The gate's requester is the release's author, or the app's owner
when no person wrote it. Callers run it for ``prod`` only.
"""

from ssc_control.approvals.capabilities import ManifestCapabilities
from ssc_control.approvals.gate import ApprovalsProdGate
from ssc_control.ports import ProdGate
from ssc_control.runtime.specs import release_manifest


def approvals_prod_gate() -> ProdGate:
    return ApprovalsProdGate(ManifestCapabilities(release_manifest))
