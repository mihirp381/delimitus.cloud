import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/**
 * Written by audit_chain_vectors.py beside this file: export files, and what the CLI's own check
 * (`ssc_cli.audit_chain.check_lines`) reports for each.
 */
export interface Vector {
  /** The export's bytes, in base64. */
  readonly file: string;
  readonly report: {
    readonly ok: boolean;
    readonly checked: number;
    readonly org_id: string | null;
    readonly first_seq: number | null;
    readonly last_seq: number | null;
    readonly last_hash: string | null;
    readonly from_genesis: boolean;
    readonly broken_line: number | null;
    readonly broken_seq: number | null;
    readonly cause: string | null;
  };
}

export const VECTORS = JSON.parse(
  readFileSync(join(import.meta.dirname, 'audit-chain.json'), 'utf8'),
) as Readonly<Record<string, Vector>>;

export function vectorBytes(name: string): Uint8Array<ArrayBuffer> {
  const vector = VECTORS[name];
  if (!vector) throw new Error(`no vector named ${name}`);
  return Uint8Array.from(atob(vector.file), (c) => c.charCodeAt(0));
}
