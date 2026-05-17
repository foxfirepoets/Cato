/**
 * LogsView — Live daemon log stream with level filtering + Model Routing tab.
 */
import React, { useState, useEffect, useCallback, useRef } from "react";

interface LogsViewProps {
  httpPort: number;
}

interface LogEntry {
  ts: number;
  level: string;
  name: string;
  msg: string;
}

interface RoutingEntry {
  ts: number;
  routed_model: string;
  raw_model: string;
  complexity: number;
  has_tools: boolean;
  msg_count: number;
}

const LEVEL_COLORS: Record<string, string> = {
  DEBUG: "#64748b",
  INFO: "#94a3b8",
  WARNING: "#eab308",
  ERROR: "#ef4444",
  CRITICAL: "#dc2626",
};

const MODEL_COLORS: Record<string, string> = {
  claude: "#9B5DE5",
  gpt: "#10B981",
  gemini: "#F77F00",
  codex: "#00D9FF",
  cursor: "#06FFA5",
  minimax: "#64748B",
  swarmsync: "#FF006E",
  llama: "#EF4444",
  mistral: "#3B82F6",
};

function getModelColor(model: string): string {
  const lower = model.toLowerCase();
  for (const [key, color] of Object.entries(MODEL_COLORS)) {
    if (lower.includes(key)) return color;
  }
  return "#94a3b8";
}

function formatModel(model: string): string {
  if (!model) return "unknown";
  // openrouter/provider/model → last two segments
  const parts = model.split("/");
  if (parts.length >= 3) return `${parts[parts.length - 2]}/${parts[parts.length - 1]}`;
  if (parts.length === 2) return parts[1];
  return model;
}

/* ── Daemon Logs Tab ── */
const DaemonLogsTab: React.FC<{ base: string }> = ({ base }) => {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [level, setLevel] = useState("");
  const [limit, setLimit] = useState(200);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);

  const fetchLogs = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: String(limit) });
      if (level) params.set("level", level);
      const r = await fetch(`${base}/api/logs?${params}`);
      setLogs(await r.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [base, level, limit]);

  useEffect(() => { fetchLogs(); }, [fetchLogs]);
  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(fetchLogs, 3000);
    return () => clearInterval(t);
  }, [autoRefresh, fetchLogs]);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  if (loading) return <div className="view-loading"><div className="app-loading-spinner" /></div>;

  return (
    <>
      <div className="page-controls" style={{ marginBottom: 12 }}>
        <select className="settings-select" value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">All Levels</option>
          <option value="DEBUG">DEBUG</option>
          <option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR</option>
        </select>
        <select className="settings-select" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          <option value={50}>50</option>
          <option value={100}>100</option>
          <option value={200}>200</option>
          <option value={500}>500</option>
        </select>
        <label className="toggle-label">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          Auto-refresh
        </label>
        <button className="btn-secondary" onClick={fetchLogs}>Refresh</button>
      </div>
      {error && <div className="page-error">{error}</div>}
      <div className="log-container">
        {logs.length === 0 ? (
          <div className="empty-state">No log entries</div>
        ) : (
          logs.map((entry, i) => (
            <div key={i} className="log-row">
              <span className="log-ts">{new Date(entry.ts * 1000).toLocaleTimeString()}</span>
              <span className="log-level" style={{ color: LEVEL_COLORS[entry.level] ?? "#94a3b8" }}>
                {entry.level.padEnd(8)}
              </span>
              <span className="log-name">{entry.name}</span>
              <span className="log-msg">{entry.msg}</span>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </>
  );
};

/* ── Model Routing Tab ── */
const ModelRoutingTab: React.FC<{ base: string }> = ({ base }) => {
  const [entries, setEntries] = useState<RoutingEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const fetch_ = useCallback(async () => {
    try {
      const r = await fetch(`${base}/api/usage/routing?limit=100`);
      const data = await r.json();
      setEntries(Array.isArray(data) ? data : []);
    } catch { /* silent */ }
    finally { setLoading(false); }
  }, [base]);

  useEffect(() => { fetch_(); }, [fetch_]);
  useEffect(() => {
    if (!autoRefresh) return;
    const t = setInterval(fetch_, 5000);
    return () => clearInterval(t);
  }, [autoRefresh, fetch_]);

  if (loading) return <div className="view-loading"><div className="app-loading-spinner" /></div>;

  // Compute model frequency summary
  const modelCounts: Record<string, number> = {};
  entries.forEach((e) => {
    const label = formatModel(e.routed_model);
    modelCounts[label] = (modelCounts[label] || 0) + 1;
  });
  const sorted = Object.entries(modelCounts).sort((a, b) => b[1] - a[1]);

  return (
    <>
      <div className="page-controls" style={{ marginBottom: 12 }}>
        <label className="toggle-label">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
          Auto-refresh
        </label>
        <button className="btn-secondary" onClick={fetch_}>Refresh</button>
      </div>

      {/* Model frequency summary */}
      {sorted.length > 0 && (
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
          {sorted.map(([model, count]) => (
            <span key={model} style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              padding: "4px 10px", borderRadius: 8, fontSize: 12, fontWeight: 600,
              background: `${getModelColor(model)}18`,
              color: getModelColor(model),
              border: `1px solid ${getModelColor(model)}44`,
            }}>
              {model}
              <span style={{
                background: `${getModelColor(model)}33`, borderRadius: 6,
                padding: "0 5px", fontSize: 11,
              }}>
                {count}
              </span>
            </span>
          ))}
        </div>
      )}

      {/* Routing history table */}
      {entries.length === 0 ? (
        <div className="empty-state">
          No routing decisions yet. Send a message to Cato and model choices will appear here.
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border-secondary, #2a2a3e)", textAlign: "left" }}>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Time</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Model</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Complexity</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Tools</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Messages</th>
              </tr>
            </thead>
            <tbody>
              {[...entries].reverse().map((e, i) => (
                <tr key={i} style={{ borderBottom: "1px solid var(--border-secondary, #1a1a2e)" }}>
                  <td style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 12, color: "var(--text-muted, #64748b)" }}>
                    {new Date(e.ts * 1000).toLocaleTimeString()}
                  </td>
                  <td style={{ padding: "6px 10px" }}>
                    <span style={{
                      display: "inline-block", padding: "2px 8px", borderRadius: 6,
                      fontSize: 12, fontWeight: 600,
                      background: `${getModelColor(e.routed_model)}18`,
                      color: getModelColor(e.routed_model),
                      border: `1px solid ${getModelColor(e.routed_model)}44`,
                    }}>
                      {formatModel(e.routed_model)}
                    </span>
                  </td>
                  <td style={{ padding: "6px 10px" }}>
                    <span style={{
                      display: "inline-block", width: 60, height: 6, borderRadius: 3,
                      background: "var(--border-secondary, #2a2a3e)", position: "relative", overflow: "hidden",
                    }}>
                      <span style={{
                        position: "absolute", left: 0, top: 0, height: "100%", borderRadius: 3,
                        width: `${Math.min(e.complexity * 100, 100)}%`,
                        background: e.complexity > 0.7 ? "#ef4444" : e.complexity > 0.4 ? "#eab308" : "#22c55e",
                      }} />
                    </span>
                    <span style={{ marginLeft: 6, fontSize: 11, color: "var(--text-muted, #64748b)" }}>
                      {(e.complexity * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td style={{ padding: "6px 10px", color: e.has_tools ? "#22c55e" : "var(--text-muted, #64748b)", fontSize: 12 }}>
                    {e.has_tools ? "Yes" : "No"}
                  </td>
                  <td style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 12, color: "var(--text-muted, #64748b)" }}>
                    {e.msg_count}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
};

/* ── Main LogsView ── */
export const LogsView: React.FC<LogsViewProps> = ({ httpPort }) => {
  const base = `http://127.0.0.1:${httpPort}`;
  const [tab, setTab] = useState<"logs" | "routing">("logs");

  return (
    <div className="page-view">
      <div className="page-header">
        <h1 className="page-title">Logs</h1>
        <div className="page-controls">
          <div style={{ display: "flex", gap: 0, borderRadius: 6, overflow: "hidden", border: "1px solid var(--border-secondary, #2a2a3e)" }}>
            <button
              onClick={() => setTab("logs")}
              style={{
                padding: "5px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer",
                border: "none",
                background: tab === "logs" ? "var(--accent-primary, #6366f1)" : "transparent",
                color: tab === "logs" ? "#fff" : "var(--text-muted, #94a3b8)",
              }}
            >
              Daemon Logs
            </button>
            <button
              onClick={() => setTab("routing")}
              style={{
                padding: "5px 14px", fontSize: 12, fontWeight: 600, cursor: "pointer",
                border: "none",
                borderLeft: "1px solid var(--border-secondary, #2a2a3e)",
                background: tab === "routing" ? "var(--accent-primary, #6366f1)" : "transparent",
                color: tab === "routing" ? "#fff" : "var(--text-muted, #94a3b8)",
              }}
            >
              Model Routing
            </button>
          </div>
        </div>
      </div>

      {tab === "logs" ? <DaemonLogsTab base={base} /> : <ModelRoutingTab base={base} />}
    </div>
  );
};

export default LogsView;
