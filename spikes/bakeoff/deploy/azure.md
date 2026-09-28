# Azure candidate: Container Apps

Throwaway subscription. Install `azure-cli` first (`brew install azure-cli`). Items marked "Unknown / verify" were not confirmed against current Azure docs.

1. Resource group, VNet with a delegated subnet for the Container Apps environment (workload profiles environment, internal-only: `--internal-only true` on `az containerapp env create`, verify flag).
2. Egress: NAT gateway with a static public IP on the subnet (fixed outbound IP row); NSG on the subnet with an outbound Deny-All rule above the defaults. Unknown / verify: whether Container Apps on workload profiles honours subnet NSG outbound rules for all app traffic (documented as supported on workload profiles; confirm and note the URL). Also enable a user-defined route to a firewall if the NSG alone does not block QUIC/UDP.
3. DNS: Azure DNS Private Resolver with an outbound ruleset, or a private DNS zone for the canary zone that returns no records (DNS exfil row). Unknown / verify: whether Container Apps use the VNet DNS settings.
4. Identity: no managed identity assigned to the apps (`--system-assigned` off). The probe's Azure IMDS call should return non-200. Unknown / verify: Container Apps expose identity tokens at `IDENTITY_ENDPOINT`, not only IMDS; also curl `$IDENTITY_ENDPOINT` from inside the container and record.
5. Images: Azure Container Registry, build with ACR Tasks (`az acr build`) — managed build service row.
6. Deploy: `az containerapp create --ingress internal --target-port 8080 --min-replicas 0 --max-replicas 2 --transport auto` per app. Isolation row: Unknown / verify which sandbox Container Apps use between apps in one environment; consider one environment per customer cell.
7. Front door: Application Gateway (internal frontend, private IP) or Azure Front Door Premium with Private Link to the internal environment. Run the runner from a VM in the VNet.
8. Public ingress row: with an internal-only environment the app FQDN resolves only inside the VNet; from a laptop expect NXDOMAIN or timeout. Record.
9. Peer row: the second app's internal FQDN. Unknown / verify: apps in the same environment can reach each other by default; if so this row fails unless the environment is per app or a network policy exists.
10. Kill commands for `kill_timer.py`: `az containerapp ingress disable -n probe-api -g cell`; `az containerapp update -n probe-api -g cell --min-replicas 0 --max-replicas 0` (verify that max 0 is accepted); `az containerapp delete -n probe-api -g cell --yes`.
11. Managed Postgres row: Azure Database for PostgreSQL Flexible Server 18, zone-redundant HA, PITR default, customer-managed key. Log store row: Log Analytics query API (KQL). Secret store row: Key Vault; org-level deny via Azure Policy or a management-group RBAC deny assignment (verify that deny assignments can be authored directly; they are normally only created by Blueprints/managed apps — Unknown / verify).
12. Cost row: Cost Management after 24 h idle plus the price list.
13. Tear down: delete the resource group.
