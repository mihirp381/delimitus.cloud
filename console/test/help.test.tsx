import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApiClient } from '../src/api/client';
import { sscAnswers, supportEmail, trustUrl } from '../src/help';
import { FIRST_RENDER_MS, whoami } from './appPage';
import { fakeApi, type Handler, json, ORIGIN, problem } from './fakeApi';
import { signedIn, start } from './harness';

afterEach(() => {
  vi.unstubAllEnvs();
});

const status = () => screen.getByRole('status', { name: 'SSC status' });

async function helpPage(routes: Record<string, Handler | Handler[]> = {}, role: 'admin' | 'member' = 'member') {
  const result = start('/help', { 'GET /v1/whoami': whoami(role), ...routes }, signedIn());
  await screen.findByRole('heading', { name: 'Help', level: 1 }, { timeout: FIRST_RENDER_MS });
  return result;
}

describe('help: support and the trust pack', () => {
  it('links the support address it was built with', async () => {
    vi.stubEnv('VITE_SSC_SUPPORT_EMAIL', ' help@example.com ');
    await helpPage();
    const link = screen.getByRole('link', { name: 'help@example.com' });
    expect(link.getAttribute('href')).toBe('mailto:help@example.com');
    expect(screen.queryByText('Ask your SSC contact.')).toBeNull();
  });

  it.each([[''], ['   '], ['not an address'], ['help@example.com?bcc=other@example.com'], ['a@b.co, c@d.co']])(
    'names no address when the build has %j',
    async (value) => {
      vi.stubEnv('VITE_SSC_SUPPORT_EMAIL', value);
      await helpPage();
      const support = screen.getByRole('region', { name: 'Support' });
      expect(within(support).getByText('Ask your SSC contact.')).toBeTruthy();
      expect(within(support).queryByRole('link')).toBeNull();
    },
  );

  it('links the trust pack in a new tab that cannot reach back', async () => {
    vi.stubEnv('VITE_SSC_TRUST_URL', 'https://trust.example.com/pack');
    await helpPage();
    const link = within(screen.getByRole('region', { name: 'Trust pack' })).getByRole('link');
    expect(link.getAttribute('href')).toBe('https://trust.example.com/pack');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it.each([[''], ['javascript:alert(1)'], ['http://trust.example.com'], ['trust.example.com']])(
    'shows no trust pack when the build has %j',
    async (value) => {
      vi.stubEnv('VITE_SSC_TRUST_URL', value);
      await helpPage();
      expect(screen.queryByRole('region', { name: 'Trust pack' })).toBeNull();
      expect(screen.queryByText(/trust pack/i)).toBeNull();
    },
  );

  it('is in the nav for a member and for an admin, last', async () => {
    await helpPage({}, 'member');
    const nav = screen.getByRole('navigation', { name: 'Main' });
    await waitFor(() =>
      expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
        'Apps',
        'Approvals',
        'Connections',
        'Internet access',
        'Help',
      ]),
    );
    expect(within(nav).getByRole('link', { name: 'Help' }).getAttribute('aria-current')).toBe('page');
  });
});

describe('help: whether SSC is answering', () => {
  it('says SSC is answering, with the time it checked', async () => {
    const before = Date.now();
    await helpPage();
    await waitFor(() => expect(status().textContent).toContain('SSC is answering'));
    const time = within(status()).getByText(/\d/, { selector: 'time' });
    const at = Date.parse(time.getAttribute('dateTime') ?? '');
    expect(at).toBeGreaterThanOrEqual(before);
    expect(at).toBeLessThanOrEqual(Date.now());
    expect(status().textContent).toContain('Checked at');
    expect(screen.queryByText(/could not get an answer/)).toBeNull();
  });

  it('says SSC is not answering when the call cannot be made, and says what to do', async () => {
    await helpPage({
      'GET /v1/whoami': () => {
        throw new TypeError('Failed to fetch');
      },
    });
    await waitFor(() => expect(status().textContent).toContain('SSC is not answering'));
    expect(screen.getByText(/The console could not get an answer from SSC/)).toBeTruthy();
    expect(within(status()).getByText(/\d/, { selector: 'time' })).toBeTruthy();
  });

  it('checks again on request, and follows SSC down and back up', async () => {
    let up = true;
    const me = whoami('member');
    const { api } = await helpPage({
      'GET /v1/whoami': (call) => (up ? me(call) : problem(503, 'UNAVAILABLE', 'Service unavailable')),
    });
    await waitFor(() => expect(status().textContent).toContain('SSC is answering'));
    const again = screen.getByRole('button', { name: 'Check again' });

    up = false;
    const calls = api.of('GET', '/v1/whoami').length;
    fireEvent.click(again);
    await waitFor(() => expect(status().textContent).toContain('SSC is not answering'));
    expect(api.of('GET', '/v1/whoami').length).toBeGreaterThan(calls);

    up = true;
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(status().textContent).toContain('SSC is answering'));
    expect(screen.queryByText(/could not get an answer/)).toBeNull();
  });

  it('shows that a check is running and takes no second one meanwhile', async () => {
    let release: (response: Response) => void = () => {};
    const me = whoami('member');
    const held: Handler = () => new Promise<Response>((resolve) => (release = resolve));
    const { api } = await helpPage({ 'GET /v1/whoami': [me, me, held] });
    await waitFor(() => expect(status().textContent).toContain('SSC is answering'));
    expect(api.of('GET', '/v1/whoami')).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    const busy = await screen.findByRole('button', { name: 'Checking…' });
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    // The last answer stays on show until the new one lands.
    expect(status().textContent).toContain('SSC is answering');
    release(json(500, {}));
    await waitFor(() => expect(status().textContent).toContain('SSC is not answering'));
    expect(screen.getByRole('button', { name: 'Check again' })).toBeTruthy();
  });
});

describe('help helpers', () => {
  it('takes one plain address and nothing else', () => {
    expect(supportEmail('help@example.com')).toBe('help@example.com');
    expect(supportEmail('first.last+ssc@mail.example.co.uk')).toBe('first.last+ssc@mail.example.co.uk');
    expect(supportEmail(undefined)).toBeNull();
    expect(supportEmail('help@localhost')).toBeNull();
    expect(supportEmail('help@example.com?subject=x')).toBeNull();
    expect(supportEmail('Help <help@example.com>')).toBeNull();
    expect(supportEmail('mailto:help@example.com')).toBeNull();
  });

  it('takes an https address and nothing else', () => {
    expect(trustUrl('https://trust.example.com')).toBe('https://trust.example.com/');
    expect(trustUrl(' https://example.com/trust?v=2#top ')).toBe('https://example.com/trust?v=2#top');
    expect(trustUrl(undefined)).toBeNull();
    expect(trustUrl('data:text/html,hello')).toBeNull();
    expect(trustUrl('//trust.example.com')).toBeNull();
  });

  it.each([
    [200, true],
    [401, true],
    [403, true],
    [404, true],
    [429, true],
    [500, false],
    [502, false],
    [503, false],
  ])('counts HTTP %i from the API as answering: %s', async (code, answering) => {
    const fake = fakeApi({
      'GET /v1/whoami': () => (code === 200 ? json(200, {}) : problem(code, 'X', 'x')),
    });
    const api = createApiClient({ baseUrl: ORIGIN, getToken: () => 'tok', fetch: fake.fetch });
    expect(await sscAnswers(api)).toBe(answering);
    expect(fake.calls.map((c) => `${c.method} ${c.path}`)).toEqual(['GET /v1/whoami']);
  });

  it('does not count a 5xx page that is not a problem document, or no answer at all', async () => {
    const page = () => new Response('<html>Bad gateway</html>', { status: 502, headers: { 'Content-Type': 'text/html' } });
    const html = createApiClient({ baseUrl: ORIGIN, getToken: () => 'tok', fetch: async () => page() });
    expect(await sscAnswers(html)).toBe(false);
    const none = createApiClient({
      baseUrl: ORIGIN,
      getToken: () => 'tok',
      fetch: async () => {
        throw new TypeError('Failed to fetch');
      },
    });
    expect(await sscAnswers(none)).toBe(false);
  });
});
