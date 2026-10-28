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
  await expect(page.locator('[data-out="total"]')).toHaveText('$32,658');
  await page.locator('[data-in="apps"]').fill('1');
  await expect(page.locator('[data-out="total"]')).toHaveText('$9,561');
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

test('the long way races once it is in view', async ({ page }) => {
  await page.goto('/');
  const race = page.locator('#race');
  const last = race.locator('.race__lane--diy li').last();
  const wait = race.locator('.race__lane--dlm li').last();
  await expect(race).toHaveClass(/is-armed/);
  await expect(last).not.toHaveClass(/is-done/);
  await race.scrollIntoViewIfNeeded();
  await expect(race).toHaveClass(/is-in/);
  await expect(last).toHaveClass(/is-done/, { timeout: 8000 });
  await expect(race.locator('[data-race="hours"]')).toHaveText('92');
  await expect(race.locator('[data-race="total"]')).toHaveText('$32,658');
  await expect(wait).toHaveClass(/is-wait/);
  await expect(wait).not.toHaveClass(/is-done/);
});

const opacity = (el: Element) => getComputedStyle(el).opacity;

test('the hero steps ease in one after another on load', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-1440');
  await page.goto('/');
  const third = page.locator('#heroSteps li').nth(2);
  await page.waitForTimeout(300);
  expect(Number(await third.evaluate(opacity))).toBeLessThan(1);
  await expect(third).toHaveCSS('opacity', '1', { timeout: 4000 });
  await expect(third).toHaveCSS('transform', 'none');
});

test('the hero steps show below the fold without a scroll', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-1440');
  await page.setViewportSize({ width: 1440, height: 600 });
  await page.goto('/');
  const third = page.locator('#heroSteps li').nth(2);
  await expect(third).toHaveCSS('opacity', '1', { timeout: 4000 });
  await third.scrollIntoViewIfNeeded();
  expect(await third.evaluate(opacity)).toBe('1');
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
  test('shows the long way finished at load', async ({ page }) => {
    await page.goto('/');
    const race = page.locator('#race');
    await expect(race).not.toHaveClass(/is-armed/);
    for (const stage of await race.locator('.race__lane--diy li, .race__lane--dlm li:not(:last-child)').all()) {
      expect(await stage.getAttribute('class')).toContain('is-done');
    }
    expect(await race.locator('.race__lane--dlm li').last().getAttribute('class')).toContain('is-wait');
    expect(await race.locator('[data-race="hours"]').textContent()).toBe('92');
  });
  test('shows the three hero steps at once', async ({ page }) => {
    await page.goto('/');
    const steps = page.locator('#heroSteps li');
    await expect(steps).toHaveCount(3);
    for (const step of await steps.all()) {
      expect(await step.evaluate(opacity)).toBe('1');
      expect(await step.evaluate((el) => el.getAnimations().length)).toBe(0);
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
    await expect(page.locator('[data-out="total"]')).toHaveText('$32,658');
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

  test('shows the long way finished', async ({ page }) => {
    await page.goto('/');
    const stages = page.locator('#race .race__lane--diy li, #race .race__lane--dlm li:not(:last-child)');
    await expect(stages).toHaveCount(10);
    for (const stage of await stages.all()) await expect(stage).toHaveClass(/is-done/);
    await expect(page.locator('#race .race__lane--dlm li').last()).toHaveClass(/is-wait/);
    await expect(page.locator('#race [data-race="hours"]')).toHaveText('92');
    await expect(page.locator('#race [data-race="total"]')).toBeVisible();
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
