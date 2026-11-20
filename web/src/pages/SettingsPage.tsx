import { useEffect, useState } from 'react';
import { MODEL_ROLES, api, type L2Policy, type ModelRole, type ModelSpec, type Settings } from '../api';
import { Card, ErrorBox, Loading } from '../components/common';
import { ROLE_LABEL } from '../lib/format';

const L2_LABEL: Record<L2Policy, string> = {
  always: '每步都检查（always）',
  on_change: '仅新版本/修补后的步骤（on_change）',
  sample: '按比例抽样（sample）',
  off: '关闭（off）',
};

const emptyModel = (): ModelSpec => ({ base_url: '', model: '', api_key: '', api_key_env: '', vision: false });

function ModelFields({ value, onChange }: { value: ModelSpec; onChange: (m: ModelSpec) => void }) {
  const set = <K extends keyof ModelSpec>(k: K, v: ModelSpec[K]) => onChange({ ...value, [k]: v });
  return (
    <div className="model-fields">
      <div className="field">
        <label>Base URL</label>
        <input type="text" placeholder="https://api.openai.com/v1" value={value.base_url} onChange={(e) => set('base_url', e.target.value)} />
      </div>
      <div className="field">
        <label>模型</label>
        <input type="text" placeholder="gpt-4.1" value={value.model} onChange={(e) => set('model', e.target.value)} />
      </div>
      <div className="field">
        <label>API Key（可直接粘贴，留空则读环境变量）</label>
        <input
          type="password"
          autoComplete="off"
          placeholder="sk-...（仅保存在本机数据目录）"
          value={value.api_key ?? ''}
          onChange={(e) => set('api_key', e.target.value)}
        />
      </div>
      <div className="field">
        <label>API Key 环境变量（备用）</label>
        <input type="text" placeholder="OPENAI_API_KEY" value={value.api_key_env} onChange={(e) => set('api_key_env', e.target.value)} />
      </div>
      <div className="field">
        <label>最大输出 tokens（可选）</label>
        <input
          type="number"
          min={1}
          value={value.max_output_tokens ?? ''}
          onChange={(e) => set('max_output_tokens', e.target.value === '' ? undefined : parseInt(e.target.value, 10))}
        />
      </div>
      <label className="check">
        <input type="checkbox" checked={!!value.vision} onChange={(e) => set('vision', e.target.checked)} />
        支持图像输入（vision）
      </label>
    </div>
  );
}

export function SettingsPage() {
  const [s, setS] = useState<Settings>();
  const [patterns, setPatterns] = useState('');
  const [loadErr, setLoadErr] = useState<Error>();
  const [err, setErr] = useState<string>();
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const load = async () => {
    setLoadErr(undefined);
    try {
      const d = await api.getSettings();
      setS(d);
      setPatterns((d.safety?.blocked_patterns ?? []).join('\n'));
    } catch (e) {
      setLoadErr(e as Error);
    }
  };
  useEffect(() => {
    void load();
  }, []);

  if (loadErr && !s) return <div className="page"><ErrorBox error={loadErr} onRetry={load} /></div>;
  if (!s) return <div className="page"><Loading /></div>;

  const upd = (fn: (d: Settings) => Settings) => {
    setS(fn(s));
    setSaved(false);
  };
  const sup = s.supervisor;
  const setSup = <K extends keyof Settings['supervisor']>(k: K, v: Settings['supervisor'][K]) =>
    upd((d) => ({ ...d, supervisor: { ...d.supervisor, [k]: v } }));

  const setRole = (role: ModelRole, m: ModelSpec | undefined) =>
    upd((d) => {
      const roles = { ...d.models.roles };
      if (m) roles[role] = m;
      else delete roles[role];
      return { ...d, models: { ...d.models, roles } };
    });

  const save = async () => {
    setSaving(true);
    setErr(undefined);
    try {
      const body: Settings = {
        ...s,
        safety: {
          ...s.safety,
          blocked_patterns: patterns
            .split('\n')
            .map((x) => x.trim())
            .filter(Boolean),
        },
      };
      const d = await api.putSettings(body);
      setS(d);
      setPatterns((d.safety?.blocked_patterns ?? []).join('\n'));
      setSaved(true);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>设置</h1>
        <p className="subtitle">模型、监督策略与安全规则。可直接粘贴 API Key（脱敏保存在本机），也可留空从环境变量读取。</p>
      </div>

      <Card title="默认模型">
        <ModelFields value={s.models.default} onChange={(m) => upd((d) => ({ ...d, models: { ...d.models, default: m } }))} />
      </Card>

      <Card title="角色模型覆盖">
        <p className="muted small">未启用覆盖的角色使用默认模型。</p>
        <div className="roles">
          {MODEL_ROLES.map((role) => {
            const m = s.models.roles?.[role];
            return (
              <div key={role} className="role-block">
                <label className="check role-title">
                  <input
                    type="checkbox"
                    checked={!!m}
                    onChange={(e) => setRole(role, e.target.checked ? { ...emptyModel(), ...s.models.default } : undefined)}
                  />
                  <strong>{ROLE_LABEL[role]}</strong> <code className="small">{role}</code>
                </label>
                {m ? <ModelFields value={m} onChange={(v) => setRole(role, v)} /> : <div className="muted small">使用默认模型</div>}
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="主智能体与多智能体并行池">
        <div className="form-grid">
          <div className="field">
            <label>最大并行子智能体数量（独立浏览器槽位上限）</label>
            <input
              type="number"
              min={2}
              max={12}
              step={1}
              value={s.coordinator?.max_parallel_workers ?? 4}
              onChange={(e) =>
                upd((d) => ({
                  ...d,
                  coordinator: {
                    ...d.coordinator,
                    max_parallel_workers: Math.min(12, Math.max(2, parseInt(e.target.value, 10) || 4)),
                  },
                }))
              }
            />
          </div>
          <div className="field">
            <label>直连子智能体单阶段步数预算（默认 120，最大 1000）</label>
            <input
              type="number"
              min={10}
              max={1000}
              step={10}
              value={s.coordinator?.operator_turn_limit ?? 120}
              onChange={(e) =>
                upd((d) => ({
                  ...d,
                  coordinator: {
                    ...d.coordinator,
                    operator_turn_limit: Math.min(1000, Math.max(10, parseInt(e.target.value, 10) || 120)),
                  },
                }))
              }
            />
          </div>
          <div className="field">
            <label>超长任务自动压缩续跑段数（0–10，默认 2 段即最高自动跑 3× 步数）</label>
            <input
              type="number"
              min={0}
              max={10}
              step={1}
              value={s.coordinator?.max_auto_continues ?? 2}
              onChange={(e) =>
                upd((d) => ({
                  ...d,
                  coordinator: {
                    ...d.coordinator,
                    max_auto_continues: Math.min(10, Math.max(0, parseInt(e.target.value, 10) || 0)),
                  },
                }))
              }
            />
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={s.coordinator?.auto_report ?? true}
              onChange={(e) =>
                upd((d) => ({
                  ...d,
                  coordinator: { ...d.coordinator, auto_report: e.target.checked },
                }))
              }
            />
            并行子智能体全部完成后自动由主智能体汇总汇报到对话流
          </label>
        </div>
      </Card>

      <Card title="监督者与自主自愈策略">
        <div className="form-grid">
          <div className="field">
            <label>L2（视觉/语义）检查策略</label>
            <select value={sup.l2_policy} onChange={(e) => setSup('l2_policy', e.target.value as L2Policy)}>
              {(Object.keys(L2_LABEL) as L2Policy[]).map((p) => (
                <option key={p} value={p}>
                  {L2_LABEL[p]}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>抽样比例（0–1）</label>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              disabled={sup.l2_policy !== 'sample'}
              value={sup.sample_rate}
              onChange={(e) => setSup('sample_rate', Math.min(1, Math.max(0, Number(e.target.value) || 0)))}
            />
          </div>
          <div className="field">
            <label>最大代码修复尝试次数（Tier 1）</label>
            <input
              type="number"
              min={0}
              step={1}
              value={sup.max_repair_attempts}
              onChange={(e) => setSup('max_repair_attempts', Math.max(0, parseInt(e.target.value, 10) || 0))}
            />
          </div>
          <div className="field">
            <label>连续失败多少次后自动回滚</label>
            <input
              type="number"
              min={1}
              step={1}
              disabled={!sup.auto_rollback}
              value={sup.rollback_after_failures}
              onChange={(e) => setSup('rollback_after_failures', Math.max(1, parseInt(e.target.value, 10) || 1))}
            />
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={sup.l15_sentinel ?? true}
              onChange={(e) => setSup('l15_sentinel', e.target.checked)}
            />
            启用 L1.5 零 Token 浏览器运行时哨兵（HTTP/JS/白屏/登录失效检测）
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={sup.adaptive_l2 ?? true}
              onChange={(e) => setSup('adaptive_l2', e.target.checked)}
            />
            运行时异常时自适应升级触发 L2 视觉校验
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={sup.tier0_heal ?? true}
              onChange={(e) => setSup('tier0_heal', e.target.checked)}
            />
            启用 Tier-0 零 Token 浏览器自愈（清理弹窗/多余标签页/重连）
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={sup.autonomous_fallback ?? true}
              onChange={(e) => setSup('autonomous_fallback', e.target.checked)}
            />
            启用 Tier-2 AI 自主接管兜底（单步修补不足时由 AI 接管恢复现场）
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={sup.unattended ?? false}
              onChange={(e) => setSup('unattended', e.target.checked)}
            />
            全局无人值守模式（不挂起等待人工工单）
          </label>
          <label className="check">
            <input type="checkbox" checked={sup.l3_enabled} onChange={(e) => setSup('l3_enabled', e.target.checked)} />
            启用 L3 最终结果校验
          </label>
          <label className="check">
            <input type="checkbox" checked={sup.auto_rollback} onChange={(e) => setSup('auto_rollback', e.target.checked)} />
            新版本连续失败时自动回滚
          </label>
        </div>
      </Card>

      <Card title="安全">
        <div className="field">
          <label>禁止模式（每行一个，匹配到的操作会被拦截）</label>
          <textarea
            rows={6}
            className="mono"
            placeholder={'rm -rf\nformat c:\n删除账户'}
            value={patterns}
            onChange={(e) => {
              setPatterns(e.target.value);
              setSaved(false);
            }}
          />
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={s.safety.confirm_side_effects}
            onChange={(e) => upd((d) => ({ ...d, safety: { ...d.safety, confirm_side_effects: e.target.checked } }))}
          />
          执行有副作用的步骤前需要人工确认
        </label>
      </Card>

      <div className="save-bar">
        <ErrorBox error={err} />
        {saved && <span className="success-text">✅ 已保存</span>}
        <span className="grow" />
        <button className="btn" onClick={() => void load()} disabled={saving}>
          放弃更改
        </button>
        <button className="btn btn-primary" onClick={() => void save()} disabled={saving}>
          {saving ? '保存中…' : '保存设置'}
        </button>
      </div>
    </div>
  );
}
