import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { APP_ID, APP_PATH, FIRST_RENDER_MS, page } from './appPage';
import { type Handler, json, problem } from './fakeApi';
import { signedIn, start } from './harness';

const GITHUB = `${APP_PATH}/github`;

const LINK = {
  app_id: APP_ID,
  repository: 'acme/expenses',
  repository_id: 4242,
  branch: 'main',
  required_checks: [{ workflow: '.github/workflows/ci.yml', name: 'test' }],
  check_name: 'ssc/preview',
  updated_at: '2026-10-01T10:00:00Z',
};

async function repository(routes: Record<string, Handler | Handler[]>) {
  const result = start(`/apps/${APP_ID}`, page(routes), signedIn());
  const panel = await screen.findByRole('region', { name: 'Repository' }, { timeout: FIRST_RENDER_MS });
  await waitFor(() => expect(within(panel).queryByText('Loading the repository…')).toBeNull());
  return { ...result, panel };
}

async function openForm(panel: HTMLElement, button: string, title: string) {
  fireEvent.click(within(panel).getByRole('button', { name: button }));
  return screen.findByRole('dialog', { name: title });
}

describe('repository', () => {
  it('takes NOT_FOUND as nothing connected', async () => {
    const { panel } = await repository({});
    expect(within(panel).getByText(/No repository is connected/)).toBeTruthy();
    expect(within(panel).queryByRole('alert')).toBeNull();
    expect(within(panel).getByRole('button', { name: 'Connect a repository' })).toBeTruthy();
  });

  it('connects a repository with its branch and required checks', async () => {
    const { api, panel } = await repository({ [`PUT ${GITHUB}`]: () => json(200, LINK) });
    const dialog = await openForm(panel, 'Connect a repository', 'Connect a repository');
    const connect = within(dialog).getByRole('button', { name: 'Connect' }) as HTMLButtonElement;
    fireEvent.change(within(dialog).getByLabelText(/^Repository/), { target: { value: 'acme' } });
    expect(connect.disabled).toBe(true);
    fireEvent.change(within(dialog).getByLabelText(/^Repository/), { target: { value: ' acme/expenses ' } });
    fireEvent.change(within(dialog).getByLabelText(/^Checks promote requires/), {
      target: { value: '.github/workflows/ci.yml test\n\n.github/workflows/lint.yaml lint all\n' },
    });
    await act(async () => {
      fireEvent.click(connect);
    });
    expect(api.of('PUT', GITHUB)[0]?.body).toEqual({
      repository: 'acme/expenses',
      branch: null,
      required_checks: [
        { workflow: '.github/workflows/ci.yml', name: 'test' },
        { workflow: '.github/workflows/lint.yaml', name: 'lint all' },
      ],
    });
    expect((await within(panel).findByRole('status')).textContent).toBe(
      'Connected acme/expenses; pushes to main deploy preview.',
    );
    expect(within(panel).getByText('ssc/preview')).toBeTruthy();
  });

  it('refuses a check line that names no workflow file, without calling the API', async () => {
    const { api, panel } = await repository({});
    const dialog = await openForm(panel, 'Connect a repository', 'Connect a repository');
    fireEvent.change(within(dialog).getByLabelText(/^Repository/), { target: { value: 'acme/expenses' } });
    fireEvent.change(within(dialog).getByLabelText(/^Checks promote requires/), { target: { value: 'test' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Connect' }));
    });
    expect((await within(dialog).findByRole('alert')).textContent).toContain('is not a workflow file');
    expect(api.of('PUT', GITHUB)).toHaveLength(0);
  });

  it('shows how to install the GitHub App when it cannot reach the repository', async () => {
    const detail = 'No GitHub App installation connected to your company can see the repository. Install the app on the account that owns it.';
    const { panel } = await repository({
      [`PUT ${GITHUB}`]: () => problem(409, 'REPOSITORY_NOT_INSTALLED', 'The GitHub App cannot reach this repository.', detail),
    });
    const dialog = await openForm(panel, 'Connect a repository', 'Connect a repository');
    fireEvent.change(within(dialog).getByLabelText(/^Repository/), { target: { value: 'acme/expenses' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Connect' }));
    });
    const alert = await within(dialog).findByRole('alert');
    expect(alert.textContent).toContain('REPOSITORY_NOT_INSTALLED');
    expect(alert.textContent).toContain(detail);
  });

  it('shows the connected repository and changes it with what it had filled in', async () => {
    const { api, panel } = await repository({
      [`GET ${GITHUB}`]: () => json(200, LINK),
      [`PUT ${GITHUB}`]: ({ body }) => json(200, { ...LINK, ...(body as object) }),
    });
    expect(within(panel).getByText('acme/expenses')).toBeTruthy();
    expect(within(panel).getByText('.github/workflows/ci.yml')).toBeTruthy();
    const dialog = await openForm(panel, 'Change', 'Change the repository');
    expect((within(dialog).getByLabelText(/^Checks promote requires/) as HTMLTextAreaElement).value).toBe(
      '.github/workflows/ci.yml test',
    );
    fireEvent.change(within(dialog).getByLabelText(/^Branch/), { target: { value: 'release' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    });
    expect(api.of('PUT', GITHUB)[0]?.body).toEqual({
      repository: 'acme/expenses',
      branch: 'release',
      required_checks: LINK.required_checks,
    });
  });

  it('disconnects only after the slug is typed', async () => {
    const { api, panel } = await repository({
      [`GET ${GITHUB}`]: () => json(200, LINK),
      [`DELETE ${GITHUB}`]: () => new Response(null, { status: 204 }),
    });
    fireEvent.click(within(panel).getByRole('button', { name: 'Disconnect the repository' }));
    const dialog = await screen.findByRole('dialog', { name: 'Disconnect the repository' });
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: 'expenses' } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }));
    });
    expect(api.of('DELETE', GITHUB)).toHaveLength(1);
    expect((await within(panel).findByRole('status')).textContent).toBe('Disconnected acme/expenses.');
    expect(within(panel).getByText(/No repository is connected/)).toBeTruthy();
  });

  it('tells someone who is not a builder that the repository is not theirs to see', async () => {
    const { panel } = await repository({
      [`GET ${GITHUB}`]: () => problem(403, 'FORBIDDEN', 'You may not do this.'),
    });
    expect(within(panel).getByText('Only builders of this app can see its repository.')).toBeTruthy();
    expect(within(panel).queryByRole('alert')).toBeNull();
  });
});
