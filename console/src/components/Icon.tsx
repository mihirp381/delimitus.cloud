import type { ReactNode } from 'react';

/**
 * The console's icons: small line drawings in the text colour, inline so nothing is fetched and
 * no dependency is added. Each is decoration: the words beside it carry the meaning.
 */
const PATHS = {
  apps: (
    <>
      <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="2.5" y="9" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="9" width="4.5" height="4.5" rx="1" />
    </>
  ),
  approvals: (
    <>
      <path d="M2.5 9.5h3l1 1.5h3l1-1.5h3" />
      <path d="M2.5 9.5 4.3 3.9a1 1 0 0 1 1-.7h5.4a1 1 0 0 1 1 .7l1.8 5.6v2.3a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1Z" />
    </>
  ),
  connections: (
    <>
      <ellipse cx="8" cy="4" rx="5" ry="1.9" />
      <path d="M3 4v4c0 1 2.2 1.9 5 1.9s5-.9 5-1.9V4" />
      <path d="M3 8v4c0 1 2.2 1.9 5 1.9s5-.9 5-1.9V8" />
    </>
  ),
  globe: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M2.5 8h11" />
      <path d="M8 2.5c1.7 1.6 2.5 3.4 2.5 5.5S9.7 11.9 8 13.500C6.3 11.9 5.5 10.1 5.5 8S6.3 4.1 8 2.500Z" />
    </>
  ),
  server: (
    <>
      <rect x="2.5" y="2.8" width="11" height="4.2" rx="1" />
      <rect x="2.5" y="9" width="11" height="4.2" rx="1" />
      <path d="M5 4.900h.01M5 11.100h.01" />
    </>
  ),
  people: (
    <>
      <circle cx="6" cy="5.5" r="2.3" />
      <path d="M2 13c.3-2.2 1.9-3.5 4-3.500s3.7 1.3 4 3.5" />
      <path d="M10.5 3.400a2.3 2.3 0 0 1 0 4.300M11.8 9.800c1.2.5 2 1.6 2.2 3.2" />
    </>
  ),
  audit: (
    <>
      <path d="M4 2.500h5.500L12.5 5.500v7a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V3.500a1 1 0 0 1 1-1Z" />
      <path d="M9.3 2.700v3h3M5.5 8.500h5M5.5 11h3.5" />
    </>
  ),
  help: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M6.3 6.400a1.8 1.8 0 0 1 3.5.5c0 1.2-1.8 1.5-1.8 2.600M8 11.500h.01" />
    </>
  ),
  menu: <path d="M2.5 4.500h11M2.5 8h11M2.5 11.500h11" />,
  close: <path d="m4 4 8 8M12 4l-8 8" />,
  chevron: <path d="m4.5 6.5 3.5 3.5 3.5-3.5" />,
  copy: (
    <>
      <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
      <path d="M10.5 5.500v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2" />
    </>
  ),
  check: <path d="m3.5 8.5 3 3 6-6.5" />,
  search: (
    <>
      <circle cx="7" cy="7" r="4.3" />
      <path d="m10.3 10.3 3.2 3.2" />
    </>
  ),
  sun: (
    <>
      <circle cx="8" cy="8" r="2.8" />
      <path d="M8 1.800v1.400M8 12.800v1.400M1.8 8h1.400M12.8 8h1.400M3.6 3.600l1 1M11.4 11.400l1 1M12.4 3.600l-1 1M4.6 11.400l-1 1" />
    </>
  ),
  moon: <path d="M13 9.300A5.5 5.5 0 0 1 6.7 3a5.5 5.5 0 1 0 6.3 6.300Z" />,
  monitor: (
    <>
      <rect x="2" y="3" width="12" height="8" rx="1.2" />
      <path d="M6 13.500h4M8 11v2.5" />
    </>
  ),
  signOut: (
    <>
      <path d="M6.5 13.500h-3a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3" />
      <path d="M10.5 5 13.5 8l-3 3M13.5 8h-7" />
    </>
  ),
  external: <path d="M9.5 2.500h4v4M13.5 2.5 7.5 8.500M11.5 9.500v3a1 1 0 0 1-1 1h-7a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3" />,
  user: (
    <>
      <circle cx="8" cy="5.5" r="2.6" />
      <path d="M3 13.500c.4-2.5 2.4-4 5-4s4.6 1.5 5 4" />
    </>
  ),
} as const satisfies Record<string, ReactNode>;

export type IconName = keyof typeof PATHS;

export function Icon({ name, className }: { readonly name: IconName; readonly className?: string }) {
  return (
    <svg
      className={className ? `icon ${className}` : 'icon'}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
