interface Props {
  /** What is loading, read out and not shown: "Loading apps…". */
  readonly label: string;
  /** How many placeholder rows to draw. */
  readonly rows?: number;
  /** One bar in a line of text, in place of a value. */
  readonly inline?: boolean;
}

/** Placeholder rows while a list loads, in the shape of the table that replaces them. */
export function Loading({ label, rows = 4, inline }: Props) {
  if (inline) {
    return (
      <span className="skeleton-inline" role="status">
        <span className="visually-hidden">{label}</span>
        <span className="skeleton-bar" aria-hidden="true" />
      </span>
    );
  }
  return (
    <div className="skeleton" role="status">
      <span className="visually-hidden">{label}</span>
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skeleton-row" aria-hidden="true">
          <span className="skeleton-bar" />
          <span className="skeleton-bar" />
          <span className="skeleton-bar" />
        </span>
      ))}
    </div>
  );
}
