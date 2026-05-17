/**
 * DashboardView — Live dashboard home page.
 * Polls /health, /api/budget/summary, /api/sessions, /api/usage/summary
 */
import React, { useState, useEffect, useCallback } from "react";
import { ActivityIndicator } from "../components/ActivityIndicator";

import type { View } from "../components/Sidebar";

interface DashboardViewProps {
  httpPort: number;
  onNavigate: (view: View) => void;
}

interface HealthData {
  status: string;
  version: string;
  sessions: number;
  uptime: number;
}

interface BudgetData {
  session_spend: number;
  session_cap: number;
  monthly_spend: number;
  monthly_cap: number;
  monthly_pct_remaining: number;
  monthly_calls: number;
  total_spend_all_time: number;
}

interface SessionEntry {
  session_id: string;
  queue_depth: number;
  running: boolean;
}

interface UsageData {
  total_calls?: number;
  total_tokens?: number;
  model_breakdown?: Record<string, number>;
}

interface AdapterEntry {
  name: string;
  status: "connected" | "disconnected" | "not_configured";
  details: Record<string, unknown>;
}

interface HeartbeatData {
  last_heartbeat: string | null;
  agent_name: string | null;
  uptime_seconds: number | null;
  status: "alive" | "stale" | "unknown";
}

function formatUptime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

interface MetricCardProps {
  label: string;
  value: React.ReactNode;
  sub?: string;
  accent?: string;
}

const MetricCard: React.FC<MetricCardProps> = ({ label, value, sub, accent }) => (
  <div className="dash-card" style={accent ? { borderTop: `3px solid ${accent}` } : {}}>
    <div className="dash-card-label">{label}</div>
    <div className="dash-card-value">{value}</div>
    {sub && <div className="dash-card-sub">{sub}</div>}
  </div>
);

export const DashboardView: React.FC<DashboardViewProps> = ({ httpPort, onNavigate }) => {
  const base = `http://127.0.0.1:${httpPort}`;
  const [health, setHealth] = useState<HealthData | null>(null);
  const [budget, setBudget] = useState<BudgetData | null>(null);
  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [usage, setUsage] = useState<UsageData | null>(null);
  const [adapters, setAdapters] = useState<AdapterEntry[] | null>(null);
  const [adapterError, setAdapterError] = useState(false);
  const [heartbeat, setHeartbeat] = useState<HeartbeatData | null>(null);
  const [heartbeatError, setHeartbeatError] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchAll = useCallback(async () => {
    try {
      const [h, b, s, u] = await Promise.allSettled([
        fetch(`${base}/health`).then((r) => r.json()),
        fetch(`${base}/api/budget/summary`).then((r) => r.json()),
        fetch(`${base}/api/sessions`).then((r) => r.json()),
        fetch(`${base}/api/usage/summary`).then((r) => r.json()),
      ]);
      if (h.status === "fulfilled") setHealth(h.value as HealthData);
      if (b.status === "fulfilled") setBudget(b.value as BudgetData);
      if (s.status === "fulfilled") setSessions((s.value as SessionEntry[]) || []);
      if (u.status === "fulfilled") setUsage(u.value as UsageData);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [base]);

  const fetchAdapters = useCallback(async () => {
    try {
      const data = await fetch(`${base}/api/adapters`).then((r) => r.json());
      setAdapters((data.adapters as AdapterEntry[]) || []);
      setAdapterError(false);
    } catch {
      setAdapterError(true);
    }
  }, [base]);

  const fetchHeartbeat = useCallback(async () => {
    try {
      const data = await fetch(`${base}/api/heartbeat`).then((r) => r.json());
      setHeartbeat(data as HeartbeatData);
      setHeartbeatError(false);
    } catch {
      setHeartbeatError(true);
    }
  }, [base]);

  useEffect(() => {
    fetchAll();
    const t = setInterval(fetchAll, 10000);
    return () => clearInterval(t);
  }, [fetchAll]);

  useEffect(() => {
    fetchAdapters();
    fetchHeartbeat();
    const t = setInterval(() => {
      fetchAdapters();
      fetchHeartbeat();
    }, 30000);
    return () => clearInterval(t);
  }, [fetchAdapters, fetchHeartbeat]);

  if (loading) {
    return (
      <div className="dash-loading">
        <div className="app-loading-spinner" />
        <p>Loading dashboard…</p>
      </div>
    );
  }

  const monthPct = budget?.monthly_pct_remaining ?? 100;
  const monthColor = monthPct > 40 ? "#22c55e" : monthPct > 15 ? "#eab308" : "#ef4444";

  return (
    <div className="dash-view">
      <div className="dash-header">
        <h1 className="dash-title">Dashboard</h1>
        <ActivityIndicator httpPort={httpPort} />
        {error && <span className="dash-error-badge">⚠ {error}</span>}
        <button className="dash-refresh-btn" onClick={fetchAll}>Refresh</button>
      </div>

      <div className="dash-grid">
        {/* Gateway status */}
        <MetricCard
          label="Gateway Status"
          accent={health?.status === "ok" ? "#22c55e" : "#ef4444"}
          value={
            <span style={{ color: health?.status === "ok" ? "#22c55e" : "#ef4444", fontWeight: 700 }}>
              {health?.status === "ok" ? "Online" : "Offline"}
            </span>
          }
          sub={health ? `v${health.version} · up ${formatUptime(health.uptime)}` : "—"}
        />

        {/* Active sessions */}
        <MetricCard
          label="Active Sessions"
          accent="#3b82f6"
          value={sessions.length}
          sub={`${sessions.filter((s) => s.running).length} running`}
        />

        {/* Monthly spend */}
        <MetricCard
          label="Monthly Spend"
          accent={monthColor}
          value={`$${budget?.monthly_spend.toFixed(4) ?? "0.0000"}`}
          sub={`$${budget?.monthly_cap ?? 20} cap · ${monthPct.toFixed(0)}% remaining`}
        />

        {/* Session spend */}
        <MetricCard
          label="Session Spend"
          accent="#a855f7"
          value={`$${budget?.session_spend.toFixed(4) ?? "0.0000"}`}
          sub={`$${budget?.session_cap ?? 1} cap per session`}
        />

        {/* Total calls */}
        <MetricCard
          label="Total API Calls"
          value={budget?.monthly_calls ?? 0}
          sub="this month"
        />

        {/* All-time spend */}
        <MetricCard
          label="All-Time Spend"
          value={`$${budget?.total_spend_all_time.toFixed(2) ?? "0.00"}`}
          sub="since install"
        />
      </div>

      {/* Hard cap badge */}
      <div className="dash-cap-badge">
        <span className="dash-cap-icon">🛡</span>
        <span>
          <strong>Hard spending caps enforced</strong> — $
          {budget?.session_cap ?? 1}/session · $
          {budget?.monthly_cap ?? 20}/month. OpenClaw has no caps.
        </span>
      </div>

      {/* Active sessions list */}
      {sessions.length > 0 && (
        <div className="dash-section">
          <div className="dash-section-title">Active Sessions</div>
          <div className="dash-session-list">
            {sessions.map((s) => (
              <div key={s.session_id} className="dash-session-row">
                <span className={`status-dot ${s.running ? "status-ready" : "status-stopped"}`} />
                <code className="dash-session-id">{s.session_id}</code>
                <span className="dash-session-meta">
                  {s.running ? "running" : "idle"} · queue: {s.queue_depth}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Usage model breakdown */}
      {usage?.model_breakdown && Object.keys(usage.model_breakdown).length > 0 && (
        <div className="dash-section">
          <div className="dash-section-title">Model Usage</div>
          <div className="dash-model-list">
            {Object.entries(usage.model_breakdown).map(([model, calls]) => (
              <div key={model} className="dash-model-row">
                <span className="dash-model-name">{model}</span>
                <span className="dash-model-calls">{calls} calls</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Adapters */}
      <div className="dash-section">
        <div className="dash-section-title">Adapters</div>
        {adapterError ? (
          <div className="dash-section-empty">Adapter status unavailable</div>
        ) : adapters === null ? (
          <div className="dash-section-empty">Loading…</div>
        ) : adapters.length === 0 ? (
          <div className="dash-section-empty">No adapters configured</div>
        ) : (
          <div className="dash-adapter-list">
            {adapters.map((adapter) => {
              const color =
                adapter.status === "connected"
                  ? "#22c55e"
                  : adapter.status === "disconnected"
                  ? "#eab308"
                  : "#6b7280";
              return (
                <span
                  key={adapter.name}
                  className="dash-adapter-pill"
                  style={{ borderColor: color, color }}
                >
                  <span
                    className="status-dot"
                    style={{ backgroundColor: color, display: "inline-block", marginRight: 6 }}
                  />
                  {adapter.name}
                  <span className="dash-adapter-status"> {adapter.status}</span>
                </span>
              );
            })}
          </div>
        )}
      </div>

      {/* Heartbeat */}
      <div className="dash-section">
        <div className="dash-section-title">Heartbeat</div>
        {heartbeatError ? (
          <div className="dash-section-empty">Heartbeat unavailable</div>
        ) : heartbeat === null ? (
          <div className="dash-section-empty">Loading…</div>
        ) : (
          <div className="dash-heartbeat-row">
            {heartbeat.agent_name && (
              <span className="dash-heartbeat-agent">{heartbeat.agent_name}</span>
            )}
            <span
              className="dash-adapter-pill"
              style={{
                borderColor:
                  heartbeat.status === "alive"
                    ? "#22c55e"
                    : heartbeat.status === "stale"
                    ? "#eab308"
                    : "#6b7280",
                color:
                  heartbeat.status === "alive"
                    ? "#22c55e"
                    : heartbeat.status === "stale"
                    ? "#eab308"
                    : "#6b7280",
              }}
            >
              {heartbeat.status}
            </span>
            {heartbeat.last_heartbeat && (
              <span className="dash-session-meta">
                last: {new Date(heartbeat.last_heartbeat).toLocaleTimeString()}
              </span>
            )}
            {heartbeat.uptime_seconds !== null && (
              <span className="dash-session-meta">
                up {formatUptime(Math.floor(heartbeat.uptime_seconds))}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Quick launch */}
      <div className="dash-section">
        <div className="dash-section-title">Quick Launch</div>
        <div className="dash-quick-btns">
          <button className="dash-quick-btn" onClick={() => onNavigate("chat")}>
            💬 New Chat
          </button>
          <button className="dash-quick-btn" onClick={() => onNavigate("cron")}>
            ⏱ New Cron Job
          </button>
          <button className="dash-quick-btn" onClick={() => onNavigate("skills")}>
            🧩 Browse Skills
          </button>
        </div>
      </div>
    </div>
  );
};

export default DashboardView;
