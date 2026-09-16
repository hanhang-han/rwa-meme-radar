import { readFileSync, writeFileSync, mkdirSync, renameSync } from 'node:fs';
import { dirname } from 'node:path';
export function readSnapshot<T>(file: string): T | null {
  try {
    return JSON.parse(readFileSync(file, 'utf8'), (key, value) => {
      if ((key === 'volumeRaw' || key === 'lastPriceWei') && typeof value === 'string' && /^\d+$/.test(value)) return BigInt(value);
      return value;
    }) as T;
  } catch { return null; }
}
export function writeSnapshot(file: string, value: unknown) {
  mkdirSync(dirname(file), { recursive: true });
  const temp = file + '.tmp';
  writeFileSync(temp, JSON.stringify(value, (_key, v) => typeof v === 'bigint' ? v.toString() : v));
  renameSync(temp, file);
}
