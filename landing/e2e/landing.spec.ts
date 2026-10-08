import { expect, test, type Page } from '@playwright/test';

// What SSC-065's done-when asks of the page in a browser: nothing from another origin, no
// cookie, nothing blocked by its own policy, a still hero under reduced motion, the table with
// JavaScript off, and a form that only confirms on the server's answer.

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
  await page.evaluate(async () => {
    for (let y = 0; y < document.body.scrollHeight; y += 400) {
      window.scrollTo(0, y);
      await new Promise((r) => requestAnimationFrame(() => r(null)));
    }
  });
  await page.locator('#pilot').scrollIntoViewIfNeeded();
  expect(seen.foreign).toEqual([]);
  expect(seen.cookies).toEqual([]);
  expect(seen.blocked).toEqual([]);
  expect(await page.context().cookies()).toEqual([]);
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

test('the list bursts out of the prompt and settles in place', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#burst')).toHaveClass(/is-armed/);
  await page.locator('#burst').scrollIntoViewIfNeeded();
  await expect(page.locator('#burst')).toHaveClass(/is-in/);
  const last = page.locator('#burst .burst__chips li').last();
  await expect(last).toHaveCSS('opacity', '1', { timeout: 4000 });
  await expect(page.locator('#burst .burst__url')).toHaveCSS('opacity', '1', { timeout: 4000 });
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
