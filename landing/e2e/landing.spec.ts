import { expect, test, type Page } from '@playwright/test';

// What SSC-065's done-when asks of the page in a browser: nothing from another origin, no
// cookie, nothing blocked by its own policy, a still hero under reduced motion, the table with
// JavaScript off, and a form that only confirms on the server's answer.

const PROMPT = 'YouBuild an inbound dock scheduler from our Snowflake data and deploy it on Delimitus.';

function watch(page: Page, origin: string) {
  const foreign: string[] = [];
  const cookies: string[] = [];
  const blocked: string[] = [];
  page.on('request', (r) => {
    const url = r.url();
    if (!url.startsWith('data:') && new URL(url).origin !== origin) foreign.push(url);
  });
  page.on('response', async (r) => {
    const header = await r.headerValue('set-cookie');
    if (header) cookies.push(`${r.url()}: ${header}`);
  });
  page.on('console', (m) => {
    if (m.type() === 'error' && /content security policy|refused to/i.test(m.text())) blocked.push(m.text());
  });
  page.on('pageerror', (e) => blocked.push(String(e)));
  return { foreign, cookies, blocked };
}

test('loads only from its own origin, sets no cookie and breaks no policy', async ({ page, baseURL }) => {
  const seen = watch(page, new URL(baseURL!).origin);
  const res = await page.goto('/');
  expect(res?.status()).toBe(200);
  expect(res?.headers()['content-security-policy']).toContain("default-src 'none'");
  // A timer, not requestAnimationFrame: headless WebKit on Linux can draw no frame, and the
  // loop then never ends.
  await page.evaluate(async () => {
    for (let y = 0; y < document.body.scrollHeight; y += 400) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 16));
    }
  });
  await page.locator('#pilot').scrollIntoViewIfNeeded();
  expect(seen.foreign).toEqual([]);
  expect(seen.cookies).toEqual([]);
  expect(seen.blocked).toEqual([]);
  expect(await page.context().cookies()).toEqual([]);
});

test('signs in at the console from the nav, the one link there', async ({ page }) => {
  await page.goto('/');
  const signIn = page.locator('a[href="https://console.delimitus.com/login"]');
  await expect(signIn).toHaveCount(1);
  await expect(page.locator('.nav__act a[href="https://console.delimitus.com/login"]')).toHaveCount(1);
  const navSignIn = page.locator('.nav__act .btn--ghost');
  await expect(navSignIn).toHaveText('Sign in');
  await expect(navSignIn).toBeVisible();
  await expect(page.locator('.hero__cta a', { hasText: 'Sign in' })).toHaveCount(0);
  // WebKit leaves links out of the Tab order by default, so focus it directly.
  await navSignIn.focus();
  await expect(navSignIn).toBeFocused();
});

test('the nav shows both buttons, and all seven links on a wide screen', async ({ page }) => {
  await page.goto('/');
  const buttons = page.locator('.nav__act .btn');
  await expect(buttons).toHaveText(['Sign in', 'Request a pilot']);
  for (const button of await buttons.all()) await expect(button).toBeInViewport({ ratio: 1 });
  const links = page.locator('.nav__links a');
  await expect(links).toHaveCount(7);
  if ((page.viewportSize()?.width ?? 0) >= 1440) {
    for (const link of await links.all()) await expect(link).toBeVisible();
    expect(await links.evaluateAll((all) => all.map((a) => a.getAttribute('href')))).toEqual(
      ['#how', '#example', '#works', '#get', '#security', '#cost', '#faq'],
    );
  } else {
    await expect(page.locator('.nav__links')).toBeHidden();
  }
});

test('has no sideways scroll', async ({ page }) => {
  await page.goto('/');
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test('the security cards stack to one column on a phone', async ({ page }) => {
  await page.goto('/');
  const columns = await page.evaluate(() => getComputedStyle(document.querySelector('.sec')!).gridTemplateColumns.split(' ').length);
  expect(columns).toBe((page.viewportSize()?.width ?? 0) <= 640 ? 1 : 3);
});

test('the calculator follows its inputs', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#calc')).toBeHidden();
  await page.locator('#model > summary').click();
  await expect(page.locator('[data-out="total"]')).toHaveText('$43,538');
  await page.locator('[data-in="apps"]').fill('1');
  await expect(page.locator('[data-out="total"]')).toHaveText('$20,441');
  await expect(page.locator('[data-out="apps"]')).toHaveText('1');
});

test('the operations tabs and the viewers switch', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('tab', { name: 'Fleet and dispatch' }).click();
  await expect(page.locator('#casePanel [data-bind="tool"]')).toHaveText('Codex');
  await page.getByRole('button', { name: /Priya Raman/ }).click();
  await expect(page.locator('[data-bind="deniedText"]')).toContainText('Not found');
  await page.getByRole('button', { name: /Sam Whitaker/ }).click();
  await expect(page.locator('[data-bind="who"]')).toHaveText('Sam Whitaker');
});

test('each team shows a simple ask and a bigger ask', async ({ page }) => {
  await page.goto('/');
  const panel = page.locator('#casePanel');
  await expect(panel.locator('.case__card--simple')).toBeVisible();
  await expect(panel.locator('.case__card--big')).toBeVisible();
  await expect(panel.locator('.case__card--simple .case__k')).toHaveText('Simple ask');
  await expect(panel.locator('.case__card--big .case__k')).toHaveText('Bigger ask');
  await page.getByRole('tab', { name: 'Facilities' }).click();
  await expect(panel.locator('[data-bind="askSimple"]')).toHaveText('A form to book a meeting room.');
  await expect(panel.locator('[data-bind="getSimple"] li')).toHaveCount(2);
  await expect(panel.locator('[data-bind="get"] li')).toHaveCount(3);
  await expect(panel.locator('.case__card--big')).toHaveCSS('opacity', '1');
  const columns = await panel.evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  expect(columns).toBe((page.viewportSize()?.width ?? 0) <= 860 ? 1 : 2);
});

test('the agent conversation plays once it is in view, and a step can be picked', async ({ page }) => {
  await page.goto('/');
  const url = page.locator('#term .chat__url');
  await expect(url).toBeHidden();
  await page.locator('#term').scrollIntoViewIfNeeded();
  await expect(url).toBeVisible({ timeout: 8000 });
  await expect(page.locator('#how [data-step="3"]')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#how [data-step="1"]').click();
  await expect(url).toBeHidden();
  await expect(page.locator('#term .chat__me')).toBeVisible();
});

test('the prompt is typed and the pipeline lights', async ({ page }) => {
  await page.goto('/');
  const me = page.locator('#term .chat__me');
  const reply = page.locator('#term .chat__ai[data-s="1"]');
  const url = page.locator('#term .chat__url');
  const nodes = page.locator('#term .term__pipe li');
  await expect(nodes).toHaveCount(5);
  await page.locator('#term').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  expect(((await me.textContent()) ?? '').length).toBeLessThan(PROMPT.length);
  expect(await page.locator('#term .chat').ariaSnapshot()).toContain(PROMPT.slice('You'.length));
  await expect(reply).toBeHidden();
  await expect(me).toHaveText(PROMPT, { timeout: 4000 });
  await expect(reply).toBeVisible();
  await expect(url).toBeVisible({ timeout: 8000 });
  for (const node of await nodes.all()) await expect(node).toHaveClass(/is-done/);
});

test('the list bursts out of the prompt and settles in place', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#burst')).toHaveClass(/is-armed/);
  await page.locator('#burst').scrollIntoViewIfNeeded();
  await expect(page.locator('#burst')).toHaveClass(/is-in/);
  const last = page.locator('#burst .burst__chips li').last();
  await expect(last).toHaveCSS('opacity', '1', { timeout: 4000 });
  await expect(page.locator('#burst .burst__url')).toHaveCSS('opacity', '1', { timeout: 4000 });
});

const ENDS = ['Yourself 567 h · $38,533', 'Delimitus 60 h · $4,080'];
const GAP = '507 h of IT time saved';
const SCALED_OUT = 'matrix(0, 0, 0, 0, 0, 0)';

async function curve(page: Page) {
  return page.locator('#curve svg').evaluate((svg) => {
    const ln = (k: string) => svg.querySelector(`.curve__ln--${k}`) as SVGPathElement;
    const pts = (k: string) => (ln(k).getAttribute('d')!.match(/[\d.]+/g) ?? []).map(Number);
    const off = (k: string) => ln(k).getAttribute('stroke-dashoffset');
    return {
      diy: pts('diy'),
      dlm: pts('dlm'),
      off: [off('diy'), off('dlm')],
      count: svg.querySelector('.curve__n')!.textContent,
      clip: Number(svg.querySelector('#curveClip rect')!.getAttribute('width')),
      labels: [...svg.querySelectorAll('.curve__lab')].map((t) => t.textContent),
      gap: svg.querySelector('.curve__save')!.textContent,
      pops: [...svg.querySelectorAll('.curve__pop')].map((g) => getComputedStyle(g).transform),
    };
  });
}

async function expectFinal(page: Page) {
  await expect.poll(async () => (await curve(page)).pops.every((t) => t === 'none'), { timeout: 4000 }).toBe(true);
  const c = await curve(page);
  expect(c.labels).toEqual(ENDS);
  expect(c.gap).toBe(GAP);
  expect(c.count).toBe('200 requests');
  expect(c.off.every((o) => o === null || Number(o) === 0)).toBe(true);
  expect(c.diy[2]).toBe(c.dlm[2]);
  expect(c.diy[1]).toBeLessThan(c.dlm[1]);
  expect(c.diy[3]).toBeLessThan(c.dlm[3]);
  expect(c.diy[3]).toBeLessThan(c.diy[1]);
  for (const t of await page.locator('#curve .curve__lab, #curve .curve__save').all()) await expect(t).toBeVisible();
}

test('the cost chart shows both lines, the gap and the end values', async ({ page }) => {
  await page.goto('/');
  const card = page.locator('#curve');
  await card.scrollIntoViewIfNeeded();
  await expect(card).toHaveClass(/is-done/, { timeout: 6000 });
  await expectFinal(page);
  const svg = card.locator('svg');
  await expect(svg).toHaveAttribute('role', 'img');
  await expect(svg).toHaveAttribute(
    'aria-label',
    'IT hours over 0 to 200 requests: doing it yourself reaches 567 h ($38,533), Delimitus 60 h ($4,080), 507 h less.',
  );
  await expect(card.locator('.curve__end')).toHaveText('~15 min is our estimate. Sources and method ↓');
  await expect(page.locator('#bars, .bars')).toHaveCount(0);
});

test('sources and method opens the cost model and its sources', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#model')).not.toHaveAttribute('open', '');
  await page.locator('#toSources').click();
  await expect(page.locator('#model')).toHaveAttribute('open', '');
  await expect(page.locator('#sources')).toHaveAttribute('open', '');
  await expect(page.locator('#sources > summary')).toBeInViewport();
  await expect(page.locator('#sources > summary')).toBeFocused();
  const cites = page.locator('#sources li[data-claim]');
  await expect(cites).toHaveCount(4);
  await expect(cites.nth(0)).toContainText('80 + 1–2 × 160 = 240–400 h');
  await expect(cites.nth(1)).toContainText('commissioned by Okta, July 2026');
  await expect(cites.nth(2)).toContainText('MetricNet');
  await expect(cites.nth(3)).toContainText('Our estimate: about 15 minutes');
});

test('the lines draw from the left once the chart is in view, and end complete', async ({ page }) => {
  await page.goto('/');
  const card = page.locator('#curve');
  await expect(card).toHaveClass(/is-armed/);
  await expect(card).not.toHaveClass(/is-in/);
  const before = await curve(page);
  expect(before.count).toBe('0 requests');
  expect(before.clip).toBe(0);
  expect(before.off.every((o) => Number(o) > 0)).toBe(true);
  expect(before.pops.every((t) => t === SCALED_OUT)).toBe(true);
  await card.scrollIntoViewIfNeeded();
  await expect(card).toHaveClass(/is-in/);
  await expect.poll(async () => {
    const c = await curve(page);
    const n = parseInt(c.count ?? '', 10);
    return n > 0 && n < 200 && c.clip > 0 && Number(c.off[0]) > 0 && c.pops.every((t) => t === SCALED_OUT);
  }, { intervals: [50], timeout: 3000 }).toBe(true);
  await expect(card).toHaveClass(/is-done/, { timeout: 5000 });
  await expectFinal(page);
});

test('the chart plays when the page opens on it', async ({ page }) => {
  await page.goto('/#cost');
  await expect(page.locator('#curve')).toHaveClass(/is-in/);
  await expect(page.locator('#curve')).toHaveClass(/is-done/, { timeout: 5000 });
  await expectFinal(page);
});

test('the chart fits its card with readable text and no sideways scroll', async ({ page }) => {
  await page.goto('/#cost');
  await expect(page.locator('#curve')).toHaveClass(/is-done/, { timeout: 5000 });
  await expectFinal(page);
  const fit = await page.locator('#curve svg').evaluate((svg) => {
    const box = svg.getBoundingClientRect();
    const scale = (svg as SVGSVGElement).getScreenCTM()!.a;
    const texts = [...svg.querySelectorAll('text')].filter((t) => t.getClientRects().length);
    const r = (sel: string) => svg.querySelector(sel)!.getBoundingClientRect();
    const apart = (a: DOMRect, b: DOMRect) => a.bottom <= b.top || b.bottom <= a.top || a.right <= b.left || b.right <= a.left;
    const [diy, dlm] = [...svg.querySelectorAll('.curve__lab')].map((t) => t.getBoundingClientRect());
    return {
      scroll: document.documentElement.scrollWidth - innerWidth,
      smallest: Math.min(...texts.map((t) => parseFloat(getComputedStyle(t).fontSize) * scale)),
      inside: texts.every((t) => {
        const b = t.getBoundingClientRect();
        return b.left >= box.left - 1 && b.right <= box.right + 1 && b.top >= box.top - 1 && b.bottom <= box.bottom + 1;
      }),
      apart: apart(diy, dlm) && apart(diy, r('.curve__save')) && apart(dlm, r('.curve__save')),
      right: svg.querySelectorAll('.curve__ax--r').length,
      narrow: matchMedia('(max-width: 599px)').matches,
    };
  });
  expect(fit.scroll).toBe(0);
  expect(fit.smallest).toBeGreaterThanOrEqual(11);
  expect(fit.inside).toBe(true);
  expect(fit.apart).toBe(true);
  expect(fit.right).toBe(fit.narrow ? 0 : 1);
});

const opacity = (el: Element) => getComputedStyle(el).opacity;
const background = (el: Element) => getComputedStyle(el).backgroundColor;
const running = (el: Element) => el.getAnimations().length;
const iterations = (el: Element) => el.getAnimations()[0]?.effect?.getComputedTiming().iterations;
const LIT = 'rgb(240, 237, 255)';

test('the hero steps light up in order and loop, text always shown', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-1440');
  await page.goto('/');
  const circles = page.locator('#heroSteps .enter__n');
  const circle = circles.nth(2);
  await page.waitForTimeout(300);
  for (const step of await page.locator('#heroSteps li').all()) expect(await step.evaluate(opacity)).toBe('1');
  expect(await circle.evaluate(background)).not.toBe(LIT);
  for (const c of await circles.all()) expect(await c.evaluate(iterations)).toBe(Infinity);
  await expect(circle).toHaveCSS('background-color', LIT, { timeout: 5000 });
  await expect.poll(() => circle.evaluate(background), { timeout: 4000 }).not.toBe(LIT);
  await expect(circle).toHaveCSS('background-color', LIT, { timeout: 6000 });
});

test('the hero steps light up below the fold without a scroll', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-1440');
  await page.setViewportSize({ width: 1440, height: 600 });
  await page.goto('/');
  const circle = page.locator('#heroSteps .enter__n').nth(2);
  await expect(circle).toHaveCSS('background-color', LIT, { timeout: 5000 });
});

test('the hero zooms into the app where there is room, and holds still where there is not', async ({ page }) => {
  await page.goto('/');
  const room = await page.evaluate(() => matchMedia('(min-width: 1100px) and (min-height: 640px)').matches);
  // Scroll as a person does: headless WebKit on Linux draws no frame for a script's scrollTo.
  const height = page.viewportSize()?.height ?? 0;
  await page.mouse.move(10, 10);
  await page.mouse.wheel(0, height * 0.6);
  const transform = () => page.locator('#scene').evaluate((el) => (el as HTMLElement).style.transform);
  if (room) {
    await expect.poll(transform).toContain('scale(');
    await page.mouse.wheel(0, height * (2.05 - 0.6));
    await expect(page.locator('#into')).toBeVisible();
    await expect(page.locator('#into')).not.toHaveCSS('opacity', '0');
  } else {
    await page.waitForTimeout(200);
    expect(await transform()).toBe('');
    await expect(page.locator('#into')).toBeHidden();
  }
});

test.describe('reduced motion', () => {
  test.use({ reducedMotion: 'reduce' });
  test('shows the whole conversation and the list at rest', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#term .chat__url')).toBeVisible();
    await expect(page.locator('#burst')).not.toHaveClass(/is-armed/);
  });
  test('shows the typed prompt and the lit pipeline at once', async ({ page }) => {
    await page.goto('/');
    expect(await page.locator('#term .chat__me').textContent()).toBe(PROMPT);
    const nodes = page.locator('#term .term__pipe li');
    await expect(nodes).toHaveCount(5);
    for (const node of await nodes.all()) expect(await node.getAttribute('class')).toContain('is-done');
  });
  test('shows the finished chart with no animation', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#curve')).not.toHaveClass(/is-armed/);
    await expectFinal(page);
    expect((await curve(page)).off).toEqual([null, null]);
    await page.locator('#curve').scrollIntoViewIfNeeded();
    await page.waitForTimeout(200);
    for (const g of await page.locator('#curve .curve__pop').all()) expect(await g.evaluate(running)).toBe(0);
    await expect(page.locator('#curve')).not.toHaveClass(/is-done/);
    await expectFinal(page);
  });
  test('shows the three hero steps lit at once', async ({ page }) => {
    await page.goto('/');
    const steps = page.locator('#heroSteps li');
    await expect(steps).toHaveCount(3);
    for (const step of await steps.all()) {
      expect(await step.evaluate(opacity)).toBe('1');
      expect(await step.evaluate(running)).toBe(0);
      const circle = step.locator('.enter__n');
      expect(await circle.evaluate(running)).toBe(0);
      expect(await circle.evaluate(background)).toBe(LIT);
    }
  });
  test('keeps the hero still', async ({ page }) => {
    await page.goto('/');
    await page.mouse.wheel(0, 900);
    await page.waitForTimeout(200);
    expect(await page.locator('#scene').evaluate((el) => (el as HTMLElement).style.transform)).toBe('');
  });
});

test.describe('without JavaScript', () => {
  test.use({ javaScriptEnabled: false });
  test('shows the default table', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#calcIn')).toBeHidden();
    await page.locator('#model > summary').click();
    await expect(page.locator('[data-out="total"]')).toHaveText('$43,538');
    await expect(page.locator('[data-out="cloud"]')).toHaveText('$2,738');
    await expect(page.locator('#term .chat__url')).toBeVisible();
    await expect(page.locator('#burst .burst__chips li').first()).toBeVisible();
  });

  test('shows the whole conversation and the lit pipeline', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#term .chat__me')).toHaveText(PROMPT);
    await expect(page.locator('#term .term__pipe li')).toHaveCount(5);
    for (const node of await page.locator('#term .term__pipe li').all()) await expect(node).toHaveClass(/is-done/);
    await expect(page.locator('#term .chat__url')).toBeVisible();
  });

  test('shows the finished chart', async ({ page }) => {
    await page.goto('/');
    await expectFinal(page);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBe(0);
  });

  test('sends the form as a plain post the server takes', async ({ page }, info) => {
    // Two projects post (one per engine): the service allows five requests a minute from one address.
    test.skip(!info.project.name.endsWith('-1440'));
    await page.goto('/');
    const form = page.locator('#pilotForm');
    await form.locator('[name="firstName"]').fill('Dana');
    await form.locator('[name="lastName"]').fill('Ortiz');
    await form.locator('[name="email"]').fill('dana@example.com');
    await form.locator('[name="company"]').fill('Example Logistics');
    const [answer] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/pilot-request')),
      form.getByRole('button', { name: 'Request a pilot' }).click(),
    ]);
    expect(answer.status()).toBe(200);
    await expect(page.getByText('We have your request and will reply by email.')).toBeVisible();
  });
});

test('the form confirms only on the server answer', async ({ page }, info) => {
  // One project posts: the service allows five requests a minute from one address.
  test.skip(info.project.name !== 'chromium-1440');
  await page.goto('/');
  const form = page.locator('#pilotForm');
  await form.getByRole('button', { name: 'Request a pilot' }).click();
  await expect(page.locator('#formStatus')).toContainText('Check the highlighted fields');
  await expect(form.locator('[name="email"]')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.locator('#formDone')).toBeHidden();
  await form.locator('[name="firstName"]').fill('Dana');
  await form.locator('[name="lastName"]').fill('Ortiz');
  await form.locator('[name="email"]').fill('dana@example.com');
  await form.locator('[name="company"]').fill('Example Logistics');
  await form.getByRole('button', { name: 'Request a pilot' }).click();
  await expect(page.locator('#formDone')).toBeVisible();
  await expect(form).toBeHidden();
});
