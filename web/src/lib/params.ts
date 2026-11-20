// Helpers for the params form generated from a Flow's params_schema.
import type { ParamSpec } from '../api';

/** Raw form values are kept as strings (text inputs) or booleans (checkboxes). */
export type RawValue = string | boolean;
export type RawValues = Record<string, RawValue>;

export function initialRawValues(schema: Record<string, ParamSpec>, given?: Record<string, any>): RawValues {
  const out: RawValues = {};
  for (const [name, spec] of Object.entries(schema || {})) {
    const v = given && name in given ? given[name] : spec.default;
    if (spec.type === 'bool') out[name] = v === undefined || v === null ? false : toBool(v);
    else out[name] = v === undefined || v === null ? '' : String(v);
  }
  return out;
}

function toBool(v: unknown): boolean {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'number') return v !== 0;
  if (typeof v === 'string') return ['true', '1', 'yes', 'on', 'y', '是'].includes(v.trim().toLowerCase());
  return Boolean(v);
}

export type CoerceResult = { values: Record<string, any>; errors: Record<string, string> };

/**
 * Convert raw form values to typed params according to the schema.
 * Empty, non-required fields are omitted so that server-side defaults apply.
 */
export function coerceParams(schema: Record<string, ParamSpec>, raw: RawValues): CoerceResult {
  const values: Record<string, any> = {};
  const errors: Record<string, string> = {};
  for (const [name, spec] of Object.entries(schema || {})) {
    const r = raw[name];
    if (spec.type === 'bool') {
      values[name] = toBool(r ?? false);
      continue;
    }
    const s = typeof r === 'string' ? r.trim() : r === undefined ? '' : String(r);
    if (s === '') {
      if (spec.required && spec.default === undefined) errors[name] = '必填';
      continue;
    }
    if (spec.type === 'int') {
      if (!/^[-+]?\d+$/.test(s)) {
        errors[name] = '需要整数';
        continue;
      }
      values[name] = parseInt(s, 10);
    } else if (spec.type === 'float') {
      const n = Number(s);
      if (!Number.isFinite(n)) {
        errors[name] = '需要数字';
        continue;
      }
      values[name] = n;
    } else {
      // str: keep untrimmed original text
      values[name] = typeof r === 'string' ? r : s;
    }
  }
  return { values, errors };
}

/** Coerce a free-form value string (key/value rows in the task form) into JSON-ish types. */
export function guessValue(s: string): any {
  const t = s.trim();
  if (t === 'true') return true;
  if (t === 'false') return false;
  if (t !== '' && /^[-+]?\d+(\.\d+)?$/.test(t)) return Number(t);
  return s;
}
