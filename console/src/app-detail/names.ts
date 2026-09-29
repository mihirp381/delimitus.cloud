import type { EnvironmentOut } from '../api/lifecycle';

export const ENV_TITLE: Readonly<Record<EnvironmentOut['name'], string>> = {
  prod: 'Production',
  preview: 'Preview',
};
