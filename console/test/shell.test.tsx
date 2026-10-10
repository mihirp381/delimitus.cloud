import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { THEME_KEY } from '../src/theme';
import { type Handler, json } from './fakeApi';
import { signedIn, start, WHOAMI } from './harness';

const MEMBER = () =>
  json(200, {
    org_id: 'org_ffffffffffffffffffff',
    subject: 'dev-member',
    kind: 'user',
    credential_id: 'c',
    is_agent: false,
    client_id: null,
    role: 'member',
  });

function request(id: string, state: string) {
  return {
    id,
    app_id: 'app_aaaaaaaaaaaaaaaaaaaa',
    environment_id: 'env_prod0000000000000000',
    kind: 'connect_data_source',
    subject_key: 'warehouse',
    payload: {},
    state,
    requested_by_user_id: 'usr_cccccccccccccccccccc',
    requested_by_name: 'Olivia Owner',
    app: 'expenses',
    environment: 'prod',
    requested_via_agent: false,
    decided_by_user_id: null,
    decided_at: null,
    decision_reason: null,
    decision_channel: null,
    recorded_by_operator: null,
    policy_decision_id: null,
    created_at: '2026-09-29T09:00:00Z',
  };
}

function inbox(approvals: unknown[], next: string | null = null): Handler {
  return () => json(200, { approvals, next_before: next });
}

const APPS: Record<string, Handler> = {
  'GET /v1/apps': () => json(200, { apps: [] }),
  'GET /v1/connections': () => json(200, { connections: [] }),
};

/** Only a member's shell is asked for here, so the admin's inventory is never read. */
function shell(routes: Record<string, Handler> = {}, path = '/connections') {
  return start(path, { ...APPS, 'GET /v1/whoami': MEMBER, ...routes }, signedIn());
}

function linksIn(group: string): string[] {
  const nav = screen.getByRole('navigation', { name: 'Main' });
  return within(within(nav).getByRole('group', { name: group }))
    .getAllByRole('link')
    .map((a) => a.textContent ?? '');
}

describe('the shell', () => {
  it('groups the navigation in a sidebar, with the admin pages for admins only', async () => {
    start('/connections', { ...APPS, 'GET /v1/whoami': WHOAMI }, signedIn());
    const nav = await screen.findByRole('navigation', { name: 'Main' });
    await within(nav).findByRole('group', { name: 'Admin' });
    expect(linksIn('Workspace')).toEqual(['Apps', 'Approvals']);
    expect(linksIn('Data and network')).toEqual(['Connections', 'Internet access']);
    expect(linksIn('Admin')).toEqual(['Your environment', 'People', 'Audit log']);
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Apps',
      'Approvals',
      'Connections',
      'Internet access',
      'Your environment',
      'People',
      'Audit log',
      'Help',
    ]);
    expect(within(nav).getByRole('link', { name: 'Connections' }).getAttribute('aria-current')).toBe('page');
    expect(within(nav).getByRole('link', { name: 'Apps' }).getAttribute('aria-current')).toBeNull();
    // Every item carries an icon drawn in the page, and none is read out.
    for (const link of within(nav).getAllByRole('link')) {
      expect(link.querySelector('svg[aria-hidden="true"]'), link.textContent ?? '').not.toBeNull();
    }
  });

  it('shows a member no admin group', async () => {
    shell();
    const nav = await screen.findByRole('navigation', { name: 'Main' });
    await screen.findByRole('button', { name: /Signed in as dev-member/ });
    expect(within(nav).queryByRole('group', { name: 'Admin' })).toBeNull();
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Apps',
      'Approvals',
      'Connections',
      'Internet access',
      'Help',
    ]);
  });

  it('shows the organisation under the brand', async () => {
    shell();
    expect((await screen.findByText('org_ffffffffffffffffffff')).closest('aside')).not.toBeNull();
    expect(screen.getByRole('link', { name: 'SSC console' }).getAttribute('href')).toBe('/');
  });

  it('says where the page sits, with the last step as the current page', async () => {
    shell();
    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' });
    const steps = within(crumbs).getAllByRole('listitem');
    expect(steps.map((li) => li.textContent)).toEqual(['Connections']);
    expect(steps[0]?.getAttribute('aria-current')).toBe('page');
  });

  it('gives a way past the sidebar to the page', async () => {
    shell();
    const skip = await screen.findByRole('link', { name: 'Skip to the page' });
    expect(skip.getAttribute('href')).toBe('#main');
    expect(screen.getByRole('main').id).toBe('main');
  });
});

describe('the approvals count', () => {
  it('counts the requests waiting for the signed-in person beside "Approvals"', async () => {
    const { api } = shell({
      'GET /v1/approvals': inbox([request('apr_1', 'pending'), request('apr_2', 'pending'), request('apr_3', 'approved')]),
    });
    const link = await screen.findByRole('link', { name: 'Approvals, 2 waiting for you' });
    expect(link.getAttribute('href')).toBe('/approvals');
    expect(link.querySelector('.nav-count')?.textContent).toBe('2');
    const reads = api.of('GET', '/v1/approvals');
    expect(reads).toHaveLength(1);
    expect([...reads[0]!.query.entries()]).toEqual([['inbox', 'true']]);
  });

  it('says when older requests were not counted', async () => {
    shell({ 'GET /v1/approvals': inbox([request('apr_1', 'pending')], 'apr_1') });
    expect(await screen.findByRole('link', { name: 'Approvals, 1+ waiting for you' })).toBeTruthy();
  });

  it('shows no count when nothing waits', async () => {
    const { api } = shell({ 'GET /v1/approvals': inbox([request('apr_3', 'denied')]) });
    await waitFor(() => expect(api.of('GET', '/v1/approvals')).toHaveLength(1));
    await screen.findByRole('button', { name: /Signed in as/ });
    expect(screen.getByRole('link', { name: 'Approvals' }).querySelector('.nav-count')).toBeNull();
  });

  it('is the same read as the Approvals page: one request, both places', async () => {
    const { api } = shell(
      { 'GET /v1/approvals': inbox([request('apr_1', 'pending'), request('apr_2', 'pending')]) },
      '/approvals',
    );
    await screen.findByText('2 requests waiting for you');
    expect(await screen.findByRole('link', { name: 'Approvals, 2 waiting for you' })).toBeTruthy();
    expect(api.of('GET', '/v1/approvals')).toHaveLength(1);
  });
});

describe('the drawer', () => {
  function parts() {
    const sidebar = document.getElementById('sidebar')!;
    const main = screen.getByRole('main', { hidden: true }).closest('.main')!;
    return { sidebar, main };
  }

  it('opens from the menu button, takes the focus, and holds the page still behind it', async () => {
    shell();
    const open = await screen.findByRole('button', { name: 'Open menu' });
    expect(open.getAttribute('aria-expanded')).toBe('false');
    expect(open.getAttribute('aria-controls')).toBe('sidebar');
    expect(parts().sidebar.classList.contains('open')).toBe(false);
    expect(parts().main.hasAttribute('inert')).toBe(false);

    fireEvent.click(open);
    expect(parts().sidebar.classList.contains('open')).toBe(true);
    expect(parts().main.hasAttribute('inert')).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close menu' }));
    expect(document.querySelector('.scrim')).not.toBeNull();
  });

  it('closes on Escape and gives the focus back to the menu button', async () => {
    shell();
    fireEvent.click(await screen.findByRole('button', { name: 'Open menu' }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(parts().sidebar.classList.contains('open')).toBe(false);
    expect(parts().main.hasAttribute('inert')).toBe(false);
    const open = screen.getByRole('button', { name: 'Open menu' });
    expect(open.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(open);
    expect(document.querySelector('.scrim')).toBeNull();
  });

  it('closes from its own button and from a click on the page behind it', async () => {
    shell();
    fireEvent.click(await screen.findByRole('button', { name: 'Open menu' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close menu' }));
    expect(parts().sidebar.classList.contains('open')).toBe(false);
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open menu', hidden: true }));

    fireEvent.click(screen.getByRole('button', { name: 'Open menu' }));
    expect(parts().sidebar.classList.contains('open')).toBe(true);
    fireEvent.click(document.querySelector('.scrim')!);
    expect(parts().sidebar.classList.contains('open')).toBe(false);
  });

  it('closes when a link in it is followed', async () => {
    const { router } = shell({ 'GET /v1/egress': () => json(200, { outbound_ip: null, rules: [] }) });
    fireEvent.click(await screen.findByRole('button', { name: 'Open menu' }));
    const nav = screen.getByRole('navigation', { name: 'Main' });
    await act(async () => {
      fireEvent.click(within(nav).getByRole('link', { name: 'Help' }));
    });
    await waitFor(() => expect(router.state.location.pathname).toBe('/help'));
    await waitFor(() => expect(parts().sidebar.classList.contains('open')).toBe(false));
    expect(parts().main.hasAttribute('inert')).toBe(false);
  });
});

describe('the user menu', () => {
  async function openMenu() {
    const button = await screen.findByRole('button', { name: /Signed in as/ });
    fireEvent.click(button);
    return { button, menu: screen.getByRole('group', { name: 'Your account' }) };
  }

  it('names the person and their role, and holds the theme and Sign out', async () => {
    shell();
    const button = await screen.findByRole('button', { name: 'Signed in as dev-member, Member' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull();
    const { menu } = await openMenu();
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(within(menu).getAllByRole('radio').map((r) => (r as HTMLInputElement).value)).toEqual([
      'light',
      'dark',
      'system',
    ]);
    expect(within(menu).getByRole('button', { name: 'Sign out' })).toBeTruthy();
  });

  it('calls an admin an org admin', async () => {
    start('/connections', { ...APPS, 'GET /v1/whoami': WHOAMI }, signedIn());
    expect(await screen.findByRole('button', { name: 'Signed in as dev-admin, Org admin' })).toBeTruthy();
  });

  it('closes on Escape, with the focus back on its button', async () => {
    shell();
    const { button } = await openMenu();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('group', { name: 'Your account' })).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it('signs out from the menu', async () => {
    const { session, router } = shell();
    const { menu } = await openMenu();
    fireEvent.click(within(menu).getByRole('button', { name: 'Sign out' }));
    expect(session.token()).toBeNull();
    await waitFor(() => expect(router.state.location.pathname).toBe('/login'));
  });
});

describe('the theme', () => {
  const root = () => document.documentElement;

  async function choose(label: string) {
    fireEvent.click(await screen.findByRole('button', { name: /Signed in as/ }));
    fireEvent.click(within(screen.getByRole('group', { name: 'Theme' })).getByRole('radio', { name: label }));
  }

  function checked(): string | undefined {
    const radios = within(screen.getByRole('group', { name: 'Theme' })).getAllByRole('radio') as HTMLInputElement[];
    return radios.find((r) => r.checked)?.value;
  }

  it('follows the system until a person chooses', async () => {
    shell();
    fireEvent.click(await screen.findByRole('button', { name: /Signed in as/ }));
    expect(checked()).toBe('system');
    expect(root().hasAttribute('data-theme')).toBe(false);
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
  });

  it('applies the choice to the root element and remembers it', async () => {
    shell();
    await choose('Dark');
    expect(root().getAttribute('data-theme')).toBe('dark');
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark');
    expect(checked()).toBe('dark');

    fireEvent.click(within(screen.getByRole('group', { name: 'Theme' })).getByRole('radio', { name: 'Light' }));
    expect(root().getAttribute('data-theme')).toBe('light');
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light');

    fireEvent.click(within(screen.getByRole('group', { name: 'Theme' })).getByRole('radio', { name: 'System' }));
    expect(root().hasAttribute('data-theme')).toBe(false);
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
  });

  it('shows a remembered choice as the one chosen', async () => {
    window.localStorage.setItem(THEME_KEY, 'dark');
    shell();
    fireEvent.click(await screen.findByRole('button', { name: /Signed in as/ }));
    expect(checked()).toBe('dark');
  });

  // The host's policy allows no inline script, so the choice is applied before the first paint by
  // a file of the console's own origin, loaded from the document's head.
  describe('before the first paint', () => {
    const CONSOLE = join(import.meta.dirname, '..');
    const script = readFileSync(join(CONSOLE, 'public/theme.js'), 'utf8');
    const run = () => new Function(script)();

    it('is loaded from the head as a file, ahead of the app, with no inline script', () => {
      const html = readFileSync(join(CONSOLE, 'index.html'), 'utf8');
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const scripts = [...doc.querySelectorAll('script')];
      expect(scripts.map((s) => s.getAttribute('src'))).toEqual(['/theme.js', '/src/main.tsx']);
      expect(scripts.every((s) => (s.textContent ?? '').trim() === '')).toBe(true);
      expect(scripts[0]?.parentElement?.tagName).toBe('HEAD');
      expect(scripts[0]?.hasAttribute('async') || scripts[0]?.hasAttribute('defer')).toBe(false);
      expect(scripts[0]?.getAttribute('type')).toBeNull();
      expect(doc.querySelector('[style]')).toBeNull();
    });

    it('reads the key the menu writes', () => {
      expect(script).toContain(`'${THEME_KEY}'`);
    });

    it.each(['light', 'dark'] as const)('applies a remembered %s', (choice) => {
      window.localStorage.setItem(THEME_KEY, choice);
      run();
      expect(root().getAttribute('data-theme')).toBe(choice);
    });

    it('leaves the system setting to decide when nothing, or nonsense, is remembered', () => {
      run();
      expect(root().hasAttribute('data-theme')).toBe(false);
      window.localStorage.setItem(THEME_KEY, 'sepia');
      run();
      expect(root().hasAttribute('data-theme')).toBe(false);
    });
  });
});
