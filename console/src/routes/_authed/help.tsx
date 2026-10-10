import { createFileRoute } from '@tanstack/react-router';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Badge } from '../../components/Badge';
import { Button } from '../../components/Button';
import { PageHeader } from '../../components/PageHeader';
import { sscAnswers, supportEmail, trustUrl } from '../../help';

export const Route = createFileRoute('/_authed/help')({
  component: HelpPage,
});

function HelpPage() {
  // Read as the page renders: both are fixed when the console is built.
  const email = supportEmail(import.meta.env.VITE_SSC_SUPPORT_EMAIL);
  const trust = trustUrl(import.meta.env.VITE_SSC_TRUST_URL);
  return (
    <>
      <PageHeader
        title="Help"
        purpose="How to reach support, and whether SSC is answering right now."
      />
      <section className="panel" aria-labelledby="help-support">
        <h2 id="help-support">Support</h2>
        {email ? (
          <p>
            Write to <a href={`mailto:${email}`}>{email}</a>.
          </p>
        ) : (
          <p>Ask your SSC contact.</p>
        )}
        <p className="muted">
          If an action failed, include the request id shown under the error: it lets us find
          exactly what happened. Never send a password, a token or a secret&apos;s value.
        </p>
      </section>
      {trust ? (
        <section className="panel" aria-labelledby="help-trust">
          <h2 id="help-trust">Trust pack</h2>
          <p>
            How SSC is built and run, what it keeps and who can reach it:{' '}
            <a href={trust} target="_blank" rel="noreferrer noopener">
              open the trust pack
            </a>
            .
          </p>
        </section>
      ) : null}
      <Status />
    </>
  );
}

interface Checked {
  readonly answering: boolean;
  readonly at: Date;
}

/** One small read of the API, made when the page opens and again on request. */
function Status() {
  const { api } = Route.useRouteContext();
  const [checked, setChecked] = useState<Checked | null>(null);
  const [checking, setChecking] = useState(false);
  const latest = useRef(0);

  const check = useCallback(async () => {
    const request = ++latest.current;
    setChecking(true);
    const answering = await sscAnswers(api);
    // An answer that lands after a newer check began, or after the page was left, is dropped.
    if (request !== latest.current) return;
    setChecked({ answering, at: new Date() });
    setChecking(false);
  }, [api]);

  useEffect(() => {
    void check();
    return () => {
      latest.current += 1;
    };
  }, [check]);

  return (
    <section className="panel" aria-labelledby="help-status">
      <h2 id="help-status">Status</h2>
      <p role="status" aria-label="SSC status">
        {checked ? (
          <>
            <Badge tone={checked.answering ? 'success' : 'danger'}>
              {checked.answering ? 'SSC is answering' : 'SSC is not answering'}
            </Badge>{' '}
            <span className="muted">
              Checked at <time dateTime={checked.at.toISOString()}>{checked.at.toLocaleTimeString()}</time>.
            </span>
          </>
        ) : (
          <span className="muted">Checking…</span>
        )}
      </p>
      {checked && !checked.answering ? (
        <p className="muted">
          The console could not get an answer from SSC. Check your own connection, then check
          again; if it stays so, tell support. Apps that are already running do not depend on
          this page.
        </p>
      ) : null}
      <div className="toolbar">
        <Button onClick={() => void check()} disabled={checking}>
          {checking ? 'Checking…' : 'Check again'}
        </Button>
      </div>
    </section>
  );
}
