import { useEffect, useState, type FormEvent } from "react";
import { apiJson } from "../api";

const settingsUrl = "/api/workbench/settings";
const textFields = ["deepseek_base_url", "deepseek_model", "sec_user_agent"] as const;
const numericFields = ["llm_batch_k", "llm_threads", "llm_timeout"] as const;
const secretFields = ["deepseek_api_key", "finnhub_api_key"] as const;
type TextField = typeof textFields[number];
type NumericField = typeof numericFields[number];
type SecretField = typeof secretFields[number];
type Draft = Record<TextField | NumericField | SecretField, string>;
type Snapshot = Record<TextField, string> & Record<NumericField, number> & {
  has_deepseek_api_key: boolean;
  has_finnhub_api_key: boolean;
  overridden: string[];
};
type Patch = Partial<Record<keyof Draft, string | number | null>>;

function fromSnapshot(snapshot: Snapshot): Draft {
  return {
    deepseek_base_url: snapshot.deepseek_base_url,
    deepseek_model: snapshot.deepseek_model,
    sec_user_agent: snapshot.sec_user_agent,
    llm_batch_k: String(snapshot.llm_batch_k),
    llm_threads: String(snapshot.llm_threads),
    llm_timeout: String(snapshot.llm_timeout),
    deepseek_api_key: "",
    finnhub_api_key: "",
  };
}

export default function SettingsWorkspace({ onModelDefaultsChange }: { onModelDefaultsChange: () => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [draft, setDraft] = useState<Draft>();
  const [restore, setRestore] = useState<Partial<Record<SecretField, boolean>>>({});
  const [reload, setReload] = useState(0);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; message: string }>();

  useEffect(() => {
    const request = new AbortController();
    void apiJson<Snapshot>(settingsUrl, { signal: request.signal }).then((data) => {
      setSnapshot(data);
      setDraft(fromSnapshot(data));
    }).catch((error: unknown) => {
      if (!request.signal.aborted) {
        setFeedback({ ok: false, message: error instanceof Error ? error.message : "无法读取设置。" });
      }
    });
    return () => request.abort();
  }, [reload]);

  if (!snapshot || !draft) {
    return <div className="settings-page">
      <h2>设置</h2>
      {feedback ? <>
        <p className="err" role="alert">{feedback.message}</p>
        <button type="button" className="btn btn-secondary" onClick={() => {
          setFeedback(undefined);
          setReload((current) => current + 1);
        }}>重试</button>
      </> : <p className="muted" role="status">正在读取设置…</p>}
    </div>;
  }

  const update = (key: keyof Draft, value: string) => {
    setDraft({ ...draft, [key]: value });
    setFeedback(undefined);
  };
  const patch: Patch = {};
  for (const key of textFields) {
    if (draft[key].trim() !== snapshot[key]) patch[key] = draft[key].trim();
  }
  for (const key of numericFields) {
    if (Number(draft[key]) !== snapshot[key]) patch[key] = Number(draft[key]);
  }
  for (const key of secretFields) {
    if (restore[key]) patch[key] = null;
    else if (draft[key].trim()) patch[key] = draft[key].trim();
  }
  const dirty = Object.keys(patch).length > 0;
  const modelChanged = ["deepseek_base_url", "deepseek_model", "deepseek_api_key"].some((key) => key in patch);

  const handleSave = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving || !dirty) return;
    setFeedback(undefined);
    setSaving(true);
    try {
      const saved = await apiJson<Snapshot>(settingsUrl, { method: "PATCH", body: JSON.stringify(patch) });
      setSnapshot(saved);
      setDraft(fromSnapshot(saved));
      setRestore({});
      if (modelChanged) onModelDefaultsChange();
      setFeedback({ ok: true, message: "设置已保存。后续请求会使用新设置。" });
    } catch (error) {
      setFeedback({ ok: false, message: error instanceof Error ? error.message : "保存设置失败。" });
    } finally {
      setSaving(false);
    }
  };

  const secretField = (key: SecretField, title: string) => {
    const configured = snapshot[key === "deepseek_api_key" ? "has_deepseek_api_key" : "has_finnhub_api_key"];
    const saved = snapshot.overridden.includes(key);
    return <div className="settings-secret">
      <label className="field">
        <span>{title}</span>
        <input className="input" type="password" value={draft[key]} autoComplete="new-password"
          maxLength={4096} spellCheck={false} disabled={restore[key]}
          onChange={(event) => update(key, event.target.value)}
          placeholder={configured ? "已配置，留空保留" : "输入新 Key"} />
      </label>
      <div className="settings-key-options">
        <span className="muted">{configured ? (saved ? "已保存在服务端" : "来自环境配置") : "未配置"}</span>
        {saved && <label><input type="checkbox" checked={Boolean(restore[key])}
          onChange={(event) => {
            setRestore({ ...restore, [key]: event.target.checked });
            update(key, "");
          }} />恢复环境配置</label>}
      </div>
    </div>;
  };

  return (
    <div className="settings-page">
      <header className="settings-heading">
        <h2>设置</h2>
        <p className="muted">管理这台服务的默认配置。投研页的临时连接可以覆盖模型默认值。</p>
      </header>
      <form onSubmit={handleSave}>
        <fieldset disabled={saving}>
          <section className="settings-section" aria-labelledby="setting-model">
            <div><h3 id="setting-model">模型连接</h3><p>投研和行情新闻分析共用。</p></div>
            <div className="settings-fields">
              <label className="field"><span>模型服务地址</span>
                <input className="input" type="url" required maxLength={512} spellCheck={false}
                  value={draft.deepseek_base_url} onChange={(event) => update("deepseek_base_url", event.target.value)} /></label>
              <label className="field"><span>模型 ID</span>
                <input className="input" required maxLength={128} spellCheck={false}
                  value={draft.deepseek_model} onChange={(event) => update("deepseek_model", event.target.value)} /></label>
              {secretField("deepseek_api_key", "模型 API Key")}
            </div>
          </section>
          <section className="settings-section" aria-labelledby="setting-sources">
            <div><h3 id="setting-sources">数据源</h3><p>用于美股数据采集。</p></div>
            <div className="settings-fields">
              {secretField("finnhub_api_key", "Finnhub API Key")}
              <label className="field"><span>SEC EDGAR 联系信息</span>
                <input className="input" required maxLength={256} spellCheck={false}
                  value={draft.sec_user_agent} onChange={(event) => update("sec_user_agent", event.target.value)} /></label>
            </div>
          </section>
          <details className="settings-section settings-advanced">
            <summary>高级设置 <small>语义查询</small></summary>
            <div className="settings-fields settings-numbers">
              {([
                ["llm_batch_k", "批大小", 128, "1"],
                ["llm_threads", "并发线程", 64, "1"],
                ["llm_timeout", "超时（秒）", 900, "any"],
              ] as const).map(([key, label, max, step]) => <label className="field" key={key}><span>{label}</span>
                <input className="input" type="number" required min={1} max={max} step={step}
                  value={draft[key]} onChange={(event) => update(key, event.target.value)} /></label>)}
            </div>
          </details>
        </fieldset>
        <div className="settings-actions">
          <button className="btn" type="submit" disabled={!dirty || saving}>{saving ? "保存中…" : "保存设置"}</button>
          {dirty && <button className="btn btn-secondary" type="button" disabled={saving} onClick={() => {
            setDraft(fromSnapshot(snapshot));
            setRestore({});
            setFeedback(undefined);
          }}>放弃修改</button>}
          {feedback && <p className={feedback.ok ? "ok-banner" : "err"} role={feedback.ok ? "status" : "alert"}>{feedback.message}</p>}
        </div>
      </form>
    </div>
  );
}
