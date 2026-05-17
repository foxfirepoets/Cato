/**
 * ActivityIndicator.tsx — Shows whether Cato is actively working on a task.
 *
 * Two data sources (whichever fires first wins):
 *   1. WebSocket "activity" events (instant, pushed by gateway)
 *   2. Polling GET /api/activity every 2s (fallback if WS drops a message)
 *
 * Renders a compact pill: green "Idle" or amber pulsing "Working… <task>"
 */

import { useState, useEffect, useRef, useCallback } from "react";

interface ActivityState {
  busy: boolean;
  task: string;
  sessionId: string;
  /**
   * BH-011 — Currently-running tool (e.g. "shell.exec(pip install -r …)" )
   * pushed by gateway when a tool dispatch starts and cleared when it ends.
   * Null between tools / when idle.
   */
  currentTool: string | null;
  /** Epoch seconds when current_tool started. Drives the tool-elapsed timer. */
  toolStartedAt: number | null;
}

interface ActivityIndicatorProps {
  httpPort: number;
  /** An existing WebSocket reference to listen for "activity" events */
  wsRef?: React.RefObject<WebSocket | null>;
}

export function ActivityIndicator({ httpPort, wsRef }: ActivityIndicatorProps) {
  const [activity, setActivity] = useState<ActivityState>({
    busy: false,
    task: "",
    sessionId: "",
    currentTool: null,
    toolStartedAt: null,
  });
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef<number>(0);
  // BH-011 — Live "tool has been running for N seconds" counter, ticks once
  // per second while a current_tool is set.  Separate from the turn-level
  // `elapsed` so the user sees both: total turn time AND current-step time.
  const [toolElapsed, setToolElapsed] = useState(0);

  // Track elapsed time while busy
  useEffect(() => {
    if (!activity.busy) {
      setElapsed(0);
      startRef.current = 0;
      return;
    }
    if (!startRef.current) startRef.current = Date.now();
    const t = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    }, 1000);
    return () => clearInterval(t);
  }, [activity.busy]);

  // Poll /api/activity as fallback (1s interval for responsiveness)
  const poll = useCallback(async () => {
    try {
      const r = await fetch(`http://127.0.0.1:${httpPort}/api/activity`);
      if (r.ok) {
        const data = await r.json();
        const newBusy = !!data.busy;
        setActivity((prev) => {
          // Only log transitions to avoid console spam
          if (prev.busy !== newBusy) {
            console.log(`[ActivityIndicator] ${newBusy ? "BUSY" : "IDLE"}`, data.task || "");
          }
          if (prev.currentTool !== (data.current_tool || null)) {
            console.log(`[ActivityIndicator] tool=${data.current_tool || "(none)"}`);
          }
          return {
            busy: newBusy,
            task: data.task || "",
            sessionId: data.session_id || "",
            currentTool: data.current_tool || null,
            toolStartedAt: data.tool_started_at ?? null,
          };
        });
      }
    } catch {
      // daemon unreachable — leave state as-is
    }
  }, [httpPort]);

  useEffect(() => {
    poll();
    const t = setInterval(poll, 1000);
    return () => clearInterval(t);
  }, [poll]);

  // Listen for WS "activity" events (instant push)
  useEffect(() => {
    if (!wsRef?.current) return;
    const ws = wsRef.current;
    const handler = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === "activity") {
          setActivity({
            busy: !!data.busy,
            task: data.task || "",
            sessionId: data.session_id || "",
            currentTool: data.current_tool || null,
            toolStartedAt: data.tool_started_at ?? null,
          });
        }
      } catch {
        // not JSON or not activity — ignore
      }
    };
    ws.addEventListener("message", handler);
    return () => ws.removeEventListener("message", handler);
  }, [wsRef]);

  // BH-011 — Tick the tool-elapsed counter while a current_tool is set.
  useEffect(() => {
    if (!activity.currentTool || !activity.toolStartedAt) {
      setToolElapsed(0);
      return;
    }
    const start = activity.toolStartedAt * 1000;
    const update = () =>
      setToolElapsed(Math.max(0, Math.floor((Date.now() - start) / 1000)));
    update();
    const t = setInterval(update, 1000);
    return () => clearInterval(t);
  }, [activity.currentTool, activity.toolStartedAt]);

  const formatElapsed = (s: number): string => {
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${s % 60}s`;
  };

  const truncateTask = (t: string, max = 50) =>
    t.length > max ? t.slice(0, max) + "…" : t;

  // BH-011 — Prefer the live tool label over the user-task preview when a
  // tool is in flight.  Tool labels are already shaped by
  // `_summarize_tool_call` on the backend so they fit on one line.
  const primaryLabel = activity.busy
    ? activity.currentTool
      ? `Running: ${truncateTask(activity.currentTool, 70)}`
      : activity.task
        ? `Working: ${truncateTask(activity.task)}`
        : "Working…"
    : "Idle";

  const fullTooltip = activity.busy
    ? activity.currentTool
      ? `${activity.currentTool}\n— turn task: ${activity.task || "(no preview)"}`
      : `Working on: ${activity.task}`
    : "Agent is idle";

  return (
    <div
      className={`activity-indicator ${activity.busy ? "activity-busy" : "activity-idle"}`}
      title={fullTooltip}
    >
      {activity.busy && <span className="activity-spinner" />}
      <span className="activity-label">{primaryLabel}</span>
      {activity.busy && activity.currentTool && toolElapsed > 2 && (
        <span className="activity-tool-elapsed" style={{ marginLeft: 6, opacity: 0.75 }}>
          ({formatElapsed(toolElapsed)})
        </span>
      )}
      {activity.busy && elapsed > 0 && (
        <span className="activity-elapsed">{formatElapsed(elapsed)}</span>
      )}
    </div>
  );
}
