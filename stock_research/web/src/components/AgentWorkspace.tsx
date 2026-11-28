import { useEffect, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  agentEvents,
  apiJson,
  isSafeHttpUrl,
  type AgentConnection,
  type AgentQueryRequest,
  type AgentQueryResponse,
  type AgentSettings,
  type AgentVerification,
  type ModelConnectionOptions,
  type ToolCallItem,
} from "../api";
import { LAST_CONNECTION_KEY, readLastConnection } from "../agentConnection";

const PRESET_PROMPTS = [
  "我想知道最近3天a股电力行业的情况",
  "从长期价值投资角度分析 000858 五粮液的估值与安全边际",
  "结合行情与财务数据给出 A 股日频量化交易策略建议",
];

export default function AgentWorkspace({ onConnectionChange }: {
  onConnectionChange: (connection: ModelConnectionOptions) => void;
}) {
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState("auto");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [defaults, setDefaults] = useState<AgentSettings | null>(null);
  const [lastConnection, setLastConnection] = useState(readLastConnection);
  const [validationErr, setValidationErr] = useState("");
  const [running, setRunning] = useState(false);
  const [stage, setStage] = useState<"idle" | "running" | "done" | "error">("idle");
  const [toolCalls, setToolCalls] = useState<ToolCallItem[]>([]);
  const [verification, setVerification] = useState<AgentVerification | null>(null);
  const [answer, setAnswer] = useState("");
  const [sources, setSources] = useState<string[]>([]);
  const [durationMs, setDurationMs] = useState<number | null>(null);
  const [runError, setRunError] = useState("");
  const [showRawMd, setShowRawMd] = useState(false);
  const [copied, setCopied] = useState(false);
  const requestRef = useRef<AbortController | null>(null);
  const effectiveBaseUrl = baseUrl.trim() || lastConnection?.base_url;
  const effectiveModel = model.trim() || lastConnection?.model;
  const defaultBaseUrl = defaults?.base_url || "https://api.deepseek.com/v1";
  const usesCustomBaseUrl = effectiveBaseUrl && effectiveBaseUrl.replace(/\/+$/, "") !== defaultBaseUrl;

  useEffect(() => {
    onConnectionChange({ base_url: effectiveBaseUrl, model: effectiveModel, api_key: apiKey.trim() || undefined });
  }, [effectiveBaseUrl, effectiveModel, apiKey, onConnectionChange]);

  useEffect(() => {
    const controller = new AbortController();
    void apiJson<AgentSettings>("/api/agent/settings", { signal: controller.signal })
      .then(setDefaults)
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => () => requestRef.current?.abort(), []);

  const showRunError = (message: string) => {
    setRunError(message);
    setAnswer(`执行失败：${message}`);
    setStage("error");
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (requestRef.current) return;
    const cleanPrompt = prompt.trim();
    if (!cleanPrompt) {
      setValidationErr("请输入研究问题或投资需求后再提交（不能为空）。");
      return;
    }

    const request: AgentQueryRequest = {
      prompt: cleanPrompt,
      mode,
      base_url: effectiveBaseUrl || undefined,
      api_key: apiKey.trim() || undefined,
      model: effectiveModel,
    };
    let remembered = false;
    const rememberConnection = (connection?: AgentConnection) => {
      if (remembered || (!request.base_url && !request.model)) return;
      const next = {
        base_url: connection?.base_url ?? request.base_url ?? defaults?.base_url,
        model: connection?.model ?? request.model ?? defaults?.model,
      };
      if (!next.base_url || !next.model) return;
      const record = { base_url: next.base_url, model: next.model };
      remembered = true;
      setLastConnection(record);
      try {
        localStorage.setItem(LAST_CONNECTION_KEY, JSON.stringify(record));
      } catch {
        // The current page can still reuse the settings.
      }
    };
    const controller = new AbortController();
    requestRef.current = controller;
    setValidationErr("");
    setRunError("");
    setRunning(true);
    setStage("running");
    setToolCalls([]);
    setVerification(null);
    setAnswer("");
    setSources([]);
    setDurationMs(null);

    try {
      for await (const { name, data } of agentEvents(request, controller.signal)) {
        switch (name) {
          case "status": {
            const status = data as { stage?: string; connection?: AgentConnection };
            if (status.stage === "started") rememberConnection(status.connection);
            break;
          }
          case "tool_start": {
            const tool = data as ToolCallItem;
            setToolCalls((prev) => [...prev, { ...tool, status: "running" }]);
            break;
          }
          case "tool_end": {
            const tool = data as ToolCallItem;
            const updated: ToolCallItem = { ...tool, status: tool.is_error ? "error" : "done" };
            setToolCalls((prev) => {
              const idx = prev.findIndex((item) =>
                tool.tool_call_id
                  ? item.tool_call_id === tool.tool_call_id
                  : item.name === tool.name && item.status === "running",
              );
              return idx < 0
                ? [...prev, updated]
                : prev.map((item, index) => (index === idx ? updated : item));
            });
            break;
          }
          case "verification":
            setVerification(data as AgentVerification);
            break;
          case "final_answer": {
            const result = data as AgentQueryResponse;
            rememberConnection(result.connection);
            setAnswer(result.answer || "已完成分析，但未生成正文。");
            setSources(result.sources || []);
            setDurationMs(result.duration_ms ?? null);
            setStage("done");
            return;
          }
          case "error": {
            const error = data as { detail?: string };
            showRunError(error.detail || "SSE 流执行出错");
            return;
          }
        }
      }
      throw new Error("Agent 实时连接中断");
    } catch {
      if (controller.signal.aborted) return;
      try {
        const result = await apiJson<AgentQueryResponse>("/api/agent/query", {
          method: "POST",
          body: JSON.stringify(request),
          signal: controller.signal,
        });
        rememberConnection(result.connection);
        setToolCalls(result.tool_calls ?? []);
        setVerification(result.verification ?? null);
        setAnswer(result.answer || "未返回有效回答。");
        setSources(result.sources ?? []);
        setDurationMs(result.duration_ms ?? null);
        setStage("done");
      } catch (err) {
        if (!controller.signal.aborted) {
          showRunError(err instanceof Error ? err.message : "Agent 查询失败");
        }
      }
    } finally {
      requestRef.current = null;
      if (!controller.signal.aborted) setRunning(false);
    }
  };

  const renderToolCallsContent = () => {
    if (toolCalls.length > 0) {
      return toolCalls.map((tc, idx) => (
        <div key={`${tc.tool_call_id || tc.name}-${idx}`} className="tool-item">
          <div className="tool-item-head">
            <span>
              #{idx + 1} {tc.name}
            </span>
            <span>
              {tc.status === "running"
                ? "执行中..."
                : tc.is_error
                ? `错误 (${tc.duration_ms ?? 0}ms)`
                : `完成 (${tc.duration_ms ?? 0}ms)`}
            </span>
          </div>
          {tc.args && Object.keys(tc.args).length > 0 && (
            <div className="tool-item-args">参数: {JSON.stringify(tc.args)}</div>
          )}
          {tc.result_summary && (
            <div className="tool-item-res">摘要: {tc.result_summary}</div>
          )}
        </div>
      ));
    }
    if (stage === "running") {
      return "正在执行工具链...";
    }
    if (stage === "done") {
      return "已完成研究推理（本次未额外触发 MCP 工具）。";
    }
    if (stage === "error") {
      return `工具链执行中断：${runError || "请检查后端或 DEEPSEEK_API_KEY 配置"}`;
    }
    return "等待调用工具...";
  };

  const handleCopyAnswer = async () => {
    if (!answer) return;
    try {
      await navigator.clipboard.writeText(answer);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      // clipboard unavailable in insecure/headless contexts
    }
  };

  const renderAnswerContent = () => {
    if (answer) {
      if (showRawMd) {
        return <pre className="md-raw-pre">{answer}</pre>;
      }
      return (
        <div className="md-body">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ href, children }) =>
                isSafeHttpUrl(href) ? (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    {children}
                  </a>
                ) : (
                  <span>{children}</span>
                ),
              table: ({ children }) => (
                <div className="md-table-wrap">
                  <table className="md-table">{children}</table>
                </div>
              ),
            }}
          >
            {answer}
          </ReactMarkdown>
        </div>
      );
    }
    if (stage === "running") {
      return "正在核验结果...";
    }
    return "提交研究问题后，结论和证据会显示在这里。";
  };

  return (
    <div className="agent-page flow">
      <section className="composer panel" aria-label="研究提问">
      <form onSubmit={handleSubmit} className="query-form">
        <details className="disclosure connection-disclosure">
          <summary>模型连接</summary>
        <div className="agent-connection">
          <label className="field">
            <span>服务地址</span>
            <input
              data-testid="agent-backend"
              className="input"
              type="text"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={lastConnection?.base_url || defaultBaseUrl}
            />
          </label>
          <label className="field">
            <span>API Key</span>
            <input
              data-testid="agent-api-key"
              className="input"
              type="password"
              autoComplete="new-password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={usesCustomBaseUrl
                ? "请输入此 Base URL 对应的 API Key"
                : defaults?.has_api_key ? "使用服务端已配置的 API Key" : "请输入 API Key"}
            />
          </label>
          <label className="field">
            <span>模型</span>
            <input
              data-testid="agent-model"
              className="input"
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={lastConnection?.model || defaults?.model || "deepseek-flash"}
            />
          </label>
        </div>
        </details>

        <textarea
          data-testid="agent-input"
          className="input"
          rows={2}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              if (!running) event.currentTarget.form?.requestSubmit();
            }
          }}
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value);
            if (validationErr && e.target.value.trim()) {
              setValidationErr("");
            }
          }}
          placeholder="想研究哪个市场、行业或标的？"
          aria-label="研究问题输入框"
        />
        <div className="query-actions">
        <select
          className="select"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          aria-label="研究模式"
        >
          <option value="auto">智能模式</option>
          <option value="value">价值投资</option>
          <option value="quant">量化策略</option>
        </select>
        <button
          data-testid="agent-submit"
          className="btn"
          type="submit"
          disabled={running}
        >
          {running ? "研判执行中..." : "发起投研分析"}
        </button>
        </div>
      </form>

      <div className="controls-row">
        <span className="muted">试试这些问题</span>
        {PRESET_PROMPTS.map((item) => (
          <button
            key={item}
            type="button"
            className="btn-chip"
            onClick={() => {
              setPrompt(item);
              setValidationErr("");
            }}
          >
            {item}
          </button>
        ))}
      </div>

      {validationErr && (
        <div
          data-testid="agent-validation"
          className="err"
          role="alert"
        >
          {validationErr}
        </div>
      )}

      {runError && stage === "error" && (
        <div className="err" role="status">
          {runError}
        </div>
      )}

      </section>
      <div className="research-output">
      <section className="panel answer-panel">
          <div className="panel-heading">
            <div>
              <h2>研究结论</h2>
              {durationMs != null && durationMs > 0 && <p>耗时 {(durationMs / 1000).toFixed(2)} 秒</p>}
            </div>
            <span className="text-badge" data-tone={verification ? (verification.grounded ?? verification.passed ? "good" : "caution") : "neutral"} role="status">
              {verification ? (verification.grounded ?? verification.passed ? "已核验" : "有待核验项") : running ? "核验尚未完成" : "暂无核验结果"}
            </span>
            {answer && (
              <div className="card-head-actions">
                <button
                  type="button"
                  className="btn-mini"
                  onClick={() => setShowRawMd((v) => !v)}
                >
                  {showRawMd ? "Markdown 渲染" : "查看原文"}
                </button>
                <button
                  type="button"
                  className="btn-mini"
                  onClick={() => void handleCopyAnswer()}
                >
                  {copied ? "已复制" : "复制"}
                </button>
              </div>
            )}
          </div>
          {verification && ((verification.ungrounded_numbers?.length || 0) > 0 ||
            (verification.unverified_urls?.length || 0) > 0 || (verification.warnings?.length || 0) > 0) && (
            <div className="info-banner verification-notes" role="status">
              {verification.ungrounded_numbers?.length ? <div>待核验数值：{verification.ungrounded_numbers.join("、")}</div> : null}
              {verification.unverified_urls?.length ? <div>待核验链接：{verification.unverified_urls.join("、")}</div> : null}
              {verification.warnings?.map((warning) => <div key={warning}>{warning}</div>)}
            </div>
          )}
          <div className={sources.length > 0 ? "answer-content with-evidence" : "answer-content"}>
          <div data-testid="agent-answer" className="agent-panel-box md-answer">
            {renderAnswerContent()}
          </div>

          {sources.length > 0 && (
            <aside className="evidence" aria-label="证据来源"><h3>证据来源</h3><div className="source-badges">
              {sources.map((src) =>
                isSafeHttpUrl(src) ? (
                  <a
                    key={src}
                    href={src}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="source-badge source-badge-link"
                  >
                    {src}
                  </a>
                ) : (
                  <span key={src} className="source-badge">
                    {src}
                  </span>
                )
              )}
            </div></aside>
          )}
          </div>
      </section>
      <details className="disclosure trace-disclosure">
        <summary>工具调用轨迹 <span>{running ? "执行中" : toolCalls.length ? toolCalls.length + " 次调用" : "暂无调用"}</span></summary>
      <div className="flow-steps">
        <span className={`flow-pill ${prompt.trim() ? "active" : ""}`}>1. 用户需求</span>
        <span>→</span>
        <span className={`flow-pill ${running || toolCalls.length > 0 ? "active" : ""}`}>
          2. 检索证据
        </span>
        <span>→</span>
        <span className={`flow-pill ${verification ? "active" : ""}`}>3. 交叉核验</span>
        <span>→</span>
        <span className={`flow-pill ${stage === "done" ? "active" : ""}`}>4. 结果展示</span>
      </div>

        <div data-testid="tool-calls-panel" className="agent-panel-box">{renderToolCallsContent()}</div>
      </details>
      </div>
    </div>
  );
}
