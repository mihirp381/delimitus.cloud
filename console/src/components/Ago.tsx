const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "3 h ago": how long since `value`, to the nearest unit a person would say. */
export function ago(value: string, now: number = Date.now()): string {
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return value;
  const past = now - then;
  const d = Math.abs(past);
  const text =
    d < MINUTE
      ? 'just now'
      : d < HOUR
        ? `${Math.floor(d / MINUTE)} min`
        : d < DAY
          ? `${Math.floor(d / HOUR)} h`
          : d < 60 * DAY
            ? `${Math.floor(d / DAY)} d`
            : d < 730 * DAY
              ? `${Math.floor(d / (30 * DAY))} mo`
              : `${Math.floor(d / (365 * DAY))} y`;
  if (text === 'just now') return text;
  return past >= 0 ? `${text} ago` : `in ${text}`;
}

/** The exact time as the console writes it everywhere else. */
export function exact(value: string): string {
  return new Date(value).toLocaleString();
}

/**
 * A time that is secondary to its row: shown as how long ago, with the exact time in the title.
 * Where the exact time is the point (a timer's history) the page writes it out.
 */
export function Ago({ at }: { readonly at: string }) {
  return (
    <time dateTime={at} title={exact(at)}>
      {ago(at)}
    </time>
  );
}
