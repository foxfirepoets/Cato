/**
 * CodingAgentView.tsx — Full coding agent page with three-panel layout.
 * Extracted and adapted from cato/ui/pages/CodingAgentPage.tsx.
 */

import React, { useCallback, useState } from "react";
import { TalkPage } from "../components/TalkPage";
import type { SynthesisResult } from "../components/TalkPage";
import { TaskInput } from "../components/TaskInput";
import { ConfidenceBadge } from "../components/ConfidenceBadge";
import { ModelSettings } from "../components/ModelSettings";
import { useTalkPageStream } from "../hooks/useTalkPageStream";
import { useLocalStorage } from "../hooks/useLocalStorage";
import logoSrc from "../assets/cato-logo.png";

interface CodingAgentViewProps {
  wsBase?: string;
  apiBase?: string;
  daemonToken?: string;
}

interface RecentTask {
  taskId: string;
  task: string;
  createdAt: number;
}

const ALL_MODELS = ["codex", "cursor", "claude", "gemini"] as const;
const MODEL_CONFIG: Record<string, { label: string; color: string }> = {
  codex:  { label: "Codex",   color: "#F59E0B" },
  cursor: { label: "Cursor",  color: "#22D3EE" },
  claude: { label: "Claude",  color: "#3B82F6" },
  gemini: { label: "Gemini",  color: "#A855F7" },
};
const MAX_RECENT_TASKS = 10;

interface RightSidebarProps {
  synthesis: SynthesisResult | null;
  isLoading: boolean;
  onCopy: (text: string) => void;
  onSave: (text: string, model: string) => void;
  taskId: string;
  copiedState: boolean;
}

const RightSidebar: React.FC<RightSidebarProps> = ({
  synthesis, isLoading, onCopy, onSave, copiedState,
}) => {
  if (isLoading && !synthesis) {
    return (
      <aside className="sidebar-right" aria-label="Results panel">
        <div className="sidebar-header-section"><span>Results</span></div>
        <div className="sidebar-content">
          <p className="empty-state">Awaiting responses...</p>
        </div>
      </aside>
    );
  }
  if (!synthesis) return null;

  const { primary, runners_up } = synthesis;
  const modelCfg = MODEL_CONFIG[primary.model.toLowerCase()] ?? { label: primary.model, color: "#94a3b8" };

  return (
    <aside className="sidebar-right" aria-label="Synthesis results">
      <div className="sidebar-header-section">
        <span>Results</span>
        <span style={{ color: "#86efac", fontSize: "11px" }}>Complete</span>
      </div>
      <div className="sidebar-content">
        <div className="synthesis-sidebar-result">
          <div className="synthesis-label">
            <span className="synthesis-label-icon" aria-hidden="true">{"\u2605"}</span>
            Primary Answer
          </div>
          <div className="synthesis-model" style={{ color: modelCfg.color }}>
            {modelCfg.label}
            <ConfidenceBadge confidence={primary.confidence} />
          </div>
          <p className="synthesis-text" style={{ maxHeight: 200, overflowY: "auto" }}>
            {primary.response}
          </p>
          <div className="synthesis-actions">
            <button className={`action-btn ${copiedState ? "copied" : ""}`} onClick={() => onCopy(primary.response)}>
              {copiedState ? "\u2713 Copied" : "Copy"}
            </button>
            <button className="action-btn" onClick={() => onSave(primary.response, primary.model)}>
              Save
            </button>
          </div>
        </div>
        {runners_up.length > 0 && (
          <div>
            <div className="sidebar-header-section" style={{ border: "none", padding: "10px 0 6px" }}>
              <span>Alternatives</span>
            </div>
            {runners_up.map((alt, idx) => {
              const altCfg = MODEL_CONFIG[alt.model.toLowerCase()] ?? { label: alt.model, color: "#94a3b8" };
              return (
                <div key={idx} className="synthesis-sidebar-result" style={{ marginBottom: 8 }}>
                  <div className="synthesis-model" style={{ color: altCfg.color }}>
                    {altCfg.label}
                    <ConfidenceBadge confidence={alt.confidence} />
                  </div>
                  <p className="synthesis-text" style={{ maxHeight: 120, overflowY: "auto", fontSize: 12 }}>
                    {alt.response}
                  </p>
                  <button className="action-btn" style={{ marginTop: 8 }} onClick={() => onCopy(alt.response)}>
                    Copy
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
};

export const CodingAgentView: React.FC<CodingAgentViewProps> = ({ wsBase, apiBase, daemonToken }) => {
  const [taskId, setTaskId] = useState<string | null>(null);
  const [taskDescription, setTaskDescription] = useState("");
  const [copiedState, setCopiedState] = useState(false);
  const [recentTasks, setRecentTasks] = useLocalStorage<RecentTask[]>("cato-recent-tasks", []);
  const [showSettings, setShowSettings] = useState(false);

  const { messages, isLoading, synthesis, error, connectionStatus, cancel } =
    useTalkPageStream(taskId ?? "", wsBase, daemonToken);

  React.useEffect(() => {
    if (!synthesis || !taskDescription || !taskId) return;
    setRecentTasks((prev) => {
      if (prev.some((t) => t.taskId === taskId)) return prev;
      return [
        { taskId, task: taskDescription, createdAt: Date.now() },
        ...prev,
      ].slice(0, MAX_RECENT_TASKS);
    });
  }, [synthesis, taskId, taskDescription, setRecentTasks]);

  const handleCopy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard may not be available
    }
    setCopiedState(true);
    setTimeout(() => setCopiedState(false), 2000);
  }, []);

  const handleSave = useCallback((text: string, model: string) => {
    // Sanitize model name to prevent path traversal in filename (KRAK-3)
    const safeModel = model.replace(/[^a-z0-9-]/gi, "_").slice(0, 32);
    const blob = new Blob([text], { type: "text/plain" });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement("a");
    a.href     = url;
    a.download = `cato-${safeModel}-result.txt`;
    // Append to DOM so Firefox/Safari trigger the download reliably (ARCH-6)
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, []);

  const handleTaskCreated = useCallback((newTaskId: string) => {
    setTaskId(newTaskId);
  }, []);

  if (!taskId) {
    // Entry view — task input form
    return (
      <div className="coding-entry">
        <div className="coding-entry-card">
          <div className="coding-entry-icon">
              <img src={logoSrc} alt="Cato" style={{ width: "100%", height: "100%", objectFit: "contain", borderRadius: "12px" }} />
            </div>
          <h1 className="coding-entry-title">Cato Coding Agent</h1>
          <p className="coding-entry-subtitle">
            Submit a task to Codex, Cursor, Claude, and Gemini
          </p>
          <TaskInput
            onTaskCreated={handleTaskCreated}
            apiBase={apiBase ?? `http://127.0.0.1:8080`}
          />
        </div>
        {recentTasks.length > 0 && (
          <div className="coding-recent">
            <h2 className="coding-recent-title">Recent Tasks</h2>
            <nav>
              {recentTasks.map((rt) => (
                <button
                  key={rt.taskId}
                  className="recent-task-item"
                  onClick={() => {
                    setTaskId(rt.taskId);
                    setTaskDescription(rt.task);
                  }}
                >
                  {rt.task.slice(0, 80)}{rt.task.length > 80 ? "\u2026" : ""}
                </button>
              ))}
            </nav>
          </div>
        )}
      </div>
    );
  }

  return (
    <main className="coding-agent-page">
      <aside className="sidebar-left" aria-label="Task details">
        <div className="sidebar-header-section">
          <span>Task</span>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {isLoading && (
              <button
                className="btn-cancel-sm"
                onClick={cancel}
                title="Cancel task"
              >
                ✕ Cancel
              </button>
            )}
            <button
              className="settings-gear-btn"
              onClick={() => setShowSettings((s) => !s)}
              aria-label="Model settings"
              title="Model settings"
            >
              ⚙
            </button>
          </div>
        </div>
        <div className="sidebar-content">
          {showSettings && (
            <div style={{ marginBottom: 16 }}>
              <ModelSettings
                apiBase={apiBase ?? "http://127.0.0.1:8080"}
                onClose={() => setShowSettings(false)}
              />
            </div>
          )}
          <TaskInput
            readOnly={isLoading}
            defaultTask={taskDescription}
            onTaskCreated={handleTaskCreated}
            apiBase={apiBase ?? `http://127.0.0.1:8080`}
          />
          {recentTasks.length > 0 && (
            <div style={{ marginTop: 20 }}>
              <div className="sidebar-header-section" style={{ border: "none", padding: "0 0 6px" }}>
                <span>Recent Tasks</span>
              </div>
              <nav>
                {recentTasks.map((rt) => (
                  <button
                    key={rt.taskId}
                    className={`recent-task-item ${rt.taskId === taskId ? "active" : ""}`}
                    onClick={() => {
                      setTaskId(rt.taskId);
                      setTaskDescription(rt.task);
                    }}
                  >
                    {rt.task.slice(0, 60)}{rt.task.length > 60 ? "\u2026" : ""}
                  </button>
                ))}
              </nav>
            </div>
          )}
        </div>
      </aside>

      <div className="talk-main">
        {error && !isLoading && (
          <div className="error-banner" role="alert" style={{ margin: 16 }}>
            <span>Error: {error}</span>
          </div>
        )}
        <TalkPage
          task={taskDescription || `Task ${taskId}`}
          models={[...ALL_MODELS]}
          messages={messages}
          isLoading={isLoading}
          synthesis={synthesis}
          error={error}
          connectionStatus={connectionStatus}
        />
      </div>

      <RightSidebar
        synthesis={synthesis}
        isLoading={isLoading}
        onCopy={handleCopy}
        onSave={handleSave}
        taskId={taskId}
        copiedState={copiedState}
      />
    </main>
  );
};

export default CodingAgentView;
