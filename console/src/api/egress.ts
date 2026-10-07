import { type ApiClient, must } from './client';
import type { components } from './schema';

export type Egress = components['schemas']['EgressOut'];
export type EgressHost = components['schemas']['EgressHostOut'];
export type EgressHosts = components['schemas']['EgressHostsOut'];
export type CatalogueEntry = components['schemas']['CatalogueEntryOut'];

/**
 * Lets apps reach `host` through the egress proxy (admins only). A high-risk host is refused
 * (VALIDATION_FAILED) unless `acknowledgeHighRisk` says the admin read the warning.
 */
export async function allowHost(
  api: ApiClient,
  host: string,
  acknowledgeHighRisk: boolean,
): Promise<EgressHosts> {
  return must(
    await api.PUT('/v1/egress/hosts/{host}', {
      params: { path: { host } },
      body: { acknowledge_high_risk: acknowledgeHighRisk },
    }),
  );
}

export async function removeHost(api: ApiClient, host: string): Promise<EgressHosts> {
  return must(await api.DELETE('/v1/egress/hosts/{host}', { params: { path: { host } } }));
}
