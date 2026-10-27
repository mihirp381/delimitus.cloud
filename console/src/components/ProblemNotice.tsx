import { ApiProblem } from '../api/problem';
import { Notice } from './Notice';

/** Shows why a call failed: the API's title and detail, and the request id to quote. */
export function ProblemNotice({ error }: { readonly error: unknown }) {
  if (error instanceof ApiProblem) {
    return (
      <Notice tone="danger">
        <p>
          <strong>{error.title}</strong>
          {error.code ? <span className="muted"> ({error.code})</span> : null}
        </p>
        {error.detail ? <p>{error.detail}</p> : null}
        {error.requestId ? (
          <p className="muted">
            Request id <code>{error.requestId}</code>
          </p>
        ) : null}
      </Notice>
    );
  }
  const message =
    error instanceof TypeError
      ? 'The API could not be reached.'
      : error instanceof Error
        ? error.message
        : 'Something went wrong.';
  return (
    <Notice tone="danger">
      <p>{message}</p>
    </Notice>
  );
}
