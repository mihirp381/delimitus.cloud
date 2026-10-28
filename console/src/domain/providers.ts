export const PROVIDERS = ['cloudflare', 'route53', 'godaddy', 'azure', 'google', 'ns1', 'other'] as const;

export type Provider = (typeof PROVIDERS)[number];

export const PROVIDER_NAME: Readonly<Record<Provider, string>> = {
  cloudflare: 'Cloudflare',
  route53: 'Amazon Route 53',
  godaddy: 'GoDaddy',
  azure: 'Azure DNS',
  google: 'Google Cloud DNS',
  ns1: 'NS1',
  other: 'your DNS provider',
};

export function providerOf(value: string | null | undefined): Provider {
  return PROVIDERS.find((p) => p === value) ?? 'other';
}

export function parentZone(suffix: string): string {
  return suffix.split('.').slice(1).join('.');
}

export function subLabel(suffix: string): string {
  return suffix.split('.')[0] ?? suffix;
}

export function providerSteps(provider: string | null | undefined, suffix: string): readonly string[] {
  const zone = parentZone(suffix);
  const label = subLabel(suffix);
  switch (providerOf(provider)) {
    case 'cloudflare':
      return [
        `In the Cloudflare dashboard, open ${zone} and go to DNS, Records.`,
        `Add a record of type NS, name ${label}, with the first name server as its content.`,
        'Add one more NS record with the same name for each other name server.',
        'Save; leave every other record as it is.',
      ];
    case 'route53':
      return [
        `In the Route 53 console, open Hosted zones and then ${zone}.`,
        `Create a record named ${label} of type NS.`,
        'Paste every name server into its value, one per line, and keep the TTL at 3600.',
        'Create the record; leave every other record as it is.',
      ];
    case 'godaddy':
      return [
        `In GoDaddy, open My Products, Domains, ${zone} and then DNS.`,
        `Add a new record of type Nameserver, host ${label}, with the first name server as its value.`,
        'Add one more record with the same host for each other name server.',
        'Save; leave every other record as it is.',
      ];
    case 'azure':
      return [
        `In the Azure portal, open DNS zones and then ${zone}.`,
        `Add a record set named ${label} of type NS.`,
        'Enter every name server, one per field, and keep the TTL at 1 hour.',
        'Select OK; leave every other record set as it is.',
      ];
    case 'google':
      return [
        `In the Google Cloud console, open Cloud DNS and then the zone for ${zone}.`,
        `Add a standard record set with DNS name ${label} and type NS.`,
        'Add every name server as its own item.',
        'Create it; leave every other record set as it is.',
      ];
    case 'ns1':
      return [
        `In the NS1 portal, open Zones and then ${zone}.`,
        `Add a record of type NS for ${label}.`,
        'Add every name server as its own answer.',
        'Save; leave every other record as it is.',
      ];
    case 'other':
      return [
        `Open the DNS settings of ${zone} where your company manages it.`,
        `Add an NS record for ${label}, once for each name server below.`,
        'If a TTL is asked for, 3600 seconds is fine.',
        'Save; leave every other record as it is.',
      ];
  }
}

export function recordLines(suffix: string, nameServers: readonly string[]): string {
  return nameServers.map((ns) => `${suffix} NS ${ns}`).join('\n');
}

export function itTicket(suffix: string, nameServers: readonly string[]): string {
  return [
    `Please add these NS records in the DNS zone ${parentZone(suffix)}:`,
    '',
    recordLines(suffix, nameServers),
    '',
    `Why: this hands ${suffix} to Delimitus, which serves our internal apps under it and issues their certificates.`,
    `Nothing else changes: no other record in ${parentZone(suffix)} is touched.`,
  ].join('\n');
}
