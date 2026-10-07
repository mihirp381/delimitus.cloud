# SSC-101 second test customer on cell 2

Places a second customer org, "Test Customer 2", on cell 2 (`proofcell02`, project `ssc-c-proofcell02`), so two orgs on two cells can be checked against each other: placement (decision 030), source custody (decision 015), and sign-in to the console and remote MCP (decision 029). It is also the org SSC-101's T1 cost, proof-run step 8 and T12 need.

Steps marked **[founder]** need the founder's dashboards or browser. Steps marked **[real]** change a real resource. Parts 1 and 2 can run at any time. Part 3 waits until the mvp-merge release is live (after T9, `docs/runbooks/ssc-064-control-plane.md` steps 8 and 9 with the new control image).

## Cost

- WorkOS production bills SSO and Directory Sync per connection (check your WorkOS plan; at list price each is a monthly charge). This adds one of each. Delete both after the tests if the org is not kept.
- Okta's developer org is free.
- Cell 2 already runs; an org on it adds only what its apps use.

## 1. Identity provider and WorkOS **[founder]**

Use the same Okta developer org as the first customer, with a new email domain, so the two orgs never share a domain. WorkOS finds an org from the email's domain (decision 029), and a domain belongs to one WorkOS organisation.

1. Pick the domain: `test2.delimitus.com` (a subdomain of a zone the platform stack owns, so the verification record can go there). No mail is needed: Okta sets the passwords.
2. WorkOS **production** dashboard, Organizations, Create: name `Test Customer 2`, domain `test2.delimitus.com`. WorkOS shows a TXT record. Add it to the `delimitus` zone in `ssc-platform-0` (or send it to the operator, who adds it with `gcloud dns record-sets create … --zone delimitus --type TXT`), then verify it in WorkOS. If WorkOS refuses a subdomain, register a cheap domain instead and use it everywhere below.
3. In that organisation, add an **SSO connection** of type Okta SAML. Create the SAML app in Okta from the values WorkOS shows (ACS URL, SP entity ID), and give WorkOS the app's metadata URL, the same way as for the first customer.
4. Add a **directory** of type Okta SCIM. Turn on provisioning in Okta with the endpoint and bearer token WorkOS shows.
5. In Okta, create a group `test-customer-2` and three people in it, each with an admin-set password and email `@test2.delimitus.com`:
   - `admin2@…`: Test Customer 2's founder and admin;
   - `builder2@…`: a member who will be made a builder;
   - `member2@…`: a plain member.
   Assign the group to the SAML app and to the provisioning app, and push the group.
6. Wait for WorkOS's directory to list the three people (Directory, Users).

## 2. Send the operator **[founder]**

No passwords, only these:

- the WorkOS organisation id (`org_01…`);
- the directory id (`directory_01…`);
- the SSO connection id (`conn_01…`);
- `admin2`'s `idp_id` as WorkOS shows it (Directory, Users, the user), and the email.

## 3. Create the org and serve cell 2 **[real]**

After the mvp-merge release is live (control, then cell 1). The operator runs these with the settings of `ssc-064-control-plane.md` step 10.2 (database through the proxy as `ssc_app`, the WorkOS key read with `read -rs`).

1. Check the founder in the directory (prints `idp_id` and email per user):

   ```sh
   curl -s -H "Authorization: Bearer $SSC_WORKOS_API_KEY" \
     "https://api.workos.com/directory_users?directory=<directory_01…>&limit=100" \
     | jq -r '.data[] | [.idp_id, .email] | @tsv'
   ```

2. Create the org on cell 2:

   ```sh
   ORG2=$(uv run python -m ssc_control.identity create-org --name "Test Customer 2" \
     --founder-name "Admin Two" --founder-email admin2@test2.delimitus.com --founder-idp-id "<idp_id>" \
     --operator op_<your name> --workos-org <org_01…> --directory <directory_01…> --sso <conn_01…> \
     --join-rule idp_id --cell-label proofcell02)
   echo $ORG2
   ```

   Pass: the org id, and `ok` for the founder on the error stream.

3. Bind cell 2 to it and roll out the new agent there (the placeholder org id goes away):

   ```sh
   cd infra && export PULUMI_BACKEND_URL=gs://ssc-platform-0-pulumi
   pulumi config set --stack c-proofcell02 org_id $ORG2
   pulumi config set --stack c-proofcell02 agent_image <the mvp-merge agent image by digest>
   pulumi preview --stack c-proofcell02 --diff
   pulumi up --stack c-proofcell02
   ```

   Pass: the preview changes the agent's image and `SSC_ORG_ID`, the gateway's org, and adds `bucket-control-api-bundles`; nothing is replaced.

4. Serve both cells from the control plane. `cells` replaces the old `cell_label` and `cell_jwks` pair (both at once are refused):

   ```sh
   J1=$(pulumi stack output --stack c-proofcell01 identity_jwks)
   J2=$(pulumi stack output --stack c-proofcell02 identity_jwks)
   pulumi config set --stack platform cells "$(jq -cn --arg a "$J1" --arg b "$J2" \
     '[{label:"proofcell01",jwks:$a},{label:"proofcell02",jwks:$b}]')"
   pulumi config rm --stack platform cell_label
   pulumi config rm --stack platform cell_jwks
   pulumi preview --stack platform --diff
   pulumi up --stack platform
   ```

   Pass: the preview changes `SSC_CELLS` on the API and the worker and writes `keys.delimitus.com/proofcell02/jwks.json`; cell 1's entry in `SSC_CELLS` is byte for byte what it was, so no app gets a new revision.

## 4. Work sign-in **[founder]**

### a. The console, as yourself (first customer)

1. Open `https://console.delimitus.com` in your usual browser.
2. Click **Continue with your work account**.
3. Type your work email. If this browser is already signed in to `auth.delimitus.com`, this step is skipped.
4. Sign in at Okta.
5. Pass: the console opens on your org's apps. Open Connections, Internet access, and one app (Repository, Promote, and an environment's Data connections and Timers).
6. Sign out. Pass: back on the sign-in page.

### b. Remote MCP from Claude Code, as yourself

1. In a terminal: `claude mcp add --transport http ssc https://api.delimitus.com/mcp`
2. Start `claude`, type `/mcp`, pick `ssc`, choose **Authenticate**. A browser opens.
3. Type your work email, sign in at Okta, and on the consent page check it names Claude Code and your org, then **Approve**.
4. Back in Claude Code, ask: "List my SSC apps and the data connections I can use."
5. Pass: it answers with your apps (tool `list_apps`) and connections (`list_connections`).

`ssc login --agent` tokens no longer work for remote `/mcp`; local `ssc mcp` is unchanged (`docs/runbooks/remote-mcp.md`).

### c. Test Customer 2, in a private window

1. Open a private window, go to `https://console.delimitus.com`, and sign in as `admin2@test2.delimitus.com`.
2. Pass: the console shows Test Customer 2 with no apps, and none of the first customer's.
3. In a terminal, sign the CLI in as `admin2` for the operator's checks: `uv run ssc login --org $ORG2` (from the repo root), and sign in at Okta in the browser it opens.

## 5. Two-org checks **[real]**

The operator runs these with `admin2`'s CLI session, then with the founder's:

1. `ssc apps create iso2` and `ssc deploy` a fixture to its preview as `admin2`. Pass: the service runs in `ssc-c-proofcell02`, nothing appears in `ssc-c-proofcell01`, and the bundle is in `ssc-c-proofcell02-cell` under `bundles/$ORG2/`, not in the control blobs bucket.
2. As the founder, `ssc apps` and `ssc status iso2`. Pass: `iso2` is not listed, and `status` answers not found.
3. A deploy of the founder's app still lands in cell 1, and its bundle in `ssc-c-proofcell01-cell`.
4. The worker log shows no `WRONG_CELL`, and cell 2's agent log shows only `X-SSC-Org: $ORG2`.

## Record and clean up

Record the org id, the `usr_` ids and the pass or fail lines in SSC-101's status. If the org is not kept after SSC-101: delete its apps, then delete the SSO connection and the directory in WorkOS (this ends the WorkOS charges), and remove the Okta apps and users.
