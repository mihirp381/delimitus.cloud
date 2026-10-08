# How to get help

If something is wrong with your app, or you are stuck, write to a person at the support inbox: **<support_email>**.

Replace `<support_email>` with the address from the `support_email` setting before this page goes to a builder.

## Get started

You need Python 3.14 and [uv](https://docs.astral.sh/uv/) (or pipx).

1. Install the CLI: `uv tool install ssc-cli` (or `pipx install ssc-cli`). Until it is on PyPI, your org admin gives you the four wheel files (`ssc_contracts`, `ssc_shared`, `ssc_bundle`, `ssc_cli`); put them in one folder and run `uv tool install --find-links <that folder> ssc-cli`.
2. Check it: `ssc --version`.
3. Sign in with your work account: `ssc login --org <org id>`. Your org admin gives you the org id (`org_` and 20 characters).
4. Create the app once: `ssc apps create <name>` (lower-case; it becomes part of the address).
5. In your app's folder: `ssc init` (it writes the notes your coding agent reads), then `ssc doctor`.
6. Deploy: `ssc deploy --app <name> --wait`. It prints the preview address when the app is live. This takes under ten minutes from a fresh install.
7. When preview is right: `ssc promote <name> --wait` (production may first need an org admin's approval). If a new version is wrong: `ssc releases <name>`, then `ssc rollback <name> R<number> --wait`.

If a deploy fails, the error names a code and, for a failed build or health check, the last lines of the log. The `Fix:` line says what to change.

## What to send

- The app's name and whether it is prod or preview.
- What you did and what you saw, with the time and your time zone.
- The deploy id, if a deploy is involved.
- Any error code on the screen, such as `DB_TIER_FULL`.

Never send a password, a token or a secret value. We never need one.

## Before you write

- **The first load is slow.** Apps sleep when nobody uses them, and wake in a few seconds. After that they are fast. Ask your org admin about the warm option for an app people open every day.
- **A dashboard reloaded after an hour.** A session app's connection ends at 60 minutes. Keep what matters in the database or in the page's URL.
- **A deploy is waiting.** The first deploy that needs a database waits while the database is created, a few minutes.
- **`DB_TIER_FULL`.** The company's database is full. Your org admin can ask for the bigger database.

## What to expect

If many people are affected, we send a status note to your admin first, and update it at the time it names, even when there is no news.

If an app is leaking data or running up cost, say so in the subject line.
