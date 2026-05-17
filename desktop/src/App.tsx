/**
 * App.tsx — Root component for Cato Desktop.
 *
 * Sidebar layout: left nav + main content area.
 * Polls the daemon health endpoint until ready.
 */

import { useState, useEffect } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Sidebar, type View } from "./components/Sidebar";
import { ChatView } from "./views/ChatView";
import type { ChatConnectionStatus } from "./hooks/useChatStream";
import { CodingAgentView } from "./views/CodingAgentView";
import { InteractiveCLIView } from "./views/InteractiveCLIView";
import { DashboardView } from "./views/DashboardView";
import { SessionsView } from "./views/SessionsView";
import { SkillsView } from "./views/SkillsView";
import { CronView } from "./views/CronView";
import { UsageView } from "./views/UsageView";
import { LogsView } from "./views/LogsView";
import { AuditLogView } from "./views/AuditLogView";
import { ConfigView } from "./views/ConfigView";
import { BudgetView } from "./views/BudgetView";
import { AlertsView } from "./views/AlertsView";
import { AuthKeysView } from "./views/AuthKeysView";
import { IdentityView } from "./views/IdentityView";
import { FlowsView } from "./views/FlowsView";
import { NodesView } from "./views/NodesView";
import { SystemView } from "./views/SystemView";
import { MemoryView } from "./views/MemoryView";
import { DiagnosticsView } from "./views/DiagnosticsView";
import "./styles/app.css";

type DaemonStatus = "starting" | "ready" | "stopped" | "error";

interface DaemonInfo {
  httpPort: number;
  wsPort: number;
  status: DaemonStatus;
  daemonToken?: string;
}

const DAEMON_DEFAULT_PORT = 8080;

function useDaemonInfo(): DaemonInfo {
  const [info, setInfo] = useState<DaemonInfo>({
    httpPort: DAEMON_DEFAULT_PORT,
    wsPort: DAEMON_DEFAULT_PORT,
    status: "starting",
  });

  useEffect(() => {
    let cancelled = false;
    let attempts = 0;
    const maxAttempts = 120;

    const poll = async () => {
      while (!cancelled && attempts < maxAttempts) {
        try {
          const status = await invoke<{
            running: boolean;
            http_port: number;
            ws_port: number;
            daemon_token?: string | null;
          }>("get_daemon_status");
          if (status.running) {
            installCatoFetchAuth(status.daemon_token ?? undefined);
            setInfo({
              httpPort: status.http_port,
              wsPort: status.ws_port,
              status: "ready",
              daemonToken: status.daemon_token ?? undefined,
            });
            return;
          }
        } catch {
          // Daemon not yet ready
        }
        attempts++;
        await new Promise((r) => setTimeout(r, 1000));
      }
      if (!cancelled) {
        setInfo((prev) => ({ ...prev, status: "error" }));
      }
    };
    poll();
    return () => { cancelled = true; };
  }, []);

  return info;
}

function installCatoFetchAuth(token?: string): void {
  if (!token) return;
  const w = window as Window & {
    __CATO_DAEMON_TOKEN__?: string;
    __CATO_FETCH_PATCHED__?: boolean;
    __CATO_ORIGINAL_FETCH__?: typeof window.fetch;
  };
  w.__CATO_DAEMON_TOKEN__ = token;
  if (w.__CATO_FETCH_PATCHED__) return;

  const originalFetch = window.fetch.bind(window);
  w.__CATO_ORIGINAL_FETCH__ = originalFetch;
  w.__CATO_FETCH_PATCHED__ = true;

  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const rawUrl = input instanceof Request ? input.url : String(input);
    const isCatoLocal =
      rawUrl.startsWith("http://127.0.0.1:") ||
      rawUrl.startsWith("http://localhost:");

    if (!isCatoLocal || !w.__CATO_DAEMON_TOKEN__) {
      return originalFetch(input, init);
    }

    const headers = new Headers(
      init?.headers ?? (input instanceof Request ? input.headers : undefined),
    );
    if (!headers.has("X-Cato-Token")) {
      headers.set("X-Cato-Token", w.__CATO_DAEMON_TOKEN__);
    }

    if (input instanceof Request) {
      return originalFetch(new Request(input, { ...init, headers }));
    }
    return originalFetch(input, { ...init, headers });
  };
}

function renderView(view: View, daemon: DaemonInfo, onNavigate: (v: View) => void): React.ReactNode {
  const { httpPort, wsPort } = daemon;
  switch (view) {
    case "dashboard":
      return <DashboardView httpPort={httpPort} onNavigate={onNavigate} />;
    case "chat":
      return <ChatView wsBase={`127.0.0.1:${wsPort}`} httpPort={httpPort} daemonToken={daemon.daemonToken} />;
    case "coding-agent":
      return (
        <CodingAgentView
          wsBase={`127.0.0.1:${httpPort}`}
          apiBase={`http://127.0.0.1:${httpPort}`}
          daemonToken={daemon.daemonToken}
        />
      );
    case "interactive-cli":
      return <InteractiveCLIView httpPort={httpPort} />;
    case "skills":
      return <SkillsView httpPort={httpPort} />;
    case "cron":
      return <CronView httpPort={httpPort} />;
    case "sessions":
      return <SessionsView httpPort={httpPort} />;
    case "usage":
      return <UsageView httpPort={httpPort} />;
    case "logs":
      return <LogsView httpPort={httpPort} />;
    case "audit":
      return <AuditLogView httpPort={httpPort} />;
    case "config":
      return <ConfigView httpPort={httpPort} />;
    case "budget":
      return <BudgetView httpPort={httpPort} />;
    case "alerts":
      return <AlertsView httpPort={httpPort} />;
    case "auth-keys":
      return <AuthKeysView httpPort={httpPort} />;
    case "identity":
      return <IdentityView httpPort={httpPort} />;
    case "flows":
      return <FlowsView httpPort={httpPort} />;
    case "nodes":
      return <NodesView httpPort={httpPort} />;
    case "memory":
      return <MemoryView httpPort={httpPort} />;
    case "system":
      return <SystemView httpPort={httpPort} />;
    case "diagnostics":
      return <DiagnosticsView httpPort={httpPort} />;
    default:
      return null;
  }
}

function App() {
  const [view, setView] = useState<View>("dashboard");
  const daemon = useDaemonInfo();
  const [chatStatus, setChatStatus] = useState<ChatConnectionStatus | "idle">("idle");

  // Allow child views to trigger navigation (e.g. quick-launch buttons)
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<string>).detail as View;
      if (detail) setView(detail);
    };
    window.addEventListener("cato-navigate", handler);
    return () => window.removeEventListener("cato-navigate", handler);
  }, []);

  // Derive a sidebar daemon status that also reflects chat WebSocket health
  // when the daemon is otherwise reported as ready.
  let sidebarStatus: DaemonStatus = daemon.status;
  if (daemon.status === "ready") {
    if (chatStatus === "connecting" || chatStatus === "reconnecting") {
      sidebarStatus = "starting";
    } else if (chatStatus === "disconnected") {
      sidebarStatus = "error";
    }
  }

  return (
    <div className="app-root app-root-sidebar">
      <Sidebar
        activeView={view}
        onNavigate={setView}
        daemonStatus={sidebarStatus}
      />

      <div className="app-content">
        {daemon.status === "starting" && (
          <div className="app-loading">
            <div className="app-loading-spinner" />
            <p>Starting Cato daemon...</p>
          </div>
        )}

        {daemon.status === "ready" && (
          <main className="app-main">
            <ErrorBoundary>
              {view === "chat"
                ? (
                  <ChatView
                    wsBase={`127.0.0.1:${daemon.wsPort}`}
                    httpPort={daemon.httpPort}
                    daemonToken={daemon.daemonToken}
                    onConnectionStatusChange={setChatStatus}
                  />
                  )
                : renderView(view, daemon, setView)}
            </ErrorBoundary>
          </main>
        )}

        {daemon.status === "error" && (
          <div className="app-error">
            <p>Failed to connect to Cato daemon.</p>
            <button className="retry-btn" onClick={() => window.location.reload()}>
              Retry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
