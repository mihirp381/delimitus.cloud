import { type ApiClient, must } from './client';
import type { components } from './schema';

export type WarmOut = components['schemas']['WarmOut'];
export type WarmEnvironment = components['schemas']['WarmEnvironmentOut'];
export type WarmGateway = components['schemas']['WarmGatewayOut'];

/** How often the gateway's state is read again while the cell deployer sets it. */
export const GATEWAY_POLL_MS = 5000;

/** What a setting adds a month, from the per-part figures the API gives (SSC-092). */
export function warmCost(warm: WarmOut, environments: number, gateway: boolean): number {
  return environments * warm.environment_monthly_usd + (gateway ? warm.gateway_monthly_usd : 0);
}

/**
 * Sets the warm option (org admins only, never in an agent session). `shown` is the monthly cost
 * the screen showed for this setting; the API refuses one that is not what it costs and audits it.
 */
export async function setWarm(
  api: ApiClient,
  environmentIds: readonly string[],
  gateway: boolean,
  shown: number,
): Promise<WarmOut> {
  return must(
    await api.PUT('/v1/warm', {
      body: { environment_ids: [...environmentIds], gateway, monthly_usd_shown: shown },
    }),
  );
}
