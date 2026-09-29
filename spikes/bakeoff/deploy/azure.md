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

## Run notes 2026-09-29 (eastus; runner VM in westus2 because eastus had no VM capacity for any size)

- Step 2: one NSG rule per service tag. A rule listing several tags failed silently. Outbound tags needed for a workload-profiles environment: AzureContainerRegistry, MicrosoftContainerRegistry, AzureFrontDoor.FirstParty, AzureMonitor, Storage, AzureActiveDirectory. The NSG blocked direct egress including UDP.
- Step 3: a private DNS zone for the canary name linked to the VNet returned NXDOMAIN to the apps. A second private zone for the environment domain with a wildcard A record to the environment static IP is needed for the runner to resolve app FQDNs.
- Step 5: `az acr build` is refused on this subscription (`TasksOperationsNotAllowed`); ACR Tasks needs a support request. Images were pushed with local `docker buildx`.
- Step 6: in an internal-only environment, `--ingress external` means reachable from the VNet, and `--ingress internal` means reachable only from inside the environment. The spike used `external`. Revisions stuck in ActivationFailed after NSG changes needed a new revision suffix.
- Step 7: no Application Gateway was needed for the spike; the environment's internal load balancer served HTTPS directly.
- Step 9: peer check fails. Apps in the same environment reach each other through the internal FQDN (HTTP 200). Isolation needs one environment per app or per customer.
- Step 4: the probe only calls IMDS; it does not exercise `IDENTITY_ENDPOINT`. The assigned identity has zero role assignments.
- Step 10: `ingress disable` took 20 s to return and 13.2 s to cut traffic. `az containerapp revision deactivate` cut traffic in 2.0 s.
