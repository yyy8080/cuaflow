// 基于脚本 params_schema 生成的参数表单辅助函数。
import type { ParamSpec } from '../api';

/** 表单原始值统一保存为字符串（文本/数字输入框）或布尔值（复选框）。 */
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
 * 根据参数 schema 将原始表单值转换为对应类型的参数对象。
 * 为空的非必填字段会被省略，以便服务端使用默认值。
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
      // 字符串类型：保留未裁剪空白的原始输入文本
      values[name] = typeof r === 'string' ? r : s;
    }
  }
  return { values, errors };
}

/** 将自由输入的字符串值（任务表单中的键值对行）推断转换为布尔、数值或字符串类型。 */
export function guessValue(s: string): any {
  const t = s.trim();
  if (t === 'true') return true;
  if (t === 'false') return false;
  if (t !== '' && /^[-+]?\d+(\.\d+)?$/.test(t)) return Number(t);
  return s;
}
