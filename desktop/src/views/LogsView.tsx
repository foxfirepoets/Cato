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
  timestamp?: string;
  request_id?: string;
  routed_model: string;
  chosen_model?: string;
  raw_model: string;
  routing_reason?: string;
  tier?: string;
  considered_models?: string[];
  estimated_cost?: number | string | null;
  actual_cost?: number | string | null;
  fallback_routing?: boolean;
  success?: boolean;
  status?: string;
  error?: string;
  complexity: number;
  complexity_score?: number;
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

function formatCost(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (n === 0) return "$0.0000";
  return n < 0.0001 ? `$${n.toExponential(2)}` : `$${n.toFixed(4)}`;
}

function isRoutingSuccess(entry: RoutingEntry): boolean {
  if (typeof entry.success === "boolean") return entry.success;
  return entry.status === "ok";
}

function formatRouteTime(entry: RoutingEntry): string {
  if (entry.timestamp) return new Date(entry.timestamp).toLocaleString();
  if (entry.ts) return new Date(entry.ts * 1000).toLocaleString();
  return "-";
}

function filenameFromDisposition(header: string | null): string {
  const match = header?.match(/filename="?([^"]+)"?/i);
  return match?.[1] || "cato-diagnostics.json";
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
  const [logPath, setLogPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(true);

  const fetch_ = useCallback(async () => {
    try {
      const r = await fetch(`${base}/api/usage/routing?limit=100`);
      const data = await r.json();
      if (Array.isArray(data)) {
        setEntries(data);
        setLogPath("");
      } else {
        setEntries(Array.isArray(data.events) ? data.events : []);
        setLogPath(data.log_path || "");
      }
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
    const label = formatModel(e.routed_model || e.chosen_model || "");
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
      {logPath && (
        <div style={{ marginBottom: 12, fontSize: 12, color: "var(--text-muted, #64748b)", overflowWrap: "anywhere" }}>
          Persistent log: <code>{logPath}</code>
        </div>
      )}

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
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Request</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Model</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Why</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Considered</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Cost</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>State</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Complexity</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Tools</th>
                <th style={{ padding: "8px 10px", color: "var(--text-muted, #64748b)" }}>Messages</th>
              </tr>
            </thead>
            <tbody>
              {[...entries].reverse().map((e, i) => {
                const success = isRoutingSuccess(e);
                return (
                <tr key={i} style={{ borderBottom: "1px solid var(--border-secondary, #1a1a2e)" }}>
                  <td style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 12, color: "var(--text-muted, #64748b)" }}>
                    {formatRouteTime(e)}
                  </td>
                  <td
                    title={e.request_id || ""}
                    style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 11, color: "var(--text-muted, #64748b)" }}
                  >
                    {(e.request_id || "-").slice(0, 12)}
                  </td>
                  <td style={{ padding: "6px 10px" }}>
                    <span style={{
                      display: "inline-block", padding: "2px 8px", borderRadius: 6,
                      fontSize: 12, fontWeight: 600,
                      background: `${getModelColor(e.routed_model || e.chosen_model || "")}18`,
                      color: getModelColor(e.routed_model || e.chosen_model || ""),
                      border: `1px solid ${getModelColor(e.routed_model || e.chosen_model || "")}44`,
                    }}>
                      {formatModel(e.routed_model || e.chosen_model || "")}
                    </span>
                  </td>
                  <td style={{ padding: "6px 10px", maxWidth: 240, fontSize: 12, color: "var(--text-muted, #64748b)" }}>
                    <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={e.routing_reason || ""}>
                      {e.routing_reason || "-"}
                    </div>
                    {e.tier && <div style={{ color: "#60a5fa", fontSize: 11 }}>{e.tier}</div>}
                  </td>
                  <td style={{ padding: "6px 10px", maxWidth: 220, fontSize: 12, color: "var(--text-muted, #64748b)" }}>
                    <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={(e.considered_models || []).join(", ")}>
                      {(e.considered_models || []).join(", ") || "-"}
                    </div>
                  </td>
                  <td style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 12, color: "var(--text-muted, #64748b)" }}>
                    est {formatCost(e.estimated_cost)}<br />
                    act {formatCost(e.actual_cost)}
                  </td>
                  <td style={{ padding: "6px 10px", fontSize: 12 }}>
                    <span style={{
                      display: "inline-block", padding: "2px 7px", borderRadius: 6, fontWeight: 700,
                      color: success ? "#86efac" : "#fca5a5",
                      background: success ? "#14532d44" : "#7f1d1d44",
                      border: `1px solid ${success ? "#15803d" : "#ef4444"}`,
                    }}>
                      {success ? "OK" : "FAIL"}
                    </span>
                    {e.fallback_routing && <div style={{ marginTop: 4, color: "#fcd34d" }}>fallback</div>}
                    {e.error && <div style={{ marginTop: 4, color: "#fca5a5", maxWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={e.error}>{e.error}</div>}
                  </td>
                  <td style={{ padding: "6px 10px" }}>
                    <span style={{
                      display: "inline-block", width: 60, height: 6, borderRadius: 3,
                      background: "var(--border-secondary, #2a2a3e)", position: "relative", overflow: "hidden",
                    }}>
                      <span style={{
                        position: "absolute", left: 0, top: 0, height: "100%", borderRadius: 3,
                        width: `${Math.min((e.complexity_score ?? e.complexity) * 100, 100)}%`,
                        background: (e.complexity_score ?? e.complexity) > 0.7 ? "#ef4444" : (e.complexity_score ?? e.complexity) > 0.4 ? "#eab308" : "#22c55e",
                      }} />
                    </span>
                    <span style={{ marginLeft: 6, fontSize: 11, color: "var(--text-muted, #64748b)" }}>
                      {((e.complexity_score ?? e.complexity) * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td style={{ padding: "6px 10px", color: e.has_tools ? "#22c55e" : "var(--text-muted, #64748b)", fontSize: 12 }}>
                    {e.has_tools ? "Yes" : "No"}
                  </td>
                  <td style={{ padding: "6px 10px", fontFamily: "monospace", fontSize: 12, color: "var(--text-muted, #64748b)" }}>
                    {e.msg_count}
                  </td>
                </tr>
                );
              })}
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
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const exportDiagnostics = async () => {
    setExporting(true);
    setExportError(null);
    try {
      const r = await fetch(`${base}/api/diagnostics/export?limit=200`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filenameFromDisposition(r.headers.get("Content-Disposition"));
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setExportError(String(e));
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="page-view">
      <div className="page-header">
        <h1 className="page-title">Logs</h1>
        <div className="page-controls">
          <button className="btn-secondary" onClick={exportDiagnostics} disabled={exporting}>
            {exporting ? "Exporting..." : "Export Diagnostics"}
          </button>
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

      {exportError && <div className="page-error">{exportError}</div>}
      {tab === "logs" ? <DaemonLogsTab base={base} /> : <ModelRoutingTab base={base} />}
    </div>
  );
};

export default LogsView;
