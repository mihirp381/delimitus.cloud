# delimitus.com

The landing page (SSC-065, decision 028). One self-contained HTML file with no build step. It is
served by `packages/ssc_landing` (`python -m ssc_landing`), which also takes the pilot request
form.

| File | What it is |
|---|---|
| `index.html` | The page: inline CSS, JS and SVG, one PNG data URI (the horizon), no other file |
| `privacy.html` | The privacy page at `/privacy`: inline CSS and SVG, no script; `SSC_LANDING_PRIVACY_PAGE`, default `privacy.html` beside the page |
| `art/` | The horizon mask; `cd landing/art && python3 embed.py` rebuilds it and re-embeds it (needs Pillow and numpy, not part of the workspace) |
| `CLAIMS.md` | Every claim on the page with its truth state and the date checked, and the public and economics sources |
| `denylist.txt` | Words that never appear on the page |
| `forbidden_claims.txt` | Absolute or unproven phrases that never appear on the page |
| `calculator_vectors.json`, `calculator.test.mjs` | The cost calculator's test vectors; `node --test landing/calculator.test.mjs` |

Run it locally:

```sh
SSC_LANDING_ENV=dev SSC_LANDING_PAGE=landing/index.html SSC_LANDING_ORIGIN=http://localhost:8080 \
  SSC_LANDING_TRUSTED_HOPS=0 uv run python -m ssc_landing
```

In dev, pilot requests stay in memory. The Python tests (`packages/ssc_landing/tests`) check the
denylist, the forbidden phrases, the claims ids, the content security policy, the page weight and
the no-script table.

## Hosting

The page is served from the control plane's entry load balancer (`ssc-control-entry`), as two more
hosts, `delimitus.com` and `www.delimitus.com`, on its URL map, with a second managed certificate
that names just those two. The service, its account and the request bucket are in the public
stage's control project (`infra/ssc_infra/landing.py`, wired in `control.py`). Control setting
`landing: true` makes the account, bucket and founder alert; `landing_image` (a digest of
`ssc-landing` in the platform registry) makes the service and puts it on the entry. `www` is
redirected to the apex by the service.

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
- **Copy.** Every claim carries `data-claim` and a row in `CLAIMS.md`. Inside a section marked
  `data-claims`, every paragraph, list item and heading except a `label` carries an id, on itself
  or on an enclosing element. Features are worded as
  part of the private pilot offer until their tickets are Done. No price for Delimitus.
- **Weight.** Under 400 KB, bitmaps included.
- **Motion.** The hero's zoom runs only where the stage fits the screen (at least 1100 × 640),
  motion is allowed and scripts run (`scripting: enabled`); everywhere else the hero is a static
  block. Its flows' moving dots are SMIL, which ignores the reduced-motion CSS, so that CSS
  hides them.
  The three steps under the hero light up in order from 1 to 3 with the line between them filling,
  hold, reset and repeat every 5.6s; pure CSS, so under reduced motion they are still and shown lit.
  How it works plays itself once it is in view: each step's bar fills, step 1 types the prompt,
  the chat lines land one by one and the pipeline lights, once per page load. Under reduced
  motion, or with no IntersectionObserver, it shows step 3 finished; with
  JavaScript off the markup holds the whole conversation and the lit pipeline.
  The team cards swap with a short fade and their list items step in; under reduced motion they
  swap at once, and with JavaScript off the warehouse asks are in the markup.
  The cost section's bar chart grows each bar from the left, 150 ms apart, once it is in view, once
  per page load, with no fade. Under reduced motion, or with no IntersectionObserver, and with
  JavaScript off, the bars show at full width.

## Nav

A solid white 60px bar: the brand, five section links (Product, Integrations, Security, Pricing,
FAQ), then Sign in and Request access. The link for the section under the bar carries
`aria-current="true"`, set by one IntersectionObserver; with none, no link is marked. Below 760px
the links and Sign in move into a menu behind a checkbox and its label, so it opens without
JavaScript; the script closes it on a link or Escape and keeps the label's `aria-expanded`. The
menu slides down, and under reduced motion just appears. A skip link to `#how` comes first.

## Sign in

The nav's Sign in link is the page's one link to the console's sign-in,
`https://console.delimitus.com/login`; a link is a navigation, not a load, so the content security
policy needs no change. `page.py`'s check allows that exact `href` (`SIGN_IN`) and still refuses
every other `href`, `src` or `action` to another origin.

## Privacy

`privacy.html` is held to the same checks at start-up, with its own policy by hash. Its one link to
another origin is Google's API Services User Data Policy (`USER_DATA_POLICY`), which Google's
sign-in review asks for. It names WorkOS as a processor, the one denylisted word it may use. Its
facts and their sources are in `CLAIMS.md`, "The privacy page"; the footer of `index.html` links
it.
