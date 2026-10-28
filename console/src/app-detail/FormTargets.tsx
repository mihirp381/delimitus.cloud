import { useRouteContext } from '@tanstack/react-router';
import { type FormEvent, useId, useState } from 'react';
import { must } from '../api/client';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import { Button } from '../components/Button';
import { ProblemNotice } from '../components/ProblemNotice';
import { useToast } from '../components/Toast';
import { ENV_TITLE } from './names';

const MAX = 20;

function origin(value: string): string {
  const v = value.trim().toLowerCase().replace(/\/+$/, '');
  return v.startsWith('https://') ? v : `https://${v.replace(/^[a-z]+:\/\//, '')}`;
}

export function FormTargets({ app, env }: { readonly app: AppOut; readonly env: EnvironmentOut }) {
  const { api, queries, queryClient } = useRouteContext({ from: '/_authed/apps/$appId' });
  const toast = useToast();
  const [list, setList] = useState<readonly string[]>(env.form_targets);
  const [typed, setTyped] = useState('');
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inputId = useId();
  const title = ENV_TITLE[env.name];
  const unchanged = list.length === env.form_targets.length && list.every((o, i) => o === env.form_targets[i]);

  function add(event: FormEvent) {
    event.preventDefault();
    if (!typed.trim()) return;
    const o = origin(typed);
    if (!list.includes(o)) setList([...list, o].sort());
    setTyped('');
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const out = must(
        await api.PUT('/v1/apps/{app_id}/environments/{environment_id}/form-targets', {
          params: { path: { app_id: app.id, environment_id: env.id } },
          body: { origins: [...list] },
        }),
      );
      setList(out.origins);
      toast(`Saved the form targets of ${title}.`);
      await queryClient.invalidateQueries({
        queryKey: queries.queryOptions('get', '/v1/apps/{app_id}', { params: { path: { app_id: app.id } } }).queryKey,
      });
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary aria-label={`Form targets of ${title}`}>Form targets</summary>
      {open ? (
        <div className="stack">
          <p className="muted">Hosts this app may post forms to besides itself.</p>
          {list.length === 0 ? (
            <p className="muted">None: forms post only to the app itself.</p>
          ) : (
            <ul className="plain-list" aria-label={`Form targets of ${title}`}>
              {list.map((o) => (
                <li key={o}>
                  <code>{o}</code>{' '}
                  <Button className="btn-sm" aria-label={`Remove ${o}`} onClick={() => setList(list.filter((x) => x !== o))}>
                    Remove
                  </Button>
                </li>
              ))}
            </ul>
          )}
          <form className="toolbar" onSubmit={add} aria-label={`Add a form target to ${title}`}>
            <label className="visually-hidden" htmlFor={inputId}>
              Origin, such as https://forms.example.com
            </label>
            <input
              id={inputId}
              value={typed}
              placeholder="https://forms.example.com"
              spellCheck={false}
              autoComplete="off"
              onChange={(e) => setTyped(e.target.value)}
            />
            <Button type="submit" disabled={!typed.trim() || list.length >= MAX}>
              Add
            </Button>
          </form>
          {error ? <ProblemNotice error={error} /> : null}
          <div>
            <Button disabled={busy || unchanged} onClick={() => void save()}>
              Save form targets
            </Button>
          </div>
        </div>
      ) : null}
    </details>
  );
}
