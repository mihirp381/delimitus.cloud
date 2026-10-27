interface PersonProps {
  readonly name: string | null | undefined;
  readonly email?: string | null;
  readonly fallback?: string;
}

export function Person({ name, email, fallback = 'No one' }: PersonProps) {
  if (!name) return <span className="muted">{fallback}</span>;
  return (
    <span className="person" title={email ?? undefined}>
      {name}
    </span>
  );
}
