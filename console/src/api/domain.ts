import { type ApiClient, must } from './client';
import type { components } from './schema';

export type Domain = components['schemas']['DomainOut'];
export type Delegation = components['schemas']['DelegationOut'];
export type AppMove = components['schemas']['AppMoveOut'];

export const DOMAIN_POLL_MS = 30_000;

export function flipped(domain: Domain): boolean {
  return domain.current_suffix !== domain.default_suffix;
}

export function polling(domain: Domain | undefined): boolean {
  if (!domain?.delegation || flipped(domain)) return false;
  return ['pending_dns', 'issuing', 'active'].includes(domain.delegation.state);
}

export async function requestDomain(api: ApiClient, suffix: string): Promise<Domain> {
  return must(await api.PUT('/v1/domain', { body: { suffix } }));
}

export async function flipDomain(api: ApiClient, to: 'customer' | 'default'): Promise<Domain> {
  return must(await api.POST('/v1/domain/flip', { body: { to, confirm: true } }));
}
