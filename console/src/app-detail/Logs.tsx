import { useRouteContext } from '@tanstack/react-router';
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { AppOut, EnvironmentOut } from '../api/lifecycle';
import {
  appended,
  EMPTY_AGAIN_MS,
  FIRST_LINES,
  LOG_SOURCES,
  type LogLine,
  type LogSource,
  logsAfter,
  MAX_LINES,
  MAX_RETRY_AFTER_S,
  MIN_FOLLOW_GAP_MS,
  newestLogs,
  severityTone,
  SOURCE_TITLE,
} from '../api/logs';
import { ApiProblem } from '../api/problem';
import { Button } from '../components/Button';
import { ProblemNotice } from '../components/ProblemNotice';
import { ENV_TITLE } from './names';

interface Props {
  readonly app: AppOut;
  readonly env: EnvironmentOut;
}

interface Row extends LogLine {
  /** A line has no id of its own; this one is its place in what this page has read. */
  readonly n: number;
}

/** Resolves after `ms`, or at once when `signal` aborts. */
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener('abort', done);
  });
}

/** Resolves once the tab is on show, or at once when `signal` aborts. */
function onShow(signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted || document.visibilityState === 'visible') return resolve();
    const done = () => {
      if (!signal.aborted && document.visibilityState !== 'visible') return;
      document.removeEventListener('visibilitychange', done);
      signal.removeEventListener('abort', done);
      resolve();
    };
    document.addEventListener('visibilitychange', done);
    signal.addEventListener('abort', done);
  });
}

/**
 * An environment's logs, read only once the section is opened: the newest lines of one source,
 * then the lines that follow for as long as the section is open and the tab is on show. Builders
 * of the app, its owner and org admins; the API refuses anyone else.
 */
export function Logs({ app, env }: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>Logs</summary>
      {open ? <LogsBody app={app} env={env} /> : null}
    </details>
  );
}

function LogsBody({ app, env }: Props) {
  const { api } = useRouteContext({ from: '/_authed/apps/$appId' });
  const [source, setSource] = useState<LogSource>('app');
  const [rows, setRows] = useState<readonly Row[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [dropped, setDropped] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  const sourceId = useId();
  const box = useRef<HTMLDivElement>(null);
  /** Whether the reader is at the newest line; only then does a new line scroll the box. */
  const atEnd = useRef(true);

  useEffect(() => {
    const abort = new AbortController();
    const { signal } = abort;
    const where = { appId: app.id, environmentId: env.id, source };
    let count = 0;
    const add = (lines: readonly LogLine[]) => {
      const more = lines.map((line) => ({ ...line, n: count++ }));
      setRows((old) => {
        if (old.length + more.length > MAX_LINES) setDropped(true);
        return appended(old, more);
      });
    };
    setRows([]);
    setLoaded(false);
    setDropped(false);
    setError(null);
    atEnd.current = true;

    async function follow() {
      try {
        let page = await newestLogs(api, where, signal);
        if (signal.aborted) return;
        add(page.lines);
        setLoaded(true);
        let cursor = page.cursor;
        while (!signal.aborted) {
          // Without a cursor there was nothing to follow from yet: after a while, read the newest again.
          if (cursor === null) await pause(EMPTY_AGAIN_MS, signal);
          await onShow(signal);
          if (signal.aborted) return;
          const asked = Date.now();
          try {
            page = cursor === null ? await newestLogs(api, where, signal) : await logsAfter(api, where, cursor, signal);
          } catch (e) {
            if (signal.aborted) return;
            if (!(e instanceof ApiProblem) || e.code !== 'LOGS_RATE_LIMITED') throw e;
            await pause(Math.min(e.retryAfter ?? MAX_RETRY_AFTER_S, MAX_RETRY_AFTER_S) * 1000, signal);
            continue;
          }
          if (signal.aborted) return;
          add(page.lines);
          cursor = page.cursor ?? cursor;
          const spent = Date.now() - asked;
          if (page.lines.length === 0 && spent < MIN_FOLLOW_GAP_MS) await pause(MIN_FOLLOW_GAP_MS - spent, signal);
        }
      } catch (e) {
        if (!signal.aborted) setError(e);
      }
    }
    void follow();
    return () => abort.abort();
  }, [api, app.id, env.id, source, attempt]);

  useLayoutEffect(() => {
    const el = box.current;
    if (el && atEnd.current) el.scrollTop = el.scrollHeight;
  }, [rows]);

  return (
    <div className="stack">
      <div className="field">
        <label htmlFor={sourceId}>Source</label>
        <select id={sourceId} value={source} onChange={(e) => setSource(e.target.value as LogSource)}>
          {LOG_SOURCES.map((s) => (
            <option key={s} value={s}>
              {SOURCE_TITLE[s]}
            </option>
          ))}
        </select>
      </div>
      {error ? (
        <>
          <ProblemNotice error={error} />
          <div className="toolbar">
            <Button onClick={() => setAttempt((n) => n + 1)}>Read the logs again</Button>
          </div>
        </>
      ) : !loaded ? (
        <p className="muted">Reading the logs…</p>
      ) : (
        <p className="muted" role="status">
          {rows.length === 0
            ? 'No lines in the last hour. New ones show here as they are written.'
            : `The newest ${FIRST_LINES} lines of the last hour, then each new line as it is written.`}
          {dropped ? ` Only the newest ${MAX_LINES} are kept here.` : ''} Secrets are redacted before a line leaves
          the cell.
        </p>
      )}
      {rows.length > 0 ? (
        <div
          ref={box}
          className="logs"
          role="log"
          aria-label={`Logs of ${ENV_TITLE[env.name]}`}
          tabIndex={0}
          onScroll={(e) => {
            const el = e.currentTarget;
            atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
        >
          {rows.map((line) => (
            <div key={line.n} className={`log-line log-${severityTone(line.severity)}`}>
              <time dateTime={line.timestamp}>{new Date(line.timestamp).toLocaleTimeString()}</time>
              <span className="log-severity">{line.severity}</span>
              {/* Log text is written by the app and its users: shown as text, never as markup. */}
              <span className="log-text">{line.text}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
