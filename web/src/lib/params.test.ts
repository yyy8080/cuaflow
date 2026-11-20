import { describe, expect, it } from 'vitest';
import type { ParamSpec } from '../api';
import { coerceParams, guessValue, initialRawValues } from './params';

const schema: Record<string, ParamSpec> = {
  month: { type: 'str', required: true },
  count: { type: 'int', default: 3 },
  ratio: { type: 'float' },
  headless: { type: 'bool', default: true },
  note: { type: 'str', default: 'x' },
};

describe('initialRawValues', () => {
  it('uses defaults and string-ifies non-bool values', () => {
    expect(initialRawValues(schema)).toEqual({ month: '', count: '3', ratio: '', headless: true, note: 'x' });
  });
  it('prefers given values', () => {
    expect(initialRawValues(schema, { count: 7, headless: 'false' })).toMatchObject({ count: '7', headless: false });
  });
});

describe('coerceParams', () => {
  it('coerces types', () => {
    const r = coerceParams(schema, { month: '2024-10', count: ' 12 ', ratio: '0.5', headless: false, note: ' keep ' });
    expect(r.errors).toEqual({});
    expect(r.values).toEqual({ month: '2024-10', count: 12, ratio: 0.5, headless: false, note: ' keep ' });
  });

  it('reports required and type errors', () => {
    const r = coerceParams(schema, { month: '', count: '1.5', ratio: 'abc', headless: true, note: '' });
    expect(r.errors).toEqual({ month: '必填', count: '需要整数', ratio: '需要数字' });
  });

  it('omits empty optional fields so server defaults apply', () => {
    const r = coerceParams(schema, { month: 'm', count: '', ratio: '', headless: true, note: '' });
    expect(r.values).toEqual({ month: 'm', headless: true });
  });

  it('required field with a default is not an error when empty', () => {
    const r = coerceParams({ a: { type: 'int', required: true, default: 1 } }, { a: '' });
    expect(r.errors).toEqual({});
  });
});

describe('guessValue', () => {
  it('guesses JSON-ish types', () => {
    expect(guessValue('true')).toBe(true);
    expect(guessValue('false')).toBe(false);
    expect(guessValue('42')).toBe(42);
    expect(guessValue('-1.5')).toBe(-1.5);
    expect(guessValue('2024-10')).toBe('2024-10');
    expect(guessValue('')).toBe('');
  });
});
