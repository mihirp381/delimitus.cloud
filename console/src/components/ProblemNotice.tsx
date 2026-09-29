import { ApiProblem } from '../api/problem';

/** Shows why a call failed: the API's title and detail, and the request id to quote. */
export function ProblemNotice({ error }: { readonly error: unknown }) {
  if (error instanceof ApiProblem) {
    return (
      <div className="notice notice-danger" role="alert">
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
      </div>
    );
  }
  const message =
    error instanceof TypeError
      ? 'The API could not be reached.'
      : error instanceof Error
        ? error.message
        : 'Something went wrong.';
  return (
    <div className="notice notice-danger" role="alert">
      <p>{message}</p>
    </div>
  );
}
