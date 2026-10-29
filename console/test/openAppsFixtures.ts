import { within } from '@testing-library/react';
import { APP_ID, OWNER, PREVIEW, PROD } from './appPage';
import { type Handler, json } from './fakeApi';

export const LEDGER = 'app_llllllllllllllllllll';
export const PAT = 'usr_pppppppppppppppppppp';

function env(name: 'prod' | 'preview', role: 'user' | 'builder', via: { kind: string; name: string | null }[]) {
  return {
    environment_id: name === 'prod' ? PROD : PREVIEW,
    name,
    url: `https://expenses${name === 'prod' ? '' : '--preview'}.acme.apps.example`,
    role,
    via,
  };
}

export const EXPENSES = {
  app_id: APP_ID,
  slug: 'expenses',
  status: 'active',
  owner_state: 'active',
  owner_name: 'Olive Owner',
  owner_email: 'olive@example.com',
  environments: [env('prod', 'user', [{ kind: 'org', name: null }])],
};
export const LEDGER_APP = {
  app_id: LEDGER,
  slug: 'ledger',
  status: 'active',
  owner_state: 'active',
  owner_name: 'Olive Owner',
  owner_email: 'olive@example.com',
  environments: [
    { ...env('prod', 'user', [{ kind: 'group', name: 'Finance' }]), environment_id: 'env_lprod00000000000000000' },
    { ...env('preview', 'builder', [{ kind: 'user', name: 'Olive Owner' }]), environment_id: 'env_lprev00000000000000000' },
  ],
};

export function mine(apps: unknown[], user = OWNER): Handler {
  return () => json(200, { user_id: user, user_name: 'Olive Owner', user_status: 'active', published_version: 3, apps });
}

export const rows = (table: HTMLElement) => within(table).getAllByRole('row').slice(1);
