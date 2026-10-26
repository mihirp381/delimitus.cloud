# ssc-cli

`ssc`, the command line tool for SSC (Small Software Cloud): deploy your company's internal apps
behind its single sign-on, share them and check on them.

```
uv tool install ssc-cli          # or 'ssc-cli[mcp]' to serve SSC's tools to a coding agent
```

Python 3.14; `uv tool install` fetches it when needed. Your SSC admin gives you your org id
(`org_...`) and assigns SSC to you in your identity provider.

```
ssc login --org <org id>         # signs you in through your company's single sign-on
ssc init                         # in the app folder: AGENTS.md, a Claude Code skill, MCP settings, ssc.toml
ssc apps create expenses         # the app, with a preview and a production environment
ssc deploy . --app expenses --wait
ssc promote expenses --wait      # build what preview runs for production, then deploy it
```

`ssc deploy` always goes to preview. A builder of the app promotes to production; a coding agent
cannot. A release that asks for new internet hosts or data connections reaches production only
after an admin of your company approves them.

When something fails, `ssc status <app>` shows each environment's state and `ssc logs <app>
--source build` the build's output. An error names its code and, when there is a next step, ends
with a `Fix:` line; `--json` on any command prints one object instead. Exit codes: 0 ok, 1 refused
or failed, 2 bad usage, 3 not signed in, 4 stopped on this machine before anything was sent (for
example by `ssc doctor`), 5 the network failed after retries.

`ssc --help` lists every command.
