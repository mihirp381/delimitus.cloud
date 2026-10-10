interface Props {
  readonly value: number;
  readonly max: number;
  /** What the meter says, in words: "10 of 10 database places used". */
  readonly label: string;
  /** The warning tone: nothing, or next to nothing, is left. */
  readonly warning?: boolean;
}

/**
 * How much of something is used, as a bar. It is drawn as an SVG whose width is an attribute, so
 * no inline style is needed (the console's CSP allows none). The figure beside it says the same.
 */
export function Meter({ value, max, label, warning }: Props) {
  const share = max > 0 ? Math.max(0, Math.min(100, Math.round((value / max) * 100))) : 0;
  return (
    <svg className={warning ? 'meter meter-warning' : 'meter'} role="img" aria-label={label}>
      <rect width={`${share}%`} height="100%" rx="3" />
    </svg>
  );
}
