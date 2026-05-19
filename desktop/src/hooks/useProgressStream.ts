/**
 * useProgressStream.ts — Claude-Code-style live work feed.
 *
 * Listens for `progress` events on the supplied WebSocket and assembles
 * them into a per-session structured state the ChatView can render.
 *
 * Event vocabulary (mirrors `cato/progress.py`):
 *
 *   turn_start    { turn, max_turns }
 *   llm_start     { model, token_budget }
 *   llm_token     { delta }         (may be batched / coarse)
 *   llm_end       { elapsed_s, tokens_used }
 *   tool_start    { tool, args_preview, call_id }
 *   tool_end      { call_id, tool, success, elapsed_s, summary }
 *   turn_end      { turn, has_final_answer }
 *   session_end   { total_turns, total_elapsed_s }
 *
 * All events carry { type: "progress", session_id, event, timestamp, ... }.
 *
 * The hook exposes a flat ordered event log per session plus a structured
 * `turns[]` view; the component picks whichever is convenient.  Old events
 * past MAX_EVENTS are dropped (the feed is for "now", not history).
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type ProgressKind =
  | "turn_start"
  | "llm_start"
  | "llm_token"
  | "llm_end"
  | "tool_start"
  | "tool_end"
  | "turn_end"
  | "session_end";

export interface ProgressEvent {
  type: "progress";
  session_id: string;
  event: ProgressKind;
  timestamp: number;
  // turn_start / turn_end
  turn?: number;
  max_turns?: number;
  has_final_answer?: boolean;
  // llm_start / llm_end
  model?: string;
  token_budget?: number;
  tokens_used?: number;
  elapsed_s?: number;
  // llm_token
  delta?: string;
  // tool_start / tool_end
  tool?: string;
  args_preview?: string;
  call_id?: string;
  success?: boolean;
  summary?: string;
  // session_end
  total_turns?: number;
  total_elapsed_s?: number;
}

export interface ToolFeedItem {
  callId: string;
  tool: string;
  argsPreview: string;
  status: "running" | "ok" | "error";
  elapsedS: number;
  summary: string;
}

export interface TurnFeedItem {
  turn: number;
  maxTurns: number;
  thinking: string;        // accumulated llm_token deltas for this turn
  llmStarted: boolean;
  llmEnded: boolean;
  llmModel: string;
  llmElapsedS: number;
  tools: ToolFeedItem[];
  hasFinalAnswer: boolean;
  done: boolean;
}

export interface ProgressFeedState {
  /** Active session id; empty when idle. */
  sessionId: string;
  /** Most recent event timestamp (ms) — drives "stale" detection. */
  lastEventTs: number;
  /** Structured per-turn data. */
  turns: TurnFeedItem[];
  /** True between session_start (first turn_start) and session_end. */
  active: boolean;
  /** Aggregate elapsed seconds reported at session_end (0 while running). */
  totalElapsedS: number;
}

const MAX_TURNS_KEPT = 20;
const MAX_THINKING_CHARS = 4000;

const EMPTY: ProgressFeedState = {
  sessionId: "",
  lastEventTs: 0,
  turns: [],
  active: false,
  totalElapsedS: 0,
};

export interface UseProgressStreamResult {
  state: ProgressFeedState;
  /** Force-clear the feed (used when the next user message is sent). */
  reset: () => void;
}

export function useProgressStream(
  wsRef?: React.RefObject<WebSocket | null>,
): UseProgressStreamResult {
  const [state, setState] = useState<ProgressFeedState>(EMPTY);

  const reset = useCallback(() => {
    setState(EMPTY);
  }, []);

  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const applyEvent = useCallback((evt: ProgressEvent) => {
    setState((prev) => {
      // New session id resets state (one user request at a time per WS).
      const sessionMatches = !prev.sessionId || prev.sessionId === evt.session_id;
      let turns = sessionMatches ? [...prev.turns] : [];

      switch (evt.event) {
        case "turn_start": {
          const turn: TurnFeedItem = {
            turn: evt.turn ?? turns.length + 1,
            maxTurns: evt.max_turns ?? 10,
            thinking: "",
            llmStarted: false,
            llmEnded: false,
            llmModel: "",
            llmElapsedS: 0,
            tools: [],
            hasFinalAnswer: false,
            done: false,
          };
          turns.push(turn);
          if (turns.length > MAX_TURNS_KEPT) {
            turns = turns.slice(-MAX_TURNS_KEPT);
          }
          break;
        }
        case "llm_start": {
          const t = turns[turns.length - 1];
          if (t) {
            t.llmStarted = true;
            t.llmModel = evt.model ?? "";
          }
          break;
        }
        case "llm_token": {
          const t = turns[turns.length - 1];
          if (t && evt.delta) {
            const next = t.thinking + evt.delta;
            t.thinking =
              next.length > MAX_THINKING_CHARS
                ? "…" + next.slice(-MAX_THINKING_CHARS)
                : next;
          }
          break;
        }
        case "llm_end": {
          const t = turns[turns.length - 1];
          if (t) {
            t.llmEnded = true;
            t.llmElapsedS = evt.elapsed_s ?? 0;
          }
          break;
        }
        case "tool_start": {
          const t = turns[turns.length - 1];
          if (t) {
            t.tools.push({
              callId: evt.call_id || `${evt.tool}-${t.tools.length}`,
              tool: evt.tool ?? "",
              argsPreview: evt.args_preview ?? "",
              status: "running",
              elapsedS: 0,
              summary: "",
            });
          }
          break;
        }
        case "tool_end": {
          const t = turns[turns.length - 1];
          if (t) {
            const idx = t.tools.findIndex(
              (tt) =>
                tt.status === "running" &&
                (tt.callId === evt.call_id || tt.tool === evt.tool),
            );
            if (idx >= 0) {
              t.tools[idx] = {
                ...t.tools[idx],
                status: evt.success === false ? "error" : "ok",
                elapsedS: evt.elapsed_s ?? 0,
                summary: evt.summary ?? "",
              };
            }
          }
          break;
        }
        case "turn_end": {
          const t = turns[turns.length - 1];
          if (t) {
            t.done = true;
            t.hasFinalAnswer = !!evt.has_final_answer;
          }
          break;
        }
        case "session_end": {
          return {
            sessionId: evt.session_id,
            lastEventTs: evt.timestamp,
            turns,
            active: false,
            totalElapsedS: evt.total_elapsed_s ?? 0,
          };
        }
        default:
          break;
      }

      return {
        sessionId: evt.session_id,
        lastEventTs: evt.timestamp,
        turns,
        active: true,
        totalElapsedS: 0,
      };
    });
  }, []);

  // Listen on the supplied WS for `progress` events.
  useEffect(() => {
    if (!wsRef?.current) return;
    const ws = wsRef.current;
    const handler = (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data) as ProgressEvent;
        if (data?.type !== "progress") return;
        applyEvent(data);
      } catch {
        // not JSON or not a progress event — ignore
      }
    };
    ws.addEventListener("message", handler);
    return () => ws.removeEventListener("message", handler);
  }, [wsRef, applyEvent]);

  return { state, reset };
}

export default useProgressStream;
