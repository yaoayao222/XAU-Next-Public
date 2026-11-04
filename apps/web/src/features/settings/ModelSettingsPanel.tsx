import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, commandKey, getApiBase, type Schema } from '../../api/client';
import { Badge, ErrorNotice, Field } from '../../shared/ui';
import { time } from '../../shared/format';
import { ModelCallPanel } from '../analysis/ModelCallPanel';

function ModelForm({ settings, profiles }: { settings: Schema['ModelSettings']; profiles: Schema['ModelProfileList'] }) {
  const cache = useQueryClient();
  const [values, setValues] = useState(settings.values); const [key, setKey] = useState(''); const [savingKey, setSavingKey] = useState(false);
  const [keyError, setKeyError] = useState<unknown>(null); const [paid, setPaid] = useState(false);
  const [selectedProfile, setSelectedProfile] = useState(profiles.active_profile_id);
  const [creating, setCreating] = useState(false); const [profileName, setProfileName] = useState('本地 CPA');
  const storageKey = 'xau-model-probe:' + getApiBase() + ':' + profiles.active_profile_id;
  const [probeId, setProbeId] = useState<string | null>(() => localStorage.getItem(storageKey) ?? settings.checks.find(item => item.kind === 'probe')?.call_id ?? null);
  const set = <K extends keyof Schema['ModelValues']>(field: K, value: Schema['ModelValues'][K]) => setValues(previous => ({ ...previous, [field]: value }));
  const [catalogue, setCatalogue] = useState<string[] | null>(null);
  const [customModel, setCustomModel] = useState(false);
  const changed = creating || JSON.stringify(values) !== JSON.stringify(settings.values) || catalogue !== null;
  function saved(result: Schema['ModelSettings']) {
    cache.setQueryData(['model-settings'], result);
    void cache.invalidateQueries({ queryKey: ['model-profiles'] });
  }
  const save = useMutation({ mutationFn: () => {
    const next = catalogue === null ? values : { ...values, model_ids: Array.from(new Set(catalogue.map(m => m.trim()).filter(Boolean))) };
    return creating ? api.createModelProfile({ expected_revision: settings.revision, name: profileName, values: next }) : api.saveModelSettings({ expected_revision: settings.revision, values: next });
  }, onSuccess: saved });
  const activate = useMutation({ mutationFn: () => api.activateModelProfile(selectedProfile, settings.revision), onSuccess: saved });
  const check = useMutation({ mutationFn: (kind: 'models' | 'usage') => api.checkModel(kind), onSuccess: result => cache.setQueryData(['model-settings'], result) });
  const probe = useMutation({ mutationFn: () => api.probeModel(commandKey()), onSuccess: result => { setProbeId(result.call_id); localStorage.setItem(storageKey, result.call_id); } });
  async function storeKey() {
    const secret = key; setKey(''); setSavingKey(true); setKeyError(null);
    try { const result = await api.setModelCredential({ key: secret, expected_revision: settings.revision }, commandKey()); saved(result); }
    catch (error) { setKeyError(error); }
    finally { setSavingKey(false); }
  }
  const models = settings.checks.find(item => item.kind === 'models');
  const selectedModels = catalogue ?? values.model_ids ?? (creating ? [] : profiles.items.find(p => p.profile_id === profiles.active_profile_id)?.model_ids) ?? [values.model_id];
  const modelOptions = Array.from(new Set([
    ...selectedModels,
    ...(!creating && models?.status === 'passed' ? models.model_ids : []),
    values.model_id,
  ].map(id => id.trim()).filter(id => id && id !== '待选择模型')));
  const usage = settings.checks.find(item => item.kind === 'usage');
  const lastUsage = settings.last_successful_usage;
  return <section className="panel model-settings" aria-label="模型 API 设置"><div className="section-heading"><h2>模型 API · 供应商连接</h2><Badge tone={settings.capability_verified ? 'good' : 'warn'}>{settings.capability_verified ? '字符保真测试已通过' : '字符保真待验证'}</Badge></div>
    <p>每套连接分别保存地址、协议、模型、参数和本机密钥。切换不会发起模型生成，也不会自动测试。</p>
    <div className="form-grid"><Field label="已保存配置"><select value={selectedProfile} disabled={changed || !!key || savingKey || activate.isPending || save.isPending} onChange={event => setSelectedProfile(event.target.value)}>{profiles.items.map(profile => <option key={profile.profile_id} value={profile.profile_id}>{profile.name}</option>)}</select></Field>
      <div className="form-actions"><button disabled={selectedProfile === profiles.active_profile_id || changed || !!key || savingKey || activate.isPending || save.isPending} onClick={() => activate.mutate()}>切换到所选配置</button>
      {!creating && <button disabled={changed || !!key || savingKey || activate.isPending || save.isPending} onClick={() => { setCreating(true); setPaid(false); setCustomModel(false); setCatalogue(null); setValues({ ...settings.values, enabled: true, provider: '本地 CPA', base_url: 'http://127.0.0.1:8317/v1', protocol: 'responses', output_format: 'json_schema', model_id: 'gpt-6.1-sol', model_ids: ['gpt-6.1-sol','gpt-6-astra'], chat_token_parameter: 'max_completion_tokens', reasoning_effort: 'high', stream: true, timeout_seconds: 600, max_output_tokens: 25000, max_response_bytes: 16000000, max_answer_bytes: 2000000, daily_budget: null, input_price_per_million: null, output_price_per_million: null, price_currency: null, price_source: null }); }}>新增配置</button>}
      {!creating && <button disabled={changed || !!key || savingKey || activate.isPending || save.isPending} onClick={() => {
        setCreating(true); setPaid(false); setCustomModel(false); setProfileName('鸡出没 DeepSeek'); setCatalogue([]);
        setValues({ ...settings.values, enabled: false, provider: '鸡出没API', base_url: 'https://xcmapi.org/v1',
          protocol: 'chat_completions', output_format: 'json_schema', model_id: '待选择模型', model_ids: [],
          chat_token_parameter: 'max_completion_tokens', reasoning_effort: 'high', stream: true,
          timeout_seconds: 600, max_output_tokens: 32000, max_response_bytes: 16000000, max_answer_bytes: 2000000,
          daily_budget: null, input_price_per_million: null, output_price_per_million: null, price_currency: null, price_source: null });
      }}>新增鸡出没连接</button>}</div></div>
    <p className="muted">当前使用：{profiles.items.find(item => item.profile_id === profiles.active_profile_id)?.name}。保存修改会更新当前这套配置；新增配置不会沿用其他配置的密钥。</p>
    {creating && <><Field label="新配置名称"><input value={profileName} maxLength={80} required onChange={event => setProfileName(event.target.value)} /></Field><p>已填入本地 CPA 推荐参数，可按需修改。新配置保存后单独录入该供应商的本地 API Key。鸡出没预设先关闭分析入口；读取模型列表、选定准确编号后再启用。</p><button onClick={() => { setCreating(false); setCustomModel(false); setValues(settings.values); setCatalogue(null); }}>取消新增</button></>}
    {(values.provider.toLowerCase().includes('cpa') || values.base_url === 'http://127.0.0.1:8317/v1') && <p className="notice">CPA 8.0.30 的 Codex 路径不会保留输出 token 上限；字节限制和超时不能替代 token 硬上限。严格格式与完整分析仍须单独实测。</p>}
    <form onSubmit={event => { event.preventDefault(); save.mutate(); }}><div className="form-grid">
      <label className="checkbox"><input type="checkbox" checked={values.enabled} onChange={event => set('enabled', event.target.checked)} />启用 API 分析入口</label>
      <Field label="中转站名称"><input value={values.provider} onChange={event => set('provider', event.target.value)} required /></Field>
      <Field label="API Base URL" hint="填写根地址或 /v1；不能填写 /dashboard。"><input type="url" value={values.base_url} onChange={event => set('base_url', event.target.value)} required /></Field>
      <Field label="调用协议"><select value={values.protocol} onChange={event => set('protocol', event.target.value as typeof values.protocol)}><option value="responses">OpenAI Responses（首选验证）</option><option value="chat_completions">OpenAI Chat Completions（兼容接口）</option><option value="anthropic_messages">Anthropic Messages（/v1/messages）</option></select></Field>
      <Field label="模型 ID" hint="选择默认模型；同一 API 的多个模型共用本连接的地址和密钥，分析时可分别勾选。选择后需保存。"><select value={customModel ? '' : values.model_id} onChange={event => { const id = event.target.value; setCustomModel(!id); if (id) set('model_id', id); }}>
        <option value="待选择模型" disabled>请选择默认模型</option>
        {modelOptions.map(id => <option key={id} value={id}>{id}</option>)}
        <option value="">手动填写模型编号…</option>
      </select></Field>
      {customModel && <Field label="自定义模型 ID" hint="逐字填写供应商提供的模型编号。"><input value={values.model_id === '待选择模型' ? '' : values.model_id} onChange={event => set('model_id', event.target.value)} required /></Field>}
      {values.protocol === 'chat_completions' && <Field label="Chat输出上限参数" hint="按供应商文档选择。失败不会自动改参数重试。"><select value={values.chat_token_parameter ?? 'max_completion_tokens'} onChange={e => set('chat_token_parameter', e.target.value as 'max_completion_tokens' | 'max_tokens')}><option value="max_completion_tokens">max_completion_tokens</option><option value="max_tokens">max_tokens</option></select></Field>}
      <Field label="推理档位" hint={values.protocol === 'anthropic_messages' ? 'Anthropic Messages 适配器不会发送 OpenAI reasoning.effort 参数；该值仅保留在配置记录中。' : '所选参数将在合成测试中明确发送；测试通过只证明中转站接受，不能独立证明执行强度。'}><select value={values.reasoning_effort} onChange={event => set('reasoning_effort', event.target.value as typeof values.reasoning_effort)}>{['omit', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'].map(item => <option key={item} value={item}>{item === 'omit' ? '不发送推理参数' : item}</option>)}</select></Field>
      <Field label="回答格式" hint={values.protocol === 'anthropic_messages' ? 'Anthropic Messages 不发送 OpenAI response_format；回答格式由冻结提示指导，并始终接受本地完整 Schema 校验。xcmapi 鉴权使用 Bearer Key。' : undefined}><select value={values.output_format} onChange={event => set('output_format', event.target.value as typeof values.output_format)}><option value="json_schema">{values.protocol === 'anthropic_messages' ? 'JSON Schema 提示约束 + 本地校验' : '严格结构化输出（须实测）'}</option><option value="json_object">{values.protocol === 'anthropic_messages' ? 'JSON 对象提示约束 + 本地校验' : 'JSON 对象格式（须实测）'}</option><option value="plain_json">提示词 JSON（不传格式参数，须实测）</option></select></Field>
      <label className="checkbox"><input type="checkbox" checked={values.stream} onChange={event => set('stream', event.target.checked)} />流式接收（完整结束后才校验）</label>
      <Field label="总超时（秒）"><input type="number" min={10} max={900} value={values.timeout_seconds} onChange={event => set('timeout_seconds', Number(event.target.value))} /></Field>
      <Field label="推理和回答总上限（token）"><input type="number" min={256} max={32000} value={values.max_output_tokens} onChange={event => set('max_output_tokens', Number(event.target.value))} /></Field>
      <Field label="原始响应上限（字节）" hint="包含流式事件包装和推理增量，不等于最终答案大小。"><input type="number" min={4096} max={16000000} value={values.max_response_bytes} onChange={event => set('max_response_bytes', Number(event.target.value))} /></Field>
      <Field label="最终答案上限（字节）" hint="提取后的 JSON 按 UTF-8 字节单独检查。"><input type="number" min={4096} max={2000000} value={values.max_answer_bytes ?? 2000000} onChange={event => set('max_answer_bytes', Number(event.target.value))} /></Field>
      <Field label="每日生成次数上限（UTC）"><input type="number" min={1} max={100} value={values.daily_call_limit} onChange={event => set('daily_call_limit', Number(event.target.value))} /></Field>
    </div><p className="muted">初始值为300秒、12000 token、原始响应2 MB、最终答案2 MB、每日10次，可按实测调整。合成测试也计入次数；结果未知的请求仍计入。仅调整限额或价格会沿用相同连接配置的既有小测证据，不代表新预算下的完整分析已经通过。</p>
    <details open={creating}><summary>这个供应商可选的模型</summary>
      <Field label="可选模型编号（每行一个）" hint="从该密钥返回的模型列表选取，或逐字填写。每个连接独立保存；同一连接的默认模型也会保留。"><textarea value={selectedModels.join('\n')} onChange={e => setCatalogue(e.target.value.split(/\r?\n/))} rows={5} /></Field>
      {!creating && models?.status === 'passed' && <div><p>上次读取的模型目录（{time(models.checked_at)}）：勾选需要的模型后保存。</p>{models.model_ids.map(id => <label className="checkbox" key={id}><input type="checkbox" checked={(catalogue ?? values.model_ids ?? [values.model_id]).includes(id)} onChange={e => { const selected = catalogue ?? values.model_ids ?? [values.model_id]; setCatalogue(e.target.checked ? [...selected, id] : selected.filter(m => m !== id)); }} />{id}</label>)}</div>}
    </details>
    <details><summary>本站价格与本地预算估算（未知可留空）</summary><div className="form-grid">
      {([['input_price_per_million', '本站输入价格 / 百万 token'], ['output_price_per_million', '本站输出价格 / 百万 token'], ['daily_budget', '每日估算预算上限'], ['price_currency', '本站价格币种'], ['price_source', '本站价格规则来源']] as const).map(([field, text]) => <Field key={field} label={text}><input value={values[field] ?? ''} onChange={event => set(field, event.target.value || null)} placeholder="未知" /></Field>)}
    </div><p className="muted">只填写本站实际规则；计费有分档、缓存价或额外费时，使用保守上限并注明来源。输入按字节数、输出按上限预留预算。估算不保证供应商账单上限，不以账户余额变化推算单次费用。</p></details>
    <div className="form-actions"><button type="submit" disabled={save.isPending || activate.isPending || !changed || (creating && !profileName.trim() || (values.enabled && values.model_id === '待选择模型'))}>{creating ? '保存为新配置并切换' : '保存模型配置'}</button>{changed && <span>先保存修改，再检查或测试。</span>}</div></form>
    <div className="form-grid"><Field label="本机 API Key" hint="先保存配置，再录入密钥。只写入此 Windows 用户的凭据存储；换机器恢复须重新录入。"><input type="password" autoComplete="new-password" disabled={changed || save.isPending || savingKey || activate.isPending} value={key} onChange={event => setKey(event.target.value)} placeholder={settings.key_configured ? '已配置，可输入新密钥替换' : '尚未配置'} maxLength={2048} /></Field><div><p>密钥状态：{settings.key_configured ? '已配置' : '未配置 / 本机不存在'}</p><button disabled={!key || savingKey || changed || save.isPending || activate.isPending} onClick={() => void storeKey()}>保存到 Windows 凭据</button></div></div>
    <div className="actions"><button disabled={!settings.key_configured || changed || check.isPending || activate.isPending} onClick={() => check.mutate('models')}>鉴权并读取模型列表</button><button disabled={!settings.key_configured || changed || check.isPending || activate.isPending} onClick={() => check.mutate('usage')}>按需查询余额</button></div>
    {models && <p>模型列表：{models.status === 'passed' ? '查询成功' : '查询失败'} · {time(models.checked_at)} · 配置修订 {models.config_revision}。{models.detail}</p>}
    <p>最近成功余额：{lastUsage?.remaining ?? '未知'} {lastUsage?.unit ?? '单位未知'} · 有效性：{lastUsage?.is_active === true ? '服务报告有效' : lastUsage?.is_active === false ? '服务报告无效' : '未知'}{lastUsage && ` · ${time(lastUsage.checked_at)}（配置修订 ${lastUsage.config_revision}）`}</p>
    {usage?.status === 'failed' && <p className="notice">本次余额查询失败：{usage.detail} 已保留上次成功结果，不能视为当前余额。</p>}
    <hr /><h3>小型合成生成测试</h3><p>将按已保存的协议、模型、推理档位及格式发送 SYNTHETIC 请求，测试最多2048 token、120秒（若配置更低则用较低值）；可能产生费用。模型列表和余额成功不代表生成可用。</p>
    <p>保存和切换连接不会自动重发旧失败，也不会自动换渠道。输出上限按本连接保存，下一次主动分析才使用；旧记录保留原参数。</p><p>逐值检查日期、引用中的连字符和下划线、负数及中文。旧版连接测试不包含这些检查，不能替代字符保真验证；更换中转站分组后也应主动重新测试。小测通过只证明本次样例，不能保证长回答合格。</p>
    <label className="checkbox"><input type="checkbox" checked={paid} onChange={event => setPaid(event.target.checked)} />我主动启动这次可能收费的合成测试</label>
    <button disabled={!paid || !settings.key_configured || changed || probe.isPending || activate.isPending} onClick={() => { probe.mutate(); setPaid(false); }}>启动合成生成测试</button>
    {probeId && <ModelCallPanel key={probeId} callId={probeId} />}
    <ErrorNotice error={save.error ?? activate.error ?? check.error ?? probe.error ?? keyError} />
  </section>;
}

export function ModelSettingsPanel() {
  const query = useQuery({ queryKey: ['model-settings'], queryFn: api.modelSettings });
  const profiles = useQuery({ queryKey: ['model-profiles'], queryFn: api.modelProfiles });
  return <><ErrorNotice error={query.error ?? profiles.error} />{query.data && profiles.data && <ModelForm key={query.data.revision + ':' + profiles.data.active_profile_id} settings={query.data} profiles={profiles.data} />}</>;
}
