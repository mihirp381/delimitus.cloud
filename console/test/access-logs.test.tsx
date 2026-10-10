import { act, fireEvent, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { type AccessExplained, accessSentence } from '../src/api/access';
import { appended, MAX_LINES, severityTone } from '../src/api/logs';
import { APP_ID, APP_PATH, envPanel, openSection, page, PROD } from './appPage';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const PERSON = 'usr_pppppppppppppppppppp';
const ACCESS = `${APP_PATH}/environments/${PROD}/access`;
const LOGS = `${APP_PATH}/environments/${PROD}/logs`;

function explained(overrides: Partial<AccessExplained> = {}): AccessExplained {
  return {
    allowed: true,
    environment_id: PROD,
    evaluated_from: 'live',
    floor: 'user',
    grants: [
      { grant_id: 'gnt_000000000000000000o1', role: 'user', subject_kind: 'org', subject_id: null, group_name: null },
      { grant_id: 'gnt_000000000000000000g1', role: 'builder', subject_kind: 'group', subject_id: 'grp_ffffffffffffffffffff', group_name: 'Finance' },
    ],
    published_version: 7,
    reason: 'granted',
    role: 'builder',
    user_id: PERSON,
    ...overrides,
  };
}

async function explainSection() {
  const panel = await envPanel('Production');
  const section = await openSection(panel, 'Why can this person open it?');
  return within(section).getByRole('form', { name: 'Explain access to Production' });
}

describe('why a person can open an environment', () => {
  it('asks nothing until a person is named, then shows the answer, the roles and the grants', async () => {
    const { api } = start(`/apps/${APP_ID}`, page({ [`GET ${ACCESS}`]: () => json(200, explained()) }), signedIn());
    const form = await explainSection();
    expect(api.of('GET', ACCESS)).toHaveLength(0);
    expect((within(form).getByRole('button', { name: 'Explain' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(form).getByLabelText('Person: email or usr_ id'), { target: { value: PERSON } });
    fireEvent.click(within(form).getByRole('button', { name: 'Explain' }));
    const answer = await within(form).findByRole('status', { name: 'Access explained' });
    expect(api.of('GET', ACCESS)[0]?.query.get('user_id')).toBe(PERSON);
    expect(within(answer).getByText('can open')).toBeTruthy();
    expect(
      within(answer).getByText(`${PERSON} can open production of expenses as builder, through the grants below.`),
    ).toBeTruthy();
    const facts = [...answer.querySelectorAll('dt')].map((dt) => [dt.textContent, dt.nextElementSibling?.textContent]);
    expect(facts).toEqual([
      ['Best role granted', 'builder'],
      ['Least role production takes', 'user'],
      ['Person', PERSON],
    ]);
    const grants = within(answer).getByRole('table', { name: 'Grants that decided access to Production' });
    expect(within(grants).getByText('Everyone in the organisation')).toBeTruthy();
    expect(within(grants).getByText('Group Finance')).toBeTruthy();
    expect(within(grants).getByText('gnt_000000000000000000g1')).toBeTruthy();
    expect(within(answer).getByText(/newest published: 7/)).toBeTruthy();
  });

  it('finds a person by email, deactivated or not, and says why they cannot open it', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        'GET /v1/users': () =>
          json(200, { users: [{ id: PERSON, email: 'pat@example.com', display_name: 'Pat Lee', status: 'deactivated', role: 'member' }] }),
        [`GET ${ACCESS}`]: () => json(200, explained({ allowed: false, reason: 'user_not_active', role: null, grants: [] })),
      }),
      signedIn(),
    );
    const form = await explainSection();
    fireEvent.change(within(form).getByLabelText('Person: email or usr_ id'), { target: { value: 'pat@example.com' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Find' }));
    const choice = (await within(form).findByRole('radio')) as HTMLInputElement;
    expect(choice.disabled).toBe(false);
    expect(within(form).getByText(/deactivated/)).toBeTruthy();
    await waitFor(() => expect(choice.checked).toBe(true));
    expect(api.of('GET', '/v1/users')[0]?.query.get('email')).toBe('pat@example.com');
    fireEvent.click(within(form).getByRole('button', { name: 'Explain' }));
    const answer = await within(form).findByRole('status', { name: 'Access explained' });
    expect(within(answer).getByText('cannot open')).toBeTruthy();
    expect(within(answer).getByText('Pat Lee cannot open production of expenses: they are deactivated.')).toBeTruthy();
    expect(within(answer).queryByRole('table')).toBeNull();
    expect(within(answer).getByText('None')).toBeTruthy();
  });

  it('shows the refusal for someone who is not a builder of the environment', async () => {
    start(
      `/apps/${APP_ID}`,
      page({ [`GET ${ACCESS}`]: () => problem(403, 'FORBIDDEN', 'Only a builder of this environment may ask.') }),
      signedIn(),
    );
    const form = await explainSection();
    fireEvent.change(within(form).getByLabelText('Person: email or usr_ id'), { target: { value: PERSON } });
    fireEvent.click(within(form).getByRole('button', { name: 'Explain' }));
    const alert = await within(form).findByRole('alert');
    expect(alert.textContent).toContain('Only a builder of this environment may ask.');
    expect(within(form).queryByRole('status', { name: 'Access explained' })).toBeNull();
  });

  it('has plain words for every reason the API can give', () => {
    const reasons: AccessExplained['reason'][] = [
      'granted',
      'below_floor',
      'no_grant',
      'app_not_active',
      'user_not_active',
      'unknown_environment',
      'no_view',
    ];
    const said = reasons.map((reason) =>
      accessSentence(explained({ reason, allowed: reason === 'granted', floor: 'builder' }), 'Pat', 'production of expenses', 'production'),
    );
    expect(new Set(said).size).toBe(reasons.length);
    for (const [i, sentence] of said.entries()) {
      expect(sentence).toMatch(i === 0 ? /^Pat can open production of expenses as builder/ : /^Pat cannot open production of expenses: /);
      expect(sentence).not.toContain(reasons[i]!);
    }
    expect(said[1]).toContain('production takes builder grants or higher');
  });
});

function line(text: string, severity = 'INFO', at = '2026-10-09T10:00:00Z') {
  return { timestamp: at, severity, source: 'app', text };
}

function logPage(lines: unknown[], cursor: string | null, source = 'app'): Handler {
  return () => json(200, { environment_id: PROD, source, lines, cursor });
}

const HANGS: Handler = () => new Promise<Response>(() => {});

function hide(hidden: boolean) {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (hidden ? 'hidden' : 'visible') });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('logs', () => {
  afterEach(() => {
    // Back to jsdom's own answer.
    Reflect.deleteProperty(document, 'visibilityState');
  });

  it('reads nothing until opened, shows the newest lines as text, then follows from the cursor', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${LOGS}`]: [
          logPage([line('GET /health 200'), line('<img src=x onerror=alert(1)> boom', 'ERROR')], '1.2.3'),
          logPage([line('worker started', 'WARNING', '2026-10-09T10:00:05Z')], '1.2.4'),
          HANGS,
        ],
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    expect(api.of('GET', LOGS)).toHaveLength(0);
    const section = await openSection(panel, 'Logs');
    const log = await within(section).findByRole('log', { name: 'Logs of Production' });
    const first = api.of('GET', LOGS)[0]!;
    expect([...first.query.entries()].sort()).toEqual([
      ['limit', '100'],
      ['source', 'app'],
    ]);
    expect(within(log).getByText('GET /health 200')).toBeTruthy();
    expect(within(log).getByText('<img src=x onerror=alert(1)> boom')).toBeTruthy();
    expect(log.querySelector('img')).toBeNull();
    expect(within(log).getByText('ERROR').closest('.log-line')?.className).toContain('log-danger');
    await within(log).findByText('worker started');
    const second = api.of('GET', LOGS)[1]!;
    expect([...second.query.entries()].sort()).toEqual([
      ['after', '1.2.3'],
      ['source', 'app'],
      ['wait', '10'],
    ]);
    await waitFor(() => expect(api.of('GET', LOGS)).toHaveLength(3));
    expect(api.of('GET', LOGS)[2]?.query.get('after')).toBe('1.2.4');
    expect(log.querySelectorAll('.log-line')).toHaveLength(3);
  });

  it('stops asking once the section is closed, and reads another source from its newest lines', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${LOGS}`]: (call) =>
          call.query.get('source') === 'build'
            ? call.query.has('after')
              ? HANGS(call)
              : logPage([line('step 3/9 done')], '9.9.9', 'build')(call)
            : logPage(call.query.has('after') ? [] : [line('hello')], '1.2.3')(call),
      }),
      signedIn(),
    );
    const panel = await envPanel('Production');
    const section = await openSection(panel, 'Logs');
    await within(section).findByText('hello');
    fireEvent.change(within(section).getByLabelText('Source'), { target: { value: 'build' } });
    await within(section).findByText('step 3/9 done');
    expect(within(section).queryByText('hello')).toBeNull();
    await waitFor(() => expect(api.of('GET', LOGS).at(-1)?.query.get('after')).toBe('9.9.9'));
    const details = section as HTMLDetailsElement;
    await act(async () => {
      details.open = false;
      details.dispatchEvent(new Event('toggle'));
    });
    const asked = api.of('GET', LOGS).length;
    await new Promise((resolve) => setTimeout(resolve, 1300));
    expect(api.of('GET', LOGS)).toHaveLength(asked);
    expect(within(section).queryByRole('log')).toBeNull();
  });

  it('does not follow while the tab is hidden, and picks up when it is shown again', async () => {
    hide(true);
    const { api } = start(
      `/apps/${APP_ID}`,
      page({ [`GET ${LOGS}`]: [logPage([line('hello')], '1.2.3'), logPage([line('back again')], '1.2.4'), HANGS] }),
      signedIn(),
    );
    const section = await openSection(await envPanel('Production'), 'Logs');
    await within(section).findByText('hello');
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(api.of('GET', LOGS)).toHaveLength(1);
    await act(async () => hide(false));
    await within(section).findByText('back again');
  });

  it('shows the refusal for someone who may not read logs, and reads again when asked', async () => {
    const { api } = start(
      `/apps/${APP_ID}`,
      page({
        [`GET ${LOGS}`]: [
          () => problem(403, 'FORBIDDEN', 'Only a builder of this environment may read its logs.'),
          logPage([], null),
          HANGS,
        ],
      }),
      signedIn(),
    );
    const section = await openSection(await envPanel('Production'), 'Logs');
    const alert = await within(section).findByRole('alert');
    expect(alert.textContent).toContain('Only a builder of this environment may read its logs.');
    expect(within(section).queryByRole('log')).toBeNull();
    fireEvent.click(within(section).getByRole('button', { name: 'Read the logs again' }));
    expect(await within(section).findByText(/No lines in the last hour/)).toBeTruthy();
    expect(api.of('GET', LOGS)).toHaveLength(2);
  });

  it('keeps only the newest lines and names what went wrong by severity', () => {
    const many = Array.from({ length: MAX_LINES }, (_, i) => i);
    const kept = appended(many, [MAX_LINES, MAX_LINES + 1]);
    expect(kept).toHaveLength(MAX_LINES);
    expect(kept[0]).toBe(2);
    expect(kept.at(-1)).toBe(MAX_LINES + 1);
    expect(appended(many, [])).toBe(many);
    expect(['ERROR', 'critical', 'WARNING', 'INFO', 'DEFAULT'].map(severityTone)).toEqual([
      'danger',
      'danger',
      'warning',
      'plain',
      'plain',
    ]);
  });
});
