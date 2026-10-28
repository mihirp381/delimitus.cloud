import type { components } from './schema';

export type UserApps = components['schemas']['UserApps'];
export type UserApp = components['schemas']['UserApp'];
export type UserAppEnvironment = components['schemas']['UserAppEnvironment'];
export type AccessVia = components['schemas']['AccessVia'];

export const EVERYONE = 'Everyone in the organisation';

/** How a grant reaches the person: the org, a named group, or them by name (`direct`). */
export function viaText(via: AccessVia, direct: string): string {
  if (via.kind === 'org') return EVERYONE;
  if (via.kind === 'group') return `Group ${via.name ?? ''}`;
  return direct;
}

export function accessText(app: UserApp, direct: string): string {
  const seen = app.environments.flatMap((e) => e.via.map((v) => viaText(v, direct)));
  return [...new Set(seen)].join(', ');
}

export function isBuilder(app: UserApp): boolean {
  return app.environments.some((e) => e.role === 'builder');
}

export function matchesOpenFilter(app: UserApp, text: string, direct: string): boolean {
  const f = text.trim().toLowerCase();
  return !f || app.slug.includes(f) || accessText(app, direct).toLowerCase().includes(f);
}
