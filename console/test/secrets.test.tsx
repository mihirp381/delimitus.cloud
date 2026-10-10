import { act, fireEvent, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { secretCommands } from '../src/app-detail/Secrets';
import { APP_ID, APP_PATH, envPanel, openSection, page, PREVIEW, PROD } from './appPage';
import { json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const PROD_SECRETS = `${APP_PATH}/environments/${PROD}/secrets`;
const PREVIEW_SECRETS = `${APP_PATH}/environments/${PREVIEW}/secrets`;

const ITEMS = [
  { name: 'DATABASE_URL', version: '4', live_version: '4', updated_at: '2026-10-01T10:00:00Z' },
  { name: 'STRIPE_KEY', version: '3', live_version: '2', updated_at: '2026-10-08T17:30:00Z' },
  { name: 'WEBHOOK_TOKEN', version: '1', live_version: null, updated_at: '2026-10-09T08:00:00Z' },
];

const secrets = (items: unknown[], env = PROD) => () => json(200, { environment_id: env, items });

async function section(name: 'Production' | 'Preview' = 'Production') {
  return openSection(await envPanel(name), 'Secrets');
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("an environment's secrets", () => {
  it('reads nothing until the section is opened, then lists names and versions', async () => {
    const { api } = start(`/apps/${APP_ID}`, page({ [`GET ${PROD_SECRETS}`]: secrets(ITEMS) }), signedIn());
    await envPanel('Production');
    expect(api.of('GET', PROD_SECRETS)).toHaveLength(0);
    const opened = await section();
    const table = await within(opened).findByRole('table', { name: 'Secrets of Production' });
    expect(api.of('GET', PROD_SECRETS)).toHaveLength(1);
    expect(api.of('GET', PREVIEW_SECRETS)).toHaveLength(0);
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Name',
      'Latest version',
      'Live version',
      'Updated',
    ]);
    const [, database, stripe, webhook] = within(table).getAllByRole('row');
    const cells = (row: HTMLElement | undefined) => within(row!).getAllByRole('cell').map((c) => c.textContent);
    // Live and latest agree: nothing to flag.
    expect(cells(database).slice(0, 3)).toEqual(['DATABASE_URL', '4', '4']);
    expect(database?.querySelector('.badge')).toBeNull();
    // A version set since the live deployment, and one never deployed.
    expect(cells(stripe).slice(0, 3)).toEqual(['STRIPE_KEY', '3', '2 takes effect on the next deployment']);
    expect(cells(webhook).slice(0, 3)).toEqual(['WEBHOOK_TOKEN', '1', 'None takes effect on the next deployment']);
    expect(stripe?.querySelector('time')?.getAttribute('datetime')).toBe('2026-10-08T17:30:00Z');
  });

  it('says values are never shown, and has no control that sets or reads one', async () => {
    start(`/apps/${APP_ID}`, page({ [`GET ${PROD_SECRETS}`]: secrets(ITEMS) }), signedIn());
    const opened = await section();
    await within(opened).findByRole('table', { name: 'Secrets of Production' });
    expect(opened.textContent).toContain('Values are never shown anywhere: not here, not in the CLI and not by the API.');
    expect(opened.querySelector('input, textarea, select')).toBeNull();
    expect(within(opened).getAllByRole('button').map((b) => b.textContent)).toEqual(['Copy', 'Copy']);
  });

  it('shows the CLI commands for this app and environment, and copies exactly them', async () => {
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });
    start(`/apps/${APP_ID}`, page({ [`GET ${PREVIEW_SECRETS}`]: secrets([], PREVIEW) }), signedIn());
    const opened = await section('Preview');
    expect(await within(opened).findByText('Preview of expenses has no secrets.')).toBeDefined();
    const commands = [...opened.querySelectorAll('pre')].map((pre) => pre.textContent);
    expect(commands).toEqual([
      'ssc secret set expenses NAME --env preview',
      'ssc secret set expenses NAME --env preview < value.txt',
    ]);
    expect(opened.textContent).toContain('The CLI asks for the value at a hidden prompt.');
    expect(opened.textContent).toContain('Setting a name that already exists stores its next version');
    await act(async () => {
      fireEvent.click(within(opened).getByRole('button', { name: 'Copy the command: Set a secret in preview' }));
    });
    expect(writeText).toHaveBeenCalledWith('ssc secret set expenses NAME --env preview');
    expect(within(opened).getByRole('status').textContent).toBe('Copied.');
  });

  it('writes the commands the way the CLI takes them', () => {
    // packages/ssc_cli/src/ssc_cli/commands/secret.py: `ssc secret set APP NAME --env prod|preview`.
    expect(secretCommands('expenses', 'prod')).toEqual({
      prompt: 'ssc secret set expenses NAME --env prod',
      piped: 'ssc secret set expenses NAME --env prod < value.txt',
    });
  });

  it('says so when the clipboard is not there', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined });
    start(`/apps/${APP_ID}`, page({ [`GET ${PROD_SECRETS}`]: secrets([]) }), signedIn());
    const opened = await section();
    await act(async () => {
      fireEvent.click(within(opened).getAllByRole('button', { name: /Copy the command/ })[0]!);
    });
    expect(within(opened).getByRole('status').textContent).toBe('Could not copy; select the command instead.');
  });

  it('shows the API refusing someone who is not a builder, with the commands still there', async () => {
    start(
      `/apps/${APP_ID}`,
      page({ [`GET ${PROD_SECRETS}`]: () => problem(403, 'FORBIDDEN', 'Builders of this environment only') }),
      signedIn(),
    );
    const opened = await section();
    expect((await within(opened).findByRole('alert')).textContent).toContain('Builders of this environment only');
    expect(within(opened).queryByRole('table')).toBeNull();
  });
});
