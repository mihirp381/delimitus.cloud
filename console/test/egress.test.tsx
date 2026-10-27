import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FIRST_RENDER_MS, OWNER, whoami } from './appPage';
import { type Call, type Handler, json, problem } from './fakeApi';
import { signedIn, start, toast } from './harness';

const IP = '34.120.10.20';

function host(name: string, highRisk = false) {
  return {
    host: name,
    high_risk: highRisk,
    added_by_user_id: OWNER,
    added_by_name: 'Olive Owner',
    added_by_email: 'olive@example.com',
    approval_request_id: null,
    created_at: '2026-10-01T10:00:00Z',
  };
}

const CATALOGUE = {
  entries: [
    { host: 'api.stripe.com', high_risk: false, listed: true, note: '', purpose: 'Payments' },
    { host: 'api.openai.com', high_risk: false, listed: false, note: '', purpose: 'AI models' },
    { host: 'pastebin.com', high_risk: true, listed: false, note: 'anyone can read what is posted there.', purpose: 'Text sharing' },
  ],
};

function routes(extra: Record<string, Handler | Handler[]> = {}, role: 'admin' | 'member' = 'admin') {
  return {
    'GET /v1/whoami': whoami(role),
    'GET /v1/egress': () => json(200, { hosts: [host('api.stripe.com')], outbound_ip: IP, proxy_address: '10.0.0.5:3128' }),
    'GET /v1/egress/catalogue': () => json(200, CATALOGUE),
    ...extra,
  };
}

function listed(...names: string[]): Handler {
  return () => json(200, { hosts: names.map((n) => host(n, n === 'pastebin.com')) });
}

async function egressPage(extra: Record<string, Handler | Handler[]> = {}, role: 'admin' | 'member' = 'admin') {
  const result = start('/egress', routes(extra, role), signedIn());
  await screen.findByRole('heading', { name: 'Internet access', level: 1 }, { timeout: FIRST_RENDER_MS });
  await screen.findByText(IP);
  return result;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('internet access', () => {
  it('shows the fixed IP once, to copy, and the allowed hosts', async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    await egressPage();
    expect(document.body.textContent?.split(IP)).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Copy fixed outbound IP' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(IP));
    const hosts = screen.getByRole('table', { name: 'Hosts apps may reach' });
    expect(within(hosts).getByText('api.stripe.com')).toBeTruthy();
    expect(document.body.textContent).not.toContain('10.0.0.5');
  });

  it('names who added a host, and links the approval that did', async () => {
    const approved = { ...host('api.twilio.com'), approval_request_id: 'apr_x1' };
    const bySsc = { ...host('api.ssc.example'), added_by_user_id: null, added_by_name: null, added_by_email: null };
    await egressPage({ 'GET /v1/egress': () => json(200, { hosts: [host('api.stripe.com'), approved, bySsc], outbound_ip: IP, proxy_address: null }) });
    const hosts = screen.getByRole('table', { name: 'Hosts apps may reach' });
    const row = within(hosts).getByText('api.twilio.com').closest('tr');
    expect(row?.querySelectorAll('td')[2]?.textContent).toBe('Olive Owner by approval');
    expect(within(row!).getByRole('link', { name: 'by approval' }).getAttribute('href')).toBe('/approvals/apr_x1');
    expect(within(hosts).getByText('api.ssc.example').closest('tr')?.querySelectorAll('td')[2]?.textContent).toBe('SSC');
    const direct = within(hosts).getByText('api.stripe.com').closest('tr');
    expect(direct?.textContent).not.toContain('approval');
    expect(direct?.textContent).toContain('Olive Owner');
    expect(direct?.textContent).not.toContain(OWNER);
  });

  it('says when the cell has no fixed IP yet', async () => {
    start('/egress', routes({ 'GET /v1/egress': () => json(200, { hosts: [], outbound_ip: null, proxy_address: null }) }), signedIn());
    expect(await screen.findByText(/Not set yet/, undefined, { timeout: FIRST_RENDER_MS })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Copy fixed outbound IP' })).toBeNull();
    expect(screen.getByText(/No host is allowed yet/)).toBeTruthy();
  });

  it('picks a host from the catalogue and allows it', async () => {
    const { api } = await egressPage({ 'PUT /v1/egress/hosts/api.openai.com': listed('api.stripe.com', 'api.openai.com') });
    const catalogue = await screen.findByRole('table', { name: 'Common hosts to pick from' });
    expect(within(catalogue).queryByRole('button', { name: 'Pick api.stripe.com' })).toBeNull();
    fireEvent.click(within(catalogue).getByRole('button', { name: 'Pick api.openai.com' }));
    const form = screen.getByRole('form', { name: 'Allow a host' });
    expect((within(form).getByLabelText(/^Host/) as HTMLInputElement).value).toBe('api.openai.com');
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: 'Allow host' }));
    });
    expect(api.of('PUT', '/v1/egress/hosts/api.openai.com')[0]?.body).toEqual({ acknowledge_high_risk: false });
    expect(await toast()).toBe('Allowed api.openai.com.');
    const hosts = screen.getByRole('table', { name: 'Hosts apps may reach' });
    expect(within(hosts).getByText('api.openai.com')).toBeTruthy();
    expect(screen.getByText(IP)).toBeTruthy();
  });

  it('allows a high-risk host only once the admin accepts the risk', async () => {
    const { api } = await egressPage({ 'PUT /v1/egress/hosts/pastebin.com': listed('api.stripe.com', 'pastebin.com') });
    const form = screen.getByRole('form', { name: 'Allow a host' });
    await screen.findByRole('table', { name: 'Common hosts to pick from' });
    fireEvent.change(within(form).getByLabelText(/^Host/), { target: { value: 'Pastebin.com' } });
    expect(within(form).getByRole('note').textContent).toContain('anyone can read what is posted there.');
    const allow = within(form).getByRole('button', { name: 'Allow host' }) as HTMLButtonElement;
    expect(allow.disabled).toBe(true);
    fireEvent.click(within(form).getByRole('checkbox'));
    await act(async () => {
      fireEvent.click(allow);
    });
    expect(api.of('PUT', '/v1/egress/hosts/pastebin.com')[0]?.body).toEqual({ acknowledge_high_risk: true });
  });

  it('shows why a host is refused', async () => {
    await egressPage({ 'PUT /v1/egress/hosts/10.0.0.1': () => problem(422, 'VALIDATION_FAILED', 'The request is invalid.', 'An IP address is not a host.') });
    const form = screen.getByRole('form', { name: 'Allow a host' });
    fireEvent.change(within(form).getByLabelText(/^Host/), { target: { value: '10.0.0.1' } });
    await act(async () => {
      fireEvent.click(within(form).getByRole('button', { name: 'Allow host' }));
    });
    expect((await within(form).findByRole('alert')).textContent).toContain('An IP address is not a host.');
  });

  it('removes a host only after its name is typed', async () => {
    const { api } = await egressPage({ 'DELETE /v1/egress/hosts/api.stripe.com': listed() });
    fireEvent.click(screen.getByRole('button', { name: 'Remove api.stripe.com' }));
    const dialog = await screen.findByRole('dialog', { name: 'Remove api.stripe.com' });
    const confirm = within(dialog).getByRole('button', { name: 'Remove host' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: 'api.stripe.com' } });
    await act(async () => {
      fireEvent.click(confirm);
    });
    const call: Call | undefined = api.of('DELETE', '/v1/egress/hosts/api.stripe.com')[0];
    expect(call?.headers.get('Idempotency-Key')).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/No host is allowed yet/)).toBeTruthy());
  });

  it('shows a member the list and the IP without any way to change them', async () => {
    const { api } = await egressPage({}, 'member');
    expect(screen.getByText('Only org admins can add or remove hosts.')).toBeTruthy();
    expect(screen.queryByRole('form', { name: 'Allow a host' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove api.stripe.com' })).toBeNull();
    expect(api.of('GET', '/v1/egress/catalogue')).toHaveLength(0);
  });
});
