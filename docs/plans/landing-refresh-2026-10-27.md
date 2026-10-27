# Landing page refresh (2026-10-27)

Source: founder review of delimitus.com (eleven points, 2026-10-27) and a read of `landing/`,
`packages/ssc_landing`, `landing/e2e` and the console sign-in branch at main 707a645. Pre-pilot, no
users: nothing on the page is load-bearing for a customer yet.

## 0. What stays true

- One file, `landing/index.html`, under 400 KB, bare inline `<script>` and `<style>` only, no `style=`,
  no `on*=`, nothing loaded from another origin, no cookie (decision 028, `ssc_landing.page.check_page`).
- Every claim carries `data-claim` and a row in `landing/CLAIMS.md`; inside a `data-claims` section every
  paragraph, list item and heading except a `label` carries an id. `denylist.txt` and
  `forbidden_claims.txt` hold. No dollar figure outside `#cost`. The agent is never shown approving;
  "an admin of your company" appears exactly seven times; "waiting for IT", "an agent can never approve a
  request", "One prompt and two approvals later" and the tools sentence stay (`tests/test_page.py`).
- Motion: CSS and SMIL, driven by classes and CSSOM; still under `prefers-reduced-motion: reduce`; the
  finished state visible with JavaScript off; the hero zoom only where the stage fits.
- The console's sign-in page is `https://console.delimitus.com/login` (branch `signin/org-invite`, PR 6:
  one card, organisation id or invite code, "Continue"). The landing links to it and nothing more, so the
  two pieces of work never touch the same file.

## 1. Tasks

| # | Task | Founder point |
|---|---|---|
| T1 | Sign in: a `Sign in` link in the nav and a `Sign in` button in the hero, both to `https://console.delimitus.com/login`; `page.py` allows that one href and nothing else from another origin | 1 |
| T2 | Copy: "Secure data connections"; the hero's "Private pilot. Not generally available yet." line removed (and "Private pilot." from `og:description`); the zoom caption reworded; every "Illustration · example …" caption, the "Example data" chip and the footer's example-data line removed; "Product names belong to their owners…" removed in both places and the `names` row dropped from `CLAIMS.md`; placeholder carrier names replaced by invented ones | 2, 3, 4, 5, 8 |
| T3 | How it works: the terminal types the prompt, lands each line with a tick, lights a five-stage pipeline (Build, Keys, Secrets, Login, Preview), pulses the two "waiting for IT" lines, slides the preview address in; the step cards show progress | 6 |
| T4 | What operations teams ask for: each team shows a simple ask and a bigger ask side by side, each with what it gets; switching a team animates the cards in | 7, 9 |
| T5 | The long way: in `#cost`, two lanes race from "the tool works" to "the team can open it": do it yourself (eight stages, an IT-hours counter from the sourced defaults, the cloud bill starting early) against Delimitus (one prompt, minutes to preview, then IT's approvals); scroll-triggered and played once, at rest under reduced motion and without JavaScript | 11 |
| T6 | Docs, claims, tests: `landing/README.md`, `CLAIMS.md`, e2e for every new behaviour, the full check list green | all |

Open with the founder (not in this branch): the meaning of point 10 ("change branding; running can become
expensive if they wish to"); whether the remaining "pilot" wording (the chip under the examples, the cost
column, the FAQ, the request section) stays; the pilot boundaries section: removed on the founder's decision
2026-10-27 (custom domains are planned, `docs/plans/custom-domains-2026-10-26.md`); the trademark line is removed on request.

## 2. Success criteria

- T1: `grep -c 'href="https://console.delimitus.com/login"' landing/index.html` is 2; `check_page`
  still refuses any other external `href`, every external `src` and `action` (tests for each); the
  Playwright origin watch stays empty (a link is not a load); the nav link is reachable by keyboard.
- T2: the removed strings are absent (`Private pilot. Not generally available`, `Illustration ·`,
  `Example data`, `Product names belong to their owners`, `Example Freight`, `Sample Linehaul`,
  `Demo Carriers`, `Placeholder Haulage`); `Secure data connections` present; `test_claims` and
  `test_page` green with the `names` row gone.
- T3: with motion allowed, the prompt is typed before the first agent line shows, all five pipeline
  nodes carry `is-done` once step 3 is reached, `#term .chat__url` becomes visible within 8 s of the
  terminal entering the viewport (existing test), the animation plays once per page load; under
  reduced motion and without JavaScript the whole conversation, the lit pipeline and the address are
  visible at once; clicking a step still selects it (existing test).
- T4: five tabs still switch (`Fleet and dispatch` still binds `Codex`); each panel shows two asks with
  the `Simple ask` and `Bigger ask` labels; every list item in `#get` carries a claim id; the panel
  stacks to one column at 360 px with no sideways scroll.
- T5: `#race` sits inside `#cost`; its figures derive from `calculator_vectors.json` defaults (80 setup
  hours, 12 per app, 2 a month, $129.53 a month) and are asserted against them in `test_page.py`; the
  Delimitus lane never shows an approval granted; the do-it-yourself lane ends on the same total the
  trio card shows; reduced motion and no-JavaScript show both lanes complete.
- T6: `uv run pytest packages/ssc_landing -q`, `node --test landing/calculator.test.mjs`,
  `npm run e2e` in `landing/e2e` (four projects), `uv run ruff format --check . && uv run ruff check .`,
  `uv run pyright` all green; page under 400 KB; README's "Adding Sign in later" replaced by the fact.
