import { useEffect, useRef, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  apiJson,
  type AgentQueryResponse,
  type AgentVerification,
  type ToolCallItem,
} from "../api";

function isSafeHttpUrl(raw: string | undefined): boolean {
  if (!raw) return false;
  try {
    const u = new URL(raw.trim());
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

const PRESET_PROMPTS = [
  "我想知道最近3天a股电力行业的情况",
  "从长期价值投资角度分析 000858 五粮液的估值与安全边际",
  "结合行情与财务数据给出 A 股日频量化交易策略建议",
];

export default function AgentWorkspace() {
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState("auto");
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
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    return () => {
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, []);

  const fallbackQuery = async (cleanPrompt: string) => {
    try {
      const res = await apiJson<AgentQueryResponse>("/api/agent/query", {
        method: "POST",
        body: JSON.stringify({ prompt: cleanPrompt, mode }),
      });
      setToolCalls(res.tool_calls ?? []);
      setVerification(res.verification ?? null);
      setAnswer(res.answer || "未返回有效回答。");
      setSources(res.sources ?? []);
      setDurationMs(res.duration_ms ?? null);
      setStage("done");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Agent 查询失败";
      setRunError(msg);
      setAnswer(`执行失败：${msg}`);
      setStage("error");
    } finally {
      setRunning(false);
    }
  };

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const cleanPrompt = prompt.trim();
    if (!cleanPrompt) {
      setValidationErr("请输入研究问题或投资需求后再提交（不能为空）。");
      return;
    }
    setValidationErr("");
    setRunError("");
    setRunning(true);
    setStage("running");
    setToolCalls([]);
    setVerification(null);
    setAnswer("");
    setSources([]);
    setDurationMs(null);

    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    let finished = false;
    let receivedAnyEvent = false;
    const streamUrl = `/api/agent/stream?prompt=${encodeURIComponent(cleanPrompt)}&mode=${encodeURIComponent(mode)}`;
    const es = new EventSource(streamUrl);
    esRef.current = es;

    es.addEventListener("status", () => {
      receivedAnyEvent = true;
    });

    es.addEventListener("tool_start", (ev) => {
      receivedAnyEvent = true;
      try {
        const data = JSON.parse((ev as MessageEvent).data) as {
          tool_call_id?: string;
          name?: string;
          args?: Record<string, unknown>;
        };
        setToolCalls((prev) => [
          ...prev,
          {
            tool_call_id: data.tool_call_id,
            name: data.name || "mcp_tool",
            args: data.args || {},
            status: "running",
          },
        ]);
      } catch {
        // ignore malformed chunk
      }
    });

    es.addEventListener("tool_end", (ev) => {
      receivedAnyEvent = true;
      try {
        const data = JSON.parse((ev as MessageEvent).data) as {
          tool_call_id?: string;
          name?: string;
          args?: Record<string, unknown>;
          result_summary?: string;
          duration_ms?: number;
          is_error?: boolean;
        };
        setToolCalls((prev) => {
          const idx = prev.findIndex(
            (item) =>
              (data.tool_call_id && item.tool_call_id === data.tool_call_id) ||
              (item.name === data.name && item.status === "running")
          );
          const updated: ToolCallItem = {
            tool_call_id: data.tool_call_id,
            name: data.name || "mcp_tool",
            args: data.args || {},
            result_summary: data.result_summary || "",
            duration_ms: data.duration_ms,
            is_error: Boolean(data.is_error),
            status: data.is_error ? "error" : "done",
          };
          if (idx >= 0) {
            const copy = [...prev];
            copy[idx] = updated;
            return copy;
          }
          return [...prev, updated];
        });
      } catch {
        // ignore malformed chunk
      }
    });

    es.addEventListener("verification", (ev) => {
      receivedAnyEvent = true;
      try {
        const data = JSON.parse((ev as MessageEvent).data) as AgentVerification;
        setVerification(data);
      } catch {
        // ignore malformed chunk
      }
    });

    es.addEventListener("final_answer", (ev) => {
      receivedAnyEvent = true;
      finished = true;
      try {
        const data = JSON.parse((ev as MessageEvent).data) as {
          answer?: string;
          sources?: string[];
          duration_ms?: number;
        };
        setAnswer(data.answer || "已完成分析，但未生成正文。");
        setSources(data.sources || []);
        setDurationMs(data.duration_ms ?? null);
        setStage("done");
      } catch {
        setStage("done");
      } finally {
        setRunning(false);
        es.close();
        if (esRef.current === es) esRef.current = null;
      }
    });

    es.addEventListener("error", (ev) => {
      const msgEv = ev as MessageEvent;
      if (msgEv && typeof msgEv.data === "string" && msgEv.data.trim()) {
        finished = true;
        try {
          const data = JSON.parse(msgEv.data) as { detail?: string };
          const msg = data.detail || "SSE 流执行出错";
          setRunError(msg);
          setAnswer(`执行失败：${msg}`);
        } catch {
          setRunError("SSE 流执行出错");
          setAnswer("执行失败：SSE 流执行出错");
        }
        setStage("error");
        setRunning(false);
        es.close();
        if (esRef.current === es) esRef.current = null;
      }
    });

    es.onerror = () => {
      if (finished) {
        es.close();
        if (esRef.current === es) esRef.current = null;
        return;
      }
      es.close();
      if (esRef.current === es) esRef.current = null;
      if (!receivedAnyEvent) {
        void fallbackQuery(cleanPrompt);
      } else {
        setRunError("实时流连接中断，正在尝试回退请求...");
        void fallbackQuery(cleanPrompt);
      }
    };
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
    return "等待回答...";
  };

  return (
    <div>
      <div className="flow-steps">
        <span className={`flow-pill ${prompt.trim() ? "active" : ""}`}>1. 用户需求</span>
        <span>→</span>
        <span className={`flow-pill ${running || toolCalls.length > 0 ? "active" : ""}`}>
          2. Pi Agent + JEVDB MCP 工具调用
        </span>
        <span>→</span>
        <span className={`flow-pill ${verification ? "active" : ""}`}>3. 交叉核验</span>
        <span>→</span>
        <span className={`flow-pill ${stage === "done" ? "active" : ""}`}>4. 结果展示</span>
      </div>

      <form onSubmit={handleSubmit} className="controls-row">
        <input
          data-testid="agent-input"
          className="input input-grow"
          type="text"
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value);
            if (validationErr && e.target.value.trim()) {
              setValidationErr("");
            }
          }}
          placeholder="输入投研问题，例如：我想知道最近3天a股电力行业的情况"
          aria-label="研究问题输入框"
        />
        <select
          className="select"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          aria-label="研究模式"
        >
          <option value="auto">智能模式 (Auto)</option>
          <option value="value">价值投资 (Value)</option>
          <option value="quant">量化策略 (Quant)</option>
        </select>
        <button
          data-testid="agent-submit"
          className="btn"
          type="submit"
          disabled={running}
        >
          {running ? "研判执行中..." : "发起投研分析"}
        </button>
      </form>

      <div className="controls-row">
        <span className="muted">快捷示例：</span>
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

      <div className="grid-2">
        <div className="card">
          <h3 className="card-title">工具调用链路 (Tool Calls Trace)</h3>
          <div data-testid="tool-calls-panel" className="agent-panel-box">
            {renderToolCallsContent()}
          </div>
        </div>

        <div className="card">
          <div className="card-head-row">
            <h3 className="card-title" style={{ margin: 0 }}>
              核验结论与依据 (Verified Research Answer)
              {durationMs ? ` · ${durationMs}ms` : ""}
            </h3>
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
          <div data-testid="agent-answer" className="agent-panel-box md-answer">
            {renderAnswerContent()}
          </div>

          {verification && (
            <div className="info-banner" style={{ marginTop: 10, marginBottom: 0 }}>
              核验状态：{verification.grounded ?? verification.passed ? "通过 (Grounded)" : "存在待核验项"}
              {Array.isArray(verification.ungrounded_numbers) &&
                verification.ungrounded_numbers.length > 0 &&
                ` · 未落地数值: ${verification.ungrounded_numbers.join(", ")}`}
              {Array.isArray(verification.warnings) &&
                verification.warnings.length > 0 &&
                ` · 提示: ${verification.warnings.join("; ")}`}
            </div>
          )}

          {sources.length > 0 && (
            <div className="source-badges">
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
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
