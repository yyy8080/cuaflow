import type { ParamSpec } from '../api';
import type { RawValues } from '../lib/params';

const TYPE_LABEL: Record<string, string> = { str: '文本', int: '整数', float: '小数', bool: '布尔' };

/** 根据 params_schema 动态生成的参数表单；表单值保持原始输入态，提交前需调用 coerceParams() 转换校验。 */
export function ParamsForm({
  schema,
  values,
  errors,
  onChange,
}: {
  schema: Record<string, ParamSpec>;
  values: RawValues;
  errors?: Record<string, string>;
  onChange: (v: RawValues) => void;
}) {
  const entries = Object.entries(schema || {});
  if (!entries.length) return <div className="muted small">此脚本没有参数</div>;
  const set = (k: string, v: string | boolean) => onChange({ ...values, [k]: v });
  return (
    <div className="params-form">
      {entries.map(([name, spec]) => (
        <div className="field" key={name}>
          <label htmlFor={`p-${name}`}>
            <code>{name}</code>
            {spec.required && <span className="req">*</span>}
            <span className="type-tag">{TYPE_LABEL[spec.type] ?? spec.type}</span>
          </label>
          {spec.type === 'bool' ? (
            <label className="check">
              <input id={`p-${name}`} type="checkbox" checked={!!values[name]} onChange={(e) => set(name, e.target.checked)} />
              {spec.description || name}
            </label>
          ) : (
            <input
              id={`p-${name}`}
              className={errors?.[name] ? 'invalid' : ''}
              type={spec.type === 'str' ? 'text' : 'number'}
              step={spec.type === 'int' ? 1 : spec.type === 'float' ? 'any' : undefined}
              value={String(values[name] ?? '')}
              placeholder={spec.default !== undefined ? `默认：${String(spec.default)}` : ''}
              onChange={(e) => set(name, e.target.value)}
            />
          )}
          {spec.type !== 'bool' && spec.description && <div className="hint">{spec.description}</div>}
          {errors?.[name] && <div className="err-text">{errors[name]}</div>}
        </div>
      ))}
    </div>
  );
}
