/**
 * DiagnosticsView — Five-tab diagnostics panel.
 *
 * Tabs: Query Tiers | Contradictions | Decisions | Anomalies | Corrections
 * Each tab fetches its endpoint on first activation (lazy load).
 */
import React, { useState, useCallback } from "react";
import { sendChatSocketPayload } from "../lib/chatTransport";

interface DiagnosticsViewProps {
  httpPort: number;
  wsPort?: number;
  daemonToken?: string;
}

type TabId = "swarmsync" | "tiers" | "contradictions" | "decisions" | "anomalies" | "corrections" | "disagreements" | "epistemic" | "context" | "retrieval" | "habits";

// ---------------------------------------------------------------------------
// SwarmSync Tab
// ---------------------------------------------------------------------------

interface RoutingLiveTest {
  http_status?: number;
  routed_model?: string;
  routing_reason?: string;
  tier?: string;
  complexity_score?: number;
  error?: string;
}

interface RoutingStatus {
  swarmsync_enabled?: boolean;
  swarmsync_api_url?: string;
  default_model?: string;
  swarm_key_present?: boolean;
  openrouter_key_present?: boolean;
  will_use_swarmsync?: boolean;
  live_test?: RoutingLiveTest;
  error?: string;
}

interface SelfTestResult {
  status: "pass" | "fail";
  reason: string;
  details: string[];
}

const boolLabel = (value: boolean | undefined): string => value ? "Present" : "Missing";
const enabledLabel = (value: boolean | undefined): string => value ? "Yes" : "No";

function statusBadgeStyle(ok: boolean): React.CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    borderRadius: 10,
    padding: "2px 8px",
    fontSize: "0.72rem",
    fontWeight: 700,
    background: ok ? "#14532d44" : "#7f1d1d44",
    color: ok ? "#86efac" : "#fca5a5",
    border: `1px solid ${ok ? "#15803d" : "#ef4444"}`,
  };
}

function FieldRow({ label, value, tone }: { label: string; value: React.ReactNode; tone?: "ok" | "warn" | "error" }) {
  const color = tone === "ok" ? "#86efac" : tone === "warn" ? "#fcd34d" : tone === "error" ? "#fca5a5" : "var(--text-primary, #e2e8f0)";
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "8px 0", borderBottom: "1px solid var(--border, #222)" }}>
      <span style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.82rem" }}>{label}</span>
      <span style={{ color, fontSize: "0.82rem", fontWeight: 600, textAlign: "right", overflowWrap: "anywhere" }}>{value || "-"}</span>
    </div>
  );
}

async function runFirstMessageSelfTest(httpPort: number, wsPort: number, daemonToken?: string): Promise<SelfTestResult> {
  const details: string[] = [];
  const base = `http://127.0.0.1:${httpPort}`;

  try {
    const health = await fetch(`${base}/health`);
    if (!health.ok) {
      return { status: "fail", reason: `/health returned HTTP ${health.status}`, details };
    }
    const healthJson = await health.json() as { status?: string };
    if (healthJson.status !== "ok") {
      return { status: "fail", reason: `/health status was ${healthJson.status || "missing"}`, details };
    }
    details.push("Runtime health endpoint returned ok.");
  } catch (e) {
    return { status: "fail", reason: `Runtime health request failed: ${String(e)}`, details };
  }

  try {
    const routing = await fetch(`${base}/api/routing/status`);
    if (!routing.ok) {
      return { status: "fail", reason: `/api/routing/status returned HTTP ${routing.status}`, details };
    }
    const routingJson = await routing.json() as RoutingStatus;
    if (routingJson.error) {
      return { status: "fail", reason: `Routing diagnostics reported: ${routingJson.error}`, details };
    }
    details.push(`Routing diagnostics loaded; will_use_swarmsync=${Boolean(routingJson.will_use_swarmsync)}.`);
  } catch (e) {
    return { status: "fail", reason: `Routing diagnostics request failed: ${String(e)}`, details };
  }

  const token = daemonToken || (window as Window & { __CATO_DAEMON_TOKEN__?: string }).__CATO_DAEMON_TOKEN__;
  if (!token) {
    return {
      status: "fail",
      reason: "Missing daemon token for WebSocket authentication.",
      details,
    };
  }

  return await new Promise<SelfTestResult>((resolve) => {
    const qs = `?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(`ws://127.0.0.1:${wsPort}/ws${qs}`);
    let settled = false;
    const finish = (result: SelfTestResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      ws.onopen = null;
      ws.onmessage = null;
      ws.onerror = null;
      ws.onclose = null;
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
      resolve(result);
    };
    const timer: ReturnType<typeof setTimeout> = setTimeout(() => {
      finish({
        status: "fail",
        reason: "Timed out waiting for WebSocket health roundtrip.",
        details,
      });
    }, 5000);

    ws.onopen = () => {
      details.push("WebSocket opened on the same endpoint used by chat submissions.");
      sendChatSocketPayload(ws, { type: "health" });
    };
    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        const data = JSON.parse(ev.data) as { type?: string; status?: string };
        if (data.type === "health" && data.status === "ok") {
          finish({
            status: "pass",
            reason: "Local first-message transport is ready.",
            details: [...details, "Chat transport serializer/send path returned a health response."],
          });
          return;
        }
        finish({
          status: "fail",
          reason: `Unexpected WebSocket response: ${ev.data.slice(0, 160)}`,
          details,
        });
      } catch (e) {
        finish({
          status: "fail",
          reason: `Invalid WebSocket JSON response: ${String(e)}`,
          details,
        });
      }
    };
    ws.onerror = () => {
      finish({
        status: "fail",
        reason: "WebSocket connection errored before the roundtrip completed.",
        details,
      });
    };
    ws.onclose = () => {
      finish({
        status: "fail",
        reason: "WebSocket closed before returning the health roundtrip.",
        details,
      });
    };
  });
}

function SwarmSyncTab({ httpPort, wsPort, daemonToken }: { httpPort: number; wsPort?: number; daemonToken?: string }) {
  const [data, setData] = useState<RoutingStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selfTesting, setSelfTesting] = useState(false);
  const [selfTest, setSelfTest] = useState<SelfTestResult | null>(null);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/routing/status`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json() as RoutingStatus);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [httpPort]);

  React.useEffect(() => { fetch_(); }, [fetch_]);

  const handleSelfTest = useCallback(async () => {
    setSelfTesting(true);
    setSelfTest(null);
    try {
      setSelfTest(await runFirstMessageSelfTest(httpPort, wsPort ?? httpPort, daemonToken));
    } finally {
      setSelfTesting(false);
    }
  }, [httpPort, wsPort, daemonToken]);

  const live = data?.live_test;
  const hasFailure = Boolean(error || data?.error || live?.error || data?.will_use_swarmsync === false);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
        <div>
          <h3 style={{ fontSize: "0.95rem", marginBottom: 4 }}>SwarmSync Live Status</h3>
          <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.82rem" }}>
            Pulled from /api/routing/status with a live routing probe when SwarmSync is enabled.
          </p>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn-secondary-sm" onClick={fetch_} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
          <button className="btn-primary btn-sm" onClick={handleSelfTest} disabled={selfTesting}>
            {selfTesting ? "Testing..." : "Run self-test"}
          </button>
        </div>
      </div>

      {error && <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>}
      {loading && !data && <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>}

      {data && (
        <div style={{
          border: `1px solid ${hasFailure ? "#f59e0b" : "#15803d"}`,
          borderRadius: 8,
          background: "var(--surface, #1e1e2e)",
          padding: "1rem",
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 8 }}>
            <strong style={{ fontSize: "0.9rem" }}>Routing Readiness</strong>
            <span style={statusBadgeStyle(!hasFailure)}>
              {!hasFailure ? "Ready" : "Attention"}
            </span>
          </div>
          <FieldRow label="SwarmSync key" value={boolLabel(data.swarm_key_present)} tone={data.swarm_key_present ? "ok" : "error"} />
          <FieldRow label="OpenRouter key" value={boolLabel(data.openrouter_key_present)} tone={data.openrouter_key_present ? "ok" : undefined} />
          <FieldRow label="SwarmSync enabled" value={enabledLabel(data.swarmsync_enabled)} tone={data.swarmsync_enabled ? "ok" : "warn"} />
          <FieldRow label="Will use SwarmSync" value={enabledLabel(data.will_use_swarmsync)} tone={data.will_use_swarmsync ? "ok" : "error"} />
          <FieldRow label="Live routed model" value={live?.routed_model || data.default_model} tone={live?.routed_model ? "ok" : undefined} />
          <FieldRow label="Routing reason" value={live?.routing_reason || live?.error || data.error || "No live routing reason reported"} tone={live?.error || data.error ? "error" : undefined} />
          <FieldRow label="Tier" value={live?.tier || "-"} />
          <FieldRow label="Failure state" value={live?.error || data.error || (data.will_use_swarmsync ? "None" : "SwarmSync will not be used")} tone={hasFailure ? "error" : "ok"} />
        </div>
      )}

      <div style={{
        border: "1px solid var(--border, #333)",
        borderRadius: 8,
        background: "var(--surface, #1e1e2e)",
        padding: "1rem",
      }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, marginBottom: 8 }}>
          <div>
            <strong style={{ fontSize: "0.9rem" }}>First Message Self-Test</strong>
            <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.8rem", marginTop: 4 }}>
              Checks runtime health, routing diagnostics, WebSocket auth, and the chat transport send path without invoking a model.
            </p>
          </div>
          {selfTest && (
            <span style={statusBadgeStyle(selfTest.status === "pass")}>
              {selfTest.status === "pass" ? "Pass" : "Fail"}
            </span>
          )}
        </div>
        {selfTest ? (
          <div>
            <p style={{ color: selfTest.status === "pass" ? "#86efac" : "#fca5a5", fontSize: "0.85rem", fontWeight: 700 }}>
              {selfTest.reason}
            </p>
            <ul style={{ marginTop: 8, paddingLeft: "1rem", color: "var(--text-secondary, #aaa)", fontSize: "0.8rem", lineHeight: 1.6 }}>
              {selfTest.details.map((detail) => <li key={detail}>{detail}</li>)}
            </ul>
          </div>
        ) : (
          <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.82rem" }}>
            Run this before a real first request when the daemon was just started or routing keys changed.
          </p>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Query Tiers Tab
// ---------------------------------------------------------------------------

interface TierInfo {
  label: string;
  description: string;
}

interface QueryClassifierData {
  tiers: Record<string, TierInfo>;
  classifier: string;
}

function QueryTiersTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<QueryClassifierData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/query-classifier`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
      setFetched(true);
    }
  }, [httpPort]);

  // Trigger on mount
  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  const TIER_COLORS: Record<string, string> = {
    TIER_A: "#4ade80",
    TIER_B: "#60a5fa",
    TIER_C: "#f59e0b",
  };

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  return (
    <div>
      <p style={{ color: "var(--text-secondary, #aaa)", marginBottom: "1rem", fontSize: "0.85rem" }}>
        Classifier strategy: <strong>{data.classifier}</strong>
      </p>
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
        {Object.entries(data.tiers).map(([key, tier]) => (
          <div
            key={key}
            style={{
              border: `1px solid ${TIER_COLORS[key] ?? "#555"}`,
              borderRadius: "8px",
              padding: "1rem 1.5rem",
              minWidth: "220px",
              flex: "1",
              background: "var(--surface, #1e1e2e)",
            }}
          >
            <div style={{ fontWeight: 700, color: TIER_COLORS[key] ?? "#fff", marginBottom: "0.4rem" }}>
              {key}
            </div>
            <div style={{ fontWeight: 600, marginBottom: "0.3rem" }}>{tier.label}</div>
            <div style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem" }}>{tier.description}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Contradictions Tab
// ---------------------------------------------------------------------------

interface ContradictionHealth {
  total: number;
  unresolved: number;
  resolved: number;
  by_type: Record<string, number>;
  most_contradicted_entities: string[];
  error?: string;
}

function ContradictionsTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<ContradictionHealth | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/contradiction-health`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
      setFetched(true);
    }
  }, [httpPort]);

  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  const unresolvedColor = (data.unresolved ?? 0) > 0 ? "#f59e0b" : "#4ade80";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {data.error && (
        <p style={{ color: "#f59e0b", fontSize: "0.85rem" }}>Warning: {data.error}</p>
      )}

      {/* Summary counts */}
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
        {[
          { label: "Total", value: data.total, color: "#60a5fa" },
          { label: "Resolved", value: data.resolved, color: "#4ade80" },
          { label: "Unresolved", value: data.unresolved, color: unresolvedColor },
        ].map(({ label, value, color }) => (
          <div
            key={label}
            style={{
              border: `1px solid ${color}`,
              borderRadius: "8px",
              padding: "0.75rem 1.25rem",
              minWidth: "120px",
              textAlign: "center",
              background: "var(--surface, #1e1e2e)",
            }}
          >
            <div style={{ fontSize: "1.8rem", fontWeight: 700, color }}>{value ?? 0}</div>
            <div style={{ fontSize: "0.8rem", color: "var(--text-secondary, #aaa)" }}>{label}</div>
          </div>
        ))}
      </div>

      {/* By type */}
      {data.by_type && Object.keys(data.by_type).length > 0 && (
        <div>
          <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>By Type</h4>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border, #333)" }}>
                <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Type</th>
                <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Count</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.by_type).map(([type, count]) => (
                <tr key={type} style={{ borderBottom: "1px solid var(--border, #222)" }}>
                  <td style={{ padding: "0.4rem 0.6rem" }}>{type}</td>
                  <td style={{ padding: "0.4rem 0.6rem", textAlign: "right" }}>{count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Most contradicted entities */}
      {data.most_contradicted_entities && data.most_contradicted_entities.length > 0 && (
        <div>
          <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>Most Contradicted Entities</h4>
          <ul style={{ paddingLeft: "1.2rem", fontSize: "0.85rem", color: "var(--text-secondary, #aaa)" }}>
            {data.most_contradicted_entities.map((e) => <li key={e}>{e}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Decisions Tab
// ---------------------------------------------------------------------------

interface OpenDecision {
  decision_id: string;
  action_taken: string;
  confidence: number;
  timestamp: number;
}

interface OverconfidenceEntry {
  avg_conf: number;
  avg_outcome: number;
  n: number;
}

interface DecisionMemoryData {
  open_decisions: OpenDecision[];
  overconfidence_profile: Record<string, OverconfidenceEntry>;
  error?: string;
}

function DecisionsTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<DecisionMemoryData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/decision-memory`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
      setFetched(true);
    }
  }, [httpPort]);

  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  const formatTs = (ts: number) =>
    ts ? new Date(ts * 1000).toLocaleString() : "-";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {data.error && (
        <p style={{ color: "#f59e0b", fontSize: "0.85rem" }}>Warning: {data.error}</p>
      )}

      <div>
        <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>
          Open Decisions ({data.open_decisions.length})
        </h4>
        {data.open_decisions.length === 0 ? (
          <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem" }}>No open decisions.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8rem" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border, #333)" }}>
                <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>ID</th>
                <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Action</th>
                <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Confidence</th>
                <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Timestamp</th>
              </tr>
            </thead>
            <tbody>
              {data.open_decisions.map((d) => (
                <tr key={d.decision_id} style={{ borderBottom: "1px solid var(--border, #222)" }}>
                  <td style={{ padding: "0.4rem 0.6rem", fontFamily: "monospace", fontSize: "0.72rem", color: "var(--text-secondary, #aaa)" }}>
                    {d.decision_id.slice(0, 8)}...
                  </td>
                  <td style={{ padding: "0.4rem 0.6rem", maxWidth: "260px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {d.action_taken}
                  </td>
                  <td style={{ padding: "0.4rem 0.6rem", textAlign: "right" }}>
                    {(d.confidence * 100).toFixed(0)}%
                  </td>
                  <td style={{ padding: "0.4rem 0.6rem", textAlign: "right", color: "var(--text-secondary, #aaa)" }}>
                    {formatTs(d.timestamp)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div>
        <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>Overconfidence Profile</h4>
        {Object.keys(data.overconfidence_profile).length === 0 ? (
          <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem" }}>No overconfidence patterns detected.</p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8rem" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--border, #333)" }}>
                <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Action Type</th>
                <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Avg Confidence</th>
                <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Avg Outcome</th>
                <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Count</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.overconfidence_profile).map(([action, stats]) => (
                <tr key={action} style={{ borderBottom: "1px solid var(--border, #222)" }}>
                  <td style={{ padding: "0.4rem 0.6rem" }}>{action}</td>
                  <td style={{ padding: "0.4rem 0.6rem", textAlign: "right", color: "#f59e0b" }}>
                    {(stats.avg_conf * 100).toFixed(1)}%
                  </td>
                  <td style={{ padding: "0.4rem 0.6rem", textAlign: "right", color: "#f87171" }}>
                    {stats.avg_outcome.toFixed(2)}
                  </td>
                  <td style={{ padding: "0.4rem 0.6rem", textAlign: "right" }}>{stats.n}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Anomalies Tab
// ---------------------------------------------------------------------------

interface AnomalyDomain {
  domain: string;
  description: string;
  active: boolean;
}

interface AnomalyDomainsData {
  domains: AnomalyDomain[];
  error?: string;
}

function AnomaliesTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<AnomalyDomainsData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/anomaly-domains`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
      setFetched(true);
    }
  }, [httpPort]);

  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  return (
    <div>
      {data.error && (
        <p style={{ color: "#f59e0b", fontSize: "0.85rem", marginBottom: "0.75rem" }}>Warning: {data.error}</p>
      )}
      <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>
        Monitored Domains ({data.domains.length})
      </h4>
      {data.domains.length === 0 ? (
        <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem" }}>No anomaly domains registered.</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.85rem" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border, #333)" }}>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Domain</th>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Description</th>
              <th style={{ textAlign: "center", padding: "0.4rem 0.6rem" }}>Status</th>
            </tr>
          </thead>
          <tbody>
            {data.domains.map((d) => (
              <tr key={d.domain} style={{ borderBottom: "1px solid var(--border, #222)" }}>
                <td style={{ padding: "0.4rem 0.6rem", fontWeight: 600 }}>{d.domain}</td>
                <td style={{ padding: "0.4rem 0.6rem", color: "var(--text-secondary, #aaa)", fontSize: "0.8rem" }}>
                  {d.description || "-"}
                </td>
                <td style={{ padding: "0.4rem 0.6rem", textAlign: "center" }}>
                  <span style={{ color: d.active ? "#4ade80" : "#6b7280", fontSize: "0.78rem" }}>
                    {d.active ? "Active" : "Inactive"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Corrections Tab
// ---------------------------------------------------------------------------

interface CorrectionRecord {
  id: number;
  task_type: string;
  wrong_approach: string;
  correct_approach: string;
  session_id: string;
  timestamp: number;
}

interface SkillCorrectionsData {
  corrections: CorrectionRecord[];
  error?: string;
}

function CorrectionsTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<SkillCorrectionsData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/skill-corrections`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
      setFetched(true);
    }
  }, [httpPort]);

  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  const formatTs = (ts: number) =>
    ts ? new Date(ts * 1000).toLocaleString() : "-";

  return (
    <div>
      {data.error && (
        <p style={{ color: "#f59e0b", fontSize: "0.85rem", marginBottom: "0.75rem" }}>Warning: {data.error}</p>
      )}
      <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>
        Recent Corrections ({data.corrections.length})
      </h4>
      {data.corrections.length === 0 ? (
        <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem" }}>No corrections yet.</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8rem" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border, #333)" }}>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>ID</th>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Task Type</th>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Wrong Approach</th>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Correct Approach</th>
              <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>When</th>
            </tr>
          </thead>
          <tbody>
            {data.corrections.map((c) => (
              <tr key={c.id} style={{ borderBottom: "1px solid var(--border, #222)" }}>
                <td style={{ padding: "0.4rem 0.6rem", color: "var(--text-secondary, #aaa)" }}>{c.id}</td>
                <td style={{ padding: "0.4rem 0.6rem" }}>{c.task_type}</td>
                <td style={{ padding: "0.4rem 0.6rem", maxWidth: "200px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "#f87171" }}>
                  {c.wrong_approach}
                </td>
                <td style={{ padding: "0.4rem 0.6rem", maxWidth: "200px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "#4ade80" }}>
                  {c.correct_approach}
                </td>
                <td style={{ padding: "0.4rem 0.6rem", textAlign: "right", color: "var(--text-secondary, #aaa)", whiteSpace: "nowrap" }}>
                  {formatTs(c.timestamp)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Disagreements Tab
// ---------------------------------------------------------------------------

function DisagreementsTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<{ thresholds: Record<string, number>; info: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/disagreements`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); setFetched(true); }
  }, [httpPort]);
  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  return (
    <div>
      <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem", marginBottom: "1rem" }}>{data.info}</p>
      <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>Disagreement Thresholds</h4>
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap" }}>
        {Object.entries(data.thresholds).map(([type, threshold]) => (
          <div key={type} style={{ border: "1px solid #555", borderRadius: 8, padding: "0.75rem 1.25rem", minWidth: 140, textAlign: "center", background: "var(--surface, #1e1e2e)" }}>
            <div style={{ fontWeight: 700, color: "#60a5fa", marginBottom: "0.3rem" }}>{type}</div>
            <div style={{ fontSize: "1.5rem", fontWeight: 700 }}>{(threshold * 100).toFixed(0)}%</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Epistemic Tab
// ---------------------------------------------------------------------------

function EpistemicTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<{ threshold: number; max_interrupts: number; premise_markers: string[]; info: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/epistemic`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); setFetched(true); }
  }, [httpPort]);
  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  return (
    <div>
      <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem", marginBottom: "1rem" }}>{data.info}</p>
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1.5rem" }}>
        <div style={{ border: "1px solid #4ade80", borderRadius: 8, padding: "0.75rem 1.25rem", textAlign: "center", background: "var(--surface, #1e1e2e)" }}>
          <div style={{ fontSize: "0.8rem", color: "var(--text-secondary, #aaa)" }}>Confidence Threshold</div>
          <div style={{ fontSize: "1.5rem", fontWeight: 700, color: "#4ade80" }}>{(data.threshold * 100).toFixed(0)}%</div>
        </div>
        <div style={{ border: "1px solid #f59e0b", borderRadius: 8, padding: "0.75rem 1.25rem", textAlign: "center", background: "var(--surface, #1e1e2e)" }}>
          <div style={{ fontSize: "0.8rem", color: "var(--text-secondary, #aaa)" }}>Max Interrupts</div>
          <div style={{ fontSize: "1.5rem", fontWeight: 700, color: "#f59e0b" }}>{data.max_interrupts}</div>
        </div>
      </div>
      <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>Premise Markers</h4>
      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        {data.premise_markers.map((m) => (
          <code key={m} style={{ padding: "2px 8px", borderRadius: 4, background: "var(--surface, #2a2a3e)", fontSize: "0.8rem" }}>{m}</code>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Context Budget Tab
// ---------------------------------------------------------------------------

function ContextBudgetTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<{ total: number; slots: Record<string, number>; info: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/context-budget`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); setFetched(true); }
  }, [httpPort]);
  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  const SLOT_COLORS: Record<string, string> = {
    tier0_identity: "#ef4444", tier0_agents: "#f59e0b",
    tier1_skill: "#4ade80", tier1_memory: "#60a5fa",
    tier1_tools: "#a78bfa", tier1_history: "#ec4899",
    headroom: "#6b7280",
  };

  return (
    <div>
      <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem", marginBottom: "1rem" }}>{data.info}</p>
      <div style={{ marginBottom: "1.5rem" }}>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.85rem", marginBottom: "0.35rem" }}>
          <span style={{ color: "var(--text-secondary, #aaa)" }}>Total Budget</span>
          <span style={{ fontWeight: 600 }}>{data.total.toLocaleString()} tokens</span>
        </div>
        {/* Stacked bar */}
        <div style={{ display: "flex", borderRadius: 6, overflow: "hidden", height: 24 }}>
          {Object.entries(data.slots).map(([slot, tokens]) => (
            <div key={slot} title={`${slot}: ${tokens} tokens`} style={{ width: `${(tokens / data.total) * 100}%`, background: SLOT_COLORS[slot] ?? "#555", minWidth: tokens > 0 ? 2 : 0 }} />
          ))}
        </div>
      </div>
      <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
        {Object.entries(data.slots).map(([slot, tokens]) => (
          <div key={slot} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.8rem" }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: SLOT_COLORS[slot] ?? "#555", display: "inline-block" }} />
            <span>{slot}: {tokens}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Retrieval Tab
// ---------------------------------------------------------------------------

function RetrievalTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<{ strategy: string; components: string[]; info: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/retrieval`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); setFetched(true); }
  }, [httpPort]);
  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  return (
    <div>
      <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem", marginBottom: "1rem" }}>{data.info}</p>
      <div style={{ marginBottom: "1rem" }}>
        <span style={{ fontSize: "0.85rem", color: "var(--text-secondary, #aaa)" }}>Strategy: </span>
        <strong>{data.strategy}</strong>
      </div>
      <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>Components</h4>
      <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
        {data.components.map((c) => (
          <div key={c} style={{ border: "1px solid #4ade80", borderRadius: 8, padding: "0.5rem 1rem", background: "var(--surface, #1e1e2e)", fontSize: "0.85rem" }}>
            {c}
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Habits Tab
// ---------------------------------------------------------------------------

function HabitsTab({ httpPort }: { httpPort: number }) {
  const [data, setData] = useState<{ patterns: Array<{ habit_id: string; habit_description: string; evidence_count: number; confidence: number; skill_affinity: string }>; count: number } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetched, setFetched] = useState(false);

  const fetch_ = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/habits`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); setFetched(true); }
  }, [httpPort]);
  React.useEffect(() => { if (!fetched) fetch_(); }, [fetched, fetch_]);

  if (loading) return <p style={{ color: "var(--text-secondary, #aaa)" }}>Loading...</p>;
  if (error) return <p style={{ color: "var(--error, #f87171)" }}>Error: {error}</p>;
  if (!data) return null;

  return (
    <div>
      <h4 style={{ marginBottom: "0.5rem", fontSize: "0.9rem" }}>Inferred Habits ({data.count})</h4>
      {data.patterns.length === 0 ? (
        <p style={{ color: "var(--text-secondary, #aaa)", fontSize: "0.85rem" }}>No habits inferred yet. Patterns emerge after repeated interactions.</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.8rem" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--border, #333)" }}>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Description</th>
              <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Evidence</th>
              <th style={{ textAlign: "right", padding: "0.4rem 0.6rem" }}>Confidence</th>
              <th style={{ textAlign: "left", padding: "0.4rem 0.6rem" }}>Skill</th>
            </tr>
          </thead>
          <tbody>
            {data.patterns.map((p) => (
              <tr key={p.habit_id} style={{ borderBottom: "1px solid var(--border, #222)" }}>
                <td style={{ padding: "0.4rem 0.6rem" }}>{p.habit_description}</td>
                <td style={{ padding: "0.4rem 0.6rem", textAlign: "right" }}>{p.evidence_count}</td>
                <td style={{ padding: "0.4rem 0.6rem", textAlign: "right", color: "#4ade80" }}>{(p.confidence * 100).toFixed(0)}%</td>
                <td style={{ padding: "0.4rem 0.6rem", color: "var(--text-secondary, #aaa)" }}>{p.skill_affinity || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main DiagnosticsView
// ---------------------------------------------------------------------------

const TABS: { id: TabId; label: string }[] = [
  { id: "swarmsync",      label: "SwarmSync" },
  { id: "tiers",          label: "Query Tiers" },
  { id: "contradictions", label: "Contradictions" },
  { id: "decisions",      label: "Decisions" },
  { id: "anomalies",      label: "Anomalies" },
  { id: "corrections",    label: "Corrections" },
  { id: "disagreements",  label: "Disagreements" },
  { id: "epistemic",      label: "Epistemic" },
  { id: "context",        label: "Context Budget" },
  { id: "retrieval",      label: "Retrieval" },
  { id: "habits",         label: "Habits" },
];

function diagnosticsFilename(header: string | null): string {
  const match = header?.match(/filename="?([^"]+)"?/i);
  return match?.[1] || "cato-diagnostics.json";
}

export function DiagnosticsView({ httpPort, wsPort, daemonToken }: DiagnosticsViewProps) {
  const [activeTab, setActiveTab] = useState<TabId>("swarmsync");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const exportDiagnostics = async () => {
    setExporting(true);
    setExportError(null);
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/diagnostics/export?limit=200`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = diagnosticsFilename(r.headers.get("Content-Disposition"));
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

  const tabContentStyle: React.CSSProperties = {
    padding: "1.25rem",
    background: "var(--surface, #1e1e2e)",
    borderRadius: "0 0 8px 8px",
    border: "1px solid var(--border, #333)",
    borderTop: "none",
    minHeight: "300px",
  };

  return (
    <div style={{ padding: "1.5rem" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "1rem", marginBottom: "1.25rem" }}>
        <h2 style={{ margin: 0, fontSize: "1.1rem", fontWeight: 700 }}>
          Diagnostics
        </h2>
        <button className="btn-secondary" onClick={exportDiagnostics} disabled={exporting}>
          {exporting ? "Exporting..." : "Export Diagnostics"}
        </button>
      </div>

      {exportError && <div className="page-error" style={{ marginBottom: "1rem" }}>{exportError}</div>}

      {/* Tab bar */}
      <div style={{ display: "flex", borderBottom: "1px solid var(--border, #333)", marginBottom: "0" }}>
        {TABS.map((tab) => {
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              style={{
                padding: "0.55rem 1rem",
                background: "transparent",
                border: "none",
                borderBottom: isActive ? "2px solid #60a5fa" : "2px solid transparent",
                color: isActive ? "#60a5fa" : "var(--text-secondary, #aaa)",
                fontWeight: isActive ? 700 : 400,
                fontSize: "0.85rem",
                cursor: "pointer",
                transition: "color 0.15s",
              }}
            >
              {tab.label}
            </button>
          );
        })}
      </div>

      {/* Tab content — always mounted so lazy fetch fires on first activation */}
      <div style={tabContentStyle}>
        {activeTab === "swarmsync"      && <SwarmSyncTab      httpPort={httpPort} wsPort={wsPort} daemonToken={daemonToken} />}
        {activeTab === "tiers"          && <QueryTiersTab      httpPort={httpPort} />}
        {activeTab === "contradictions" && <ContradictionsTab  httpPort={httpPort} />}
        {activeTab === "decisions"      && <DecisionsTab       httpPort={httpPort} />}
        {activeTab === "anomalies"      && <AnomaliesTab       httpPort={httpPort} />}
        {activeTab === "corrections"    && <CorrectionsTab     httpPort={httpPort} />}
        {activeTab === "disagreements"  && <DisagreementsTab   httpPort={httpPort} />}
        {activeTab === "epistemic"      && <EpistemicTab       httpPort={httpPort} />}
        {activeTab === "context"        && <ContextBudgetTab   httpPort={httpPort} />}
        {activeTab === "retrieval"      && <RetrievalTab       httpPort={httpPort} />}
        {activeTab === "habits"         && <HabitsTab          httpPort={httpPort} />}
      </div>
    </div>
  );
}

export default DiagnosticsView;
