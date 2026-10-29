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

const ROWS = [
  ['Yourself', '240–400 h'],
  ['Delimitus', '~10 h'],
  ['Yourself', '98 min'],
  ['Delimitus', '~15 min'],
];
async function barWidths(page: Page) {
  return page.locator('#bars .bars__v').evaluateAll((bars) =>
    bars.map((b) => ({ bar: b.getBoundingClientRect().width, track: (b.parentElement as Element).getBoundingClientRect().width })),
  );
}
async function expectFullWidth(page: Page) {
  const [diy, login, ticket, review] = await barWidths(page);
  expect(Math.abs(diy.bar - diy.track)).toBeLessThan(1);
  expect(Math.abs(login.bar - Math.max(6, login.track * 0.025))).toBeLessThan(1);
  expect(Math.abs(ticket.bar - ticket.track * 0.98)).toBeLessThan(1);
  expect(Math.abs(review.bar - review.track * 0.15)).toBeLessThan(1);
}

test('the chart shows four one-line rows and no citations of its own', async ({ page }) => {
  await page.goto('/');
  const rows = page.locator('#bars .bars__row');
  await expect(rows).toHaveCount(4);
  for (const [i, [label, value]] of ROWS.entries()) {
    const row = rows.nth(i);
    await expect(row.locator('.bars__l')).toHaveText(label);
    await expect(row.locator('.bars__n')).toHaveText(value);
    await expect(row.locator('.bars__track')).toHaveAttribute('aria-hidden', 'true');
    const [l, t, n] = await row.locator('.bars__l, .bars__track, .bars__n').evaluateAll((els) =>
      els.map((e) => { const r = e.getBoundingClientRect(); return r.top + r.height / 2; }),
    );
    expect(Math.abs(l - t)).toBeLessThan(3);
    expect(Math.abs(n - t)).toBeLessThan(3);
  }
  await expect(page.locator('#bars figcaption')).toHaveText(['Setup · IT hours', 'Each request · minutes']);
  await expect(page.locator('#bars sup, #bars ol, #bars li, #bars .bars__l span')).toHaveCount(0);
  await expect(page.locator('#bars .bars__end')).toHaveText('~15 min is our estimate. Sources and method ↓');
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

test('the panels sit side by side from 900px and stack below', async ({ page }) => {
  await page.goto('/');
  const [a, b] = await page.locator('#bars figure').evaluateAll((f) => f.map((x) => x.getBoundingClientRect().top));
  const wide = await page.evaluate(() => matchMedia('(min-width: 900px)').matches);
  if (wide) expect(b).toBe(a);
  else expect(b).toBeGreaterThan(a);
});

test('each Delimitus bar is shorter than its do-it-yourself bar', async ({ page }) => {
  await page.goto('/');
  await page.locator('#bars').scrollIntoViewIfNeeded();
  await expect(page.locator('#bars')).toHaveClass(/is-in/);
  await page.waitForTimeout(1600);
  const [diy, login, ticket, approve] = await barWidths(page);
  expect(login.bar).toBeLessThan(diy.bar);
  expect(login.bar).toBeGreaterThanOrEqual(6);
  expect(approve.bar).toBeGreaterThan(0);
  expect(approve.bar).toBeLessThan(ticket.bar);
});

test('the bars grow from zero once the chart is in view, and end at full width', async ({ page }) => {
  await page.goto('/');
  const chart = page.locator('#bars');
  await expect(chart).toHaveClass(/is-armed/);
  await expect(chart).not.toHaveClass(/is-in/);
  for (const w of await barWidths(page)) expect(w.bar).toBe(0);
  await chart.scrollIntoViewIfNeeded();
  await expect(chart).toHaveClass(/is-in/);
  await expect.poll(async () => (await barWidths(page))[0].bar).toBeGreaterThan(0);
  await expect.poll(async () => {
    const [diy] = await barWidths(page);
    return Math.round(diy.track - diy.bar);
  }, { timeout: 4000 }).toBe(0);
  await page.waitForTimeout(1200);
  await expectFullWidth(page);
  expect(await page.locator('#bars .bars__v').evaluateAll((b) => b.every((x) => getComputedStyle(x).opacity === '1'))).toBe(true);
});

test('the bars grow when the page opens on the chart', async ({ page }) => {
  await page.goto('/#cost');
  await expect(page.locator('#bars')).toHaveClass(/is-in/);
  await page.waitForTimeout(1600);
  await expectFullWidth(page);
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
  test('shows the bars at full width with no animation', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#bars')).not.toHaveClass(/is-armed/);
    await expectFullWidth(page);
    await page.locator('#bars').scrollIntoViewIfNeeded();
    await page.waitForTimeout(200);
    for (const bar of await page.locator('#bars .bars__v').all()) expect(await bar.evaluate(running)).toBe(0);
    await expectFullWidth(page);
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

  test('shows the bars at full width', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('#bars .bars__row')).toHaveCount(4);
    await expectFullWidth(page);
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
