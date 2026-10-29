import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FIRST_RENDER_MS, whoami } from './appPage';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start, toast } from './harness';

const DEFAULT = 'k3x9.delimitusapps.com';
const SUFFIX = 'tools.acme.com';
const NS = ['ns-cloud-a1.googledomains.com', 'ns-cloud-a2.googledomains.com', 'ns-cloud-a3.googledomains.com', 'ns-cloud-a4.googledomains.com'];

const me: Handler = () =>
  json(200, {
    org_id: 'org_ffffffffffffffffffff',
    subject: 'dev-admin',
    kind: 'user',
    credential_id: 'c',
    is_agent: false,
    client_id: null,
    role: 'admin',
    org_name: 'Acme',
    name: 'Dev Admin',
    email: 'dev@Acme.com',
    sign_in_mode: 'directory',
  });

function delegation(state: string, extra: Record<string, unknown> = {}) {
  return {
    id: 'dom_aaaaaaaaaaaaaaaaaaaa',
    suffix: SUFFIX,
    state,
    name_servers: NS,
    observed_ns: null,
    failure: null,
    provider: 'cloudflare',
    created_at: '2026-10-28T10:00:00Z',
    updated_at: '2026-10-28T10:00:00Z',
    last_delegated_at: null,
    ...extra,
  };
}

function domain(extra: Record<string, unknown> = {}) {
  return {
    default_suffix: DEFAULT,
    current_suffix: DEFAULT,
    previous: [],
    delegation: null,
    apps_that_move: [],
    ready_to_flip: false,
    rolling: 0,
    ...extra,
  };
}

async function domainPage(handlers: Handler | Handler[], extra: Record<string, Handler | Handler[]> = {}) {
  const result = start('/domain', { 'GET /v1/whoami': me, 'GET /v1/domain': handlers, ...extra }, signedIn());
  await screen.findByRole('heading', { name: 'Your own address', level: 1 }, { timeout: FIRST_RENDER_MS });
  return result;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('your own address', () => {
  it('suggests tools. under the sign-in domain and asks for it', async () => {
    const { api } = await domainPage(() => json(200, domain()), {
      'PUT /v1/domain': () => json(200, domain({ delegation: delegation('pending_dns') })),
    });
    const form = await screen.findByRole('form', { name: 'Create your own address' });
    expect((within(form).getByLabelText('Subdomain to delegate') as HTMLInputElement).value).toBe(SUFFIX);
    expect(screen.getByText(/One DNS change by your IT team/)).toBeTruthy();
    expect(within(form).getByRole('note').textContent).toContain('dedicated sub-label');
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: 'Create address' }));
    });
    expect(api.of('PUT', '/v1/domain')[0]?.body).toEqual({ suffix: SUFFIX });
    expect(await toast()).toBe(`Asked for ${SUFFIX}.`);
    expect(await screen.findByRole('table', { name: 'NS records to add' })).toBeTruthy();
  });

  it('says what the admission rules are when a suffix is refused', async () => {
    await domainPage(() => json(200, domain()), {
      'PUT /v1/domain': () => problem(422, 'VALIDATION_FAILED', 'The request is invalid.'),
    });
    const form = await screen.findByRole('form', { name: 'Create your own address' });
    fireEvent.change(within(form).getByLabelText('Subdomain to delegate'), { target: { value: 'acme.com' } });
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: 'Create address' }));
    });
    expect(await within(form).findByText(/at least three labels/)).toBeTruthy();
  });

  it('shows why a rule refused the suffix', async () => {
    const detail = 'This name, a name above it or a name under it is used by another organization.';
    await domainPage(() => json(200, domain()), {
      'PUT /v1/domain': () => problem(409, 'DOMAIN_SUFFIX_HELD', 'Another organization uses this address.', detail),
    });
    const form = await screen.findByRole('form', { name: 'Create your own address' });
    const input = within(form).getByLabelText('Subdomain to delegate');
    fireEvent.change(input, { target: { value: 'eu.tools.acme.com' } });
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: 'Create address' }));
    });
    expect(await within(form).findByText(detail)).toBeTruthy();
    expect(within(form).queryByText(/at least three labels/)).toBeNull();
    expect(input.getAttribute('aria-invalid')).toBe('true');
  });

  it('shows the records, the ticket, the provider steps and what the internet sees', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    await domainPage(() => json(200, domain({ delegation: delegation('pending_dns', { observed_ns: { cloudflare: ['ns1.old.example'], google: [] } }) })));
    const records = await screen.findByRole('table', { name: 'NS records to add' });
    expect(within(records).getAllByRole('row')).toHaveLength(NS.length + 1);
    fireEvent.click(screen.getByRole('button', { name: 'Copy all records' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(NS.map((ns) => `${SUFFIX} NS ${ns}`).join('\n')));
    fireEvent.click(within(records).getByRole('button', { name: `Copy name server ${NS[0]}` }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(NS[0]));
    const ticket = screen.getByLabelText('Ready to paste') as HTMLTextAreaElement;
    expect(ticket.readOnly).toBe(true);
    expect(ticket.value).toContain('DNS zone acme.com');
    expect(ticket.value).toContain('Nothing else changes');
    expect(screen.getByRole('heading', { name: 'Steps in Cloudflare' })).toBeTruthy();
    expect(screen.getByText('ns1.old.example')).toBeTruthy();
  });

  it('says the zone is being set up until it has name servers', async () => {
    await domainPage(() => json(200, domain({ delegation: delegation('pending_dns', { name_servers: [] }) })));
    expect(await screen.findByRole('heading', { name: 'Setting up the zone…' })).toBeTruthy();
    expect(screen.getByText(SUFFIX)).toBeTruthy();
    expect(screen.queryByRole('table', { name: 'NS records to add' })).toBeNull();
    expect(screen.queryByLabelText('Ready to paste')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Copy/ })).toBeNull();
  });

  it('says nothing is seen yet, with the generic steps', async () => {
    await domainPage(() => json(200, domain({ delegation: delegation('pending_dns', { provider: null }) })));
    expect(await screen.findByText('Nothing yet.')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Steps in your DNS provider' })).toBeTruthy();
  });

  it('shows the issuing state with the provisioning detail', async () => {
    await domainPage(() => json(200, domain({ delegation: delegation('issuing', { failure: 'CAA check pending' }) })));
    expect(await screen.findByText(/usually under 30 minutes, sometimes up to 2 hours/)).toBeTruthy();
    expect(screen.getByText('Certificate authority says: CAA check pending')).toBeTruthy();
  });

  it('waits for apps being prepared before the move can be confirmed', async () => {
    const moves = [{ slug: 'sales', hosts: [`sales.${SUFFIX}`, `sales--preview.${SUFFIX}`] }];
    await domainPage(() => json(200, domain({ delegation: delegation('active'), apps_that_move: moves, rolling: 2 })));
    expect(await screen.findByText('2 apps are still being prepared; you can move once they are ready.')).toBeTruthy();
    expect((screen.getByRole('button', { name: `Move my apps to ${SUFFIX}` }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(screen.getByRole('table', { name: 'Apps that move' })).getByText(`sales--preview.${SUFFIX}`)).toBeTruthy();
  });

  it('moves the apps and back, each after a confirm', async () => {
    const active = delegation('active');
    const moved = domain({ delegation: active, current_suffix: SUFFIX, previous: [{ suffix: DEFAULT, until: '2026-11-27T10:00:00Z' }] });
    const back = domain({ delegation: active, ready_to_flip: true, previous: [{ suffix: SUFFIX, until: '2026-11-27T10:00:00Z' }] });
    const { api } = await domainPage(() => json(200, domain({ delegation: active, ready_to_flip: true })), {
      'POST /v1/domain/flip': [() => json(200, moved), () => json(200, back)],
    });
    fireEvent.click(await screen.findByRole('button', { name: `Move my apps to ${SUFFIX}` }));
    let dialog = await screen.findByRole('dialog', { name: `Move my apps to ${SUFFIX}` });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Move my apps' }));
    });
    const first = api.of('POST', '/v1/domain/flip')[0];
    expect(first?.body).toEqual({ to: 'customer', confirm: true });
    expect(first?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(await toast()).toBe(`Your apps now live under ${SUFFIX}.`);
    expect(screen.getByText(/redirect until/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: `Move back to ${DEFAULT}` }));
    dialog = await screen.findByRole('dialog', { name: `Move back to ${DEFAULT}` });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Move back' }));
    });
    expect(api.of('POST', '/v1/domain/flip')[1]?.body).toEqual({ to: 'default', confirm: true });
    expect(await screen.findByRole('button', { name: `Move my apps to ${SUFFIX}` })).toBeTruthy();
  });

  it('shows why it failed and tries again', async () => {
    const { api } = await domainPage(() => json(200, domain({ delegation: delegation('failed', { failure: 'no delegation seen in 72 hours' }) })), {
      'PUT /v1/domain': () => json(200, domain({ delegation: delegation('pending_dns') })),
    });
    expect(await screen.findByText(/no delegation seen in 72 hours/)).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    });
    expect(api.of('PUT', '/v1/domain')[0]?.body).toEqual({ suffix: SUFFIX });
    expect(await screen.findByRole('table', { name: 'NS records to add' })).toBeTruthy();
  });

  it('is for admins only', async () => {
    start('/domain', { 'GET /v1/whoami': whoami('member') }, signedIn());
    expect(await screen.findByText('Only org admins can set the address your apps live at.', undefined, { timeout: FIRST_RENDER_MS })).toBeTruthy();
  });
});
