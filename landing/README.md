# delimitus.com

The landing page (SSC-065, decision 025). One self-contained HTML file with no build step. It is
served by `packages/ssc_landing` (`python -m ssc_landing`), which also takes the pilot request
form.

| File | What it is |
|---|---|
| `index.html` | The page: inline CSS and JS, two PNG data URIs, no other file |
| `art/` | The dithered pixel workstation and the horizon mask; `cd landing/art && python3 embed.py` rebuilds both and re-embeds them (needs Pillow and numpy, not part of the workspace) |
| `CLAIMS.md` | Every claim on the page, its ticket and status, or its public source; the blockers before publishing |
| `denylist.txt` | Words that never appear on the page |
| `calculator_vectors.json`, `calculator.test.mjs` | The cost calculator's test vectors; `node --test landing/calculator.test.mjs` |

Run it locally:

```sh
SSC_LANDING_ENV=dev SSC_LANDING_PAGE=landing/index.html SSC_LANDING_ORIGIN=http://localhost:8080 \
  SSC_LANDING_TRUSTED_HOPS=0 uv run python -m ssc_landing
```

In dev, pilot requests stay in memory. The Python tests (`packages/ssc_landing/tests`) check the
denylist, the claims ids, the content security policy, the page weight and the no-script table.

## Rules the page follows

- **Content security policy.** The service allows exactly the page's inline `<script>` and
  `<style>` blocks, by SHA-256 hash, computed at start-up. So: bare `<script>` and `<style>`
  tags only, no `style=` attribute, no inline event handler, and scripts set styles through
  CSSOM (`el.style.x`, `classList`) or attributes, never through `innerHTML`. The service refuses
  to start on a page that breaks this.
- **Nothing from another origin.** No fonts, scripts, images, analytics or embeds. The apex is
  the same site as `auth.` and `console.`, so the page also sets no cookie.
- **Fonts.** The page uses the system font stack. The old page named Inter and JetBrains Mono
  only as the first choice before the system stack and loaded them from Google Fonts; SSC-065
  asked to self-host them, but at about 100 KB a weight they would push the page towards the
  400 KB limit, and the system stack is what most visitors already saw while the fonts loaded.
  To self-host them later, embed them as `@font-face` data URIs and add `font-src data:` to the
  policy in `page.py`.
- **Copy.** Every claim carries `data-claim` and a row in `CLAIMS.md`. Features are worded as
  part of the private pilot offer until their tickets are Done. No price for Delimitus.
- **Weight.** Under 400 KB, bitmaps included.

## Adding "Sign in" later

When the console is live (SSC-064, SSC-057), add a link in `.nav__act` before "Request a pilot":

```html
<a class="nav__link" href="https://console.delimitus.com/">Sign in</a>
```

A link to another origin is a navigation, not a load, so the policy needs no change, but
`page.py`'s check refuses any `href` to another origin today; allow `https://console.delimitus.com/`
there and in the test.
