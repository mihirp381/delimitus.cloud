import { describe, expect, it } from 'vitest';
import { itTicket, PROVIDERS, parentZone, providerOf, providerSteps, recordLines } from '../src/domain/providers';

const SUFFIX = 'tools.example.com';
const NS = ['ns-cloud-a1.googledomains.com', 'ns-cloud-a2.googledomains.com'];

describe('DNS providers', () => {
  it.each(PROVIDERS)('%s: a short numbered list naming the zone and the label', (provider) => {
    const steps = providerSteps(provider, SUFFIX);
    expect(steps.length).toBeGreaterThanOrEqual(3);
    expect(steps.length).toBeLessThanOrEqual(5);
    expect(steps.join(' ')).toContain('example.com');
    expect(steps.join(' ')).toMatch(/\btools\b/);
  });

  it('gives an unknown or missing provider the generic steps', () => {
    expect(providerOf('dnsimple')).toBe('other');
    expect(providerOf(null)).toBe('other');
    expect(providerSteps(undefined, SUFFIX)).toEqual(providerSteps('other', SUFFIX));
    expect(providerSteps('cloudflare', SUFFIX)).not.toEqual(providerSteps('other', SUFFIX));
  });

  it('names the parent zone', () => {
    expect(parentZone('tools.example.co.uk')).toBe('example.co.uk');
  });

  it('writes one record per name server and a ticket that says nothing else changes', () => {
    expect(recordLines(SUFFIX, NS)).toBe(
      'tools.example.com NS ns-cloud-a1.googledomains.com\ntools.example.com NS ns-cloud-a2.googledomains.com',
    );
    const ticket = itTicket(SUFFIX, NS);
    expect(ticket).toContain('DNS zone example.com');
    expect(ticket).toContain(recordLines(SUFFIX, NS));
    expect(ticket).toContain('Nothing else changes');
  });
});
