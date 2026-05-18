/**
 * useTalkPageStream.ts — WebSocket hook for streaming model responses.
 *
 * Connects to /ws/coding-agent/{taskId} and handles all event types.
 * Adapted for desktop: accepts configurable wsBase for sidecar connection.
 */

import {
  useState,
  useEffect,
  useRef,
  useCallback,
  type MutableRefObject,
} from "react";
import type { TalkMessage } from "../components/MessageBubble";
import type { SynthesisResult } from "../components/TalkPage";

export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected"
  | "closed";

export type CancelFn = () => void;

export interface UseTalkPageStreamResult {
  messages: TalkMessage[];
  isLoading: boolean;
  synthesis: SynthesisResult | null;
  error: string | null;
  connectionStatus: ConnectionStatus;
  messagesEndRef: MutableRefObject<HTMLDivElement | null>;
  cancel: CancelFn;
}

const MAX_RETRIES          = 5;
const INITIAL_BACKOFF_MS   = 500;
const MAX_BACKOFF_MS       = 16_000;
// Server sends heartbeats every 30 s — give it 45 s before declaring reconnecting
const HEARTBEAT_TIMEOUT_MS = 45_000;
const TASK_TIMEOUT_MS      = 5_000;

export function useTalkPageStream(
  taskId: string,
  wsBase?: string,
  daemonToken?: string,
): UseTalkPageStreamResult {
  const [messages,         setMessages]         = useState<TalkMessage[]>([]);
  const [isLoading,        setIsLoading]        = useState<boolean>(true);
  const [synthesis,        setSynthesis]        = useState<SynthesisResult | null>(null);
  const [error,            setError]            = useState<string | null>(null);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("connecting");

  const messagesEndRef    = useRef<HTMLDivElement | null>(null);
  const wsRef             = useRef<WebSocket | null>(null);
  const retriesRef        = useRef<number>(0);
  const heartbeatTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const taskTimerRef      = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedRef         = useRef<boolean>(false);
  const messagesRef       = useRef<TalkMessage[]>([]);
  const synthesisRef      = useRef<SynthesisResult | null>(null);
  const connectRef        = useRef<() => void>(() => {});

  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  useEffect(() => {
    synthesisRef.current = synthesis;
  }, [synthesis]);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  const resetHeartbeatTimer = useCallback(() => {
    if (heartbeatTimerRef.current) clearTimeout(heartbeatTimerRef.current);
    heartbeatTimerRef.current = setTimeout(() => {
      setConnectionStatus((prev) =>
        prev === "connected" ? "reconnecting" : prev,
      );
    }, HEARTBEAT_TIMEOUT_MS);
  }, []);

  const clearHeartbeatTimer = useCallback(() => {
    if (heartbeatTimerRef.current) {
      clearTimeout(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
  }, []);

  const startTaskTimeout = useCallback(() => {
    if (taskTimerRef.current) clearTimeout(taskTimerRef.current);
    taskTimerRef.current = setTimeout(() => {
      const msgs = messagesRef.current;
      if (msgs.length > 0 && !synthesisRef.current) {
        const best = [...msgs].sort((a, b) => b.confidence - a.confidence)[0];
        setSynthesis({
          primary: {
            model:            best.model,
            response:         best.text,
            confidence:       best.confidence,
            confidence_level: best.confidence >= 0.9 ? "high" : best.confidence >= 0.7 ? "medium" : "low",
          },
          runners_up: msgs
            .filter((m) => m.id !== best.id)
            .map((m) => ({
              model:            m.model,
              response:         m.text,
              confidence:       m.confidence,
              confidence_level: m.confidence >= 0.9 ? "high" : m.confidence >= 0.7 ? "medium" : "low",
            })),
          early_exit: false,
        });
      }
      setIsLoading(false);
    }, TASK_TIMEOUT_MS);
  }, []);

  const clearTaskTimeout = useCallback(() => {
    if (taskTimerRef.current) {
      clearTimeout(taskTimerRef.current);
      taskTimerRef.current = null;
    }
  }, []);

  const handleMessage = useCallback(
    (raw: string) => {
      let parsed: { event: string; data: Record<string, unknown> };
      try {
        parsed = JSON.parse(raw.trimEnd());
      } catch {
        console.warn("[useTalkPageStream] Unparseable message:", raw);
        return;
      }

      const { event, data } = parsed;
      resetHeartbeatTimer();

      switch (event) {
        case "heartbeat":
          break;

        case "claude_response":
        case "codex_response":
        case "gemini_response": {
          const model = event.replace("_response", "");
          // Runtime validation — coerce types safely
          const rawId = typeof data.id === "string" ? data.id : crypto.randomUUID();
          const rawTimestamp = typeof data.timestamp === "number" ? data.timestamp : Date.now();
          const rawText = typeof data.text === "string" ? data.text.slice(0, 100_000) : "";
          const rawConfidence = typeof data.confidence === "number"
            ? Math.max(0, Math.min(1, data.confidence))
            : 0.75;
          const rawReasoning = typeof data.reasoning === "string"
            ? data.reasoning.slice(0, 50_000) : undefined;
          const rawCode = typeof data.code === "string"
            ? data.code.slice(0, 100_000) : undefined;

          const msg: TalkMessage = {
            id: rawId,
            model,
            timestamp: rawTimestamp,
            text: rawText,
            confidence: rawConfidence,
            reasoning: rawReasoning,
            code: rawCode,
          };
          setMessages((prev) => [...prev, msg]);
          scrollToBottom();
          startTaskTimeout();
          break;
        }

        case "synthesis_complete": {
          // Validate synthesis data structure
          const primary = data.primary as Record<string, unknown> | undefined;
          if (!primary || typeof primary.model !== "string" || typeof primary.response !== "string") {
            console.warn("[useTalkPageStream] Invalid synthesis_complete data");
            break;
          }
          const syn: SynthesisResult = {
            primary: {
              model: primary.model,
              response: String(primary.response).slice(0, 100_000),
              confidence: typeof primary.confidence === "number" ? primary.confidence : 0,
              confidence_level: typeof primary.confidence_level === "string" ? primary.confidence_level : "low",
            },
            runners_up: Array.isArray(data.runners_up)
              ? (data.runners_up as Array<Record<string, unknown>>).map((r) => ({
                  model: String(r.model ?? ""),
                  response: String(r.response ?? "").slice(0, 100_000),
                  confidence: typeof r.confidence === "number" ? r.confidence : 0,
                  confidence_level: typeof r.confidence_level === "string" ? r.confidence_level : "low",
                }))
              : [],
            early_exit: Boolean(data.early_exit),
          };
          setSynthesis(syn);
          setIsLoading(false);
          clearTaskTimeout();
          closedRef.current = true;
          break;
        }

        case "early_termination":
          setIsLoading(false);
          clearTaskTimeout();
          break;

        case "error":
          setError((data.message as string) ?? "Unknown error");
          break;

        case "status":
          break;

        default:
          console.warn("[useTalkPageStream] Unknown event:", event);
      }
    },
    [resetHeartbeatTimer, scrollToBottom, startTaskTimeout, clearTaskTimeout],
  );

  const connect = useCallback(() => {
    if (closedRef.current) return;
    // Don't connect until a real task ID exists (empty string = no task submitted yet)
    if (!taskId || taskId.trim() === "") return;

    // KRAK-4: validate wsBase is localhost-only — desktop app never connects to external hosts
    const rawHost = wsBase ?? "127.0.0.1:8080";
    const host = /^127\.0\.0\.1:\d+$/.test(rawHost) ? rawHost : "127.0.0.1:8080";
    const token = daemonToken || (window as Window & { __CATO_DAEMON_TOKEN__?: string }).__CATO_DAEMON_TOKEN__;
    const url  = `ws://${host}/ws/coding-agent/${encodeURIComponent(taskId)}`;

    setConnectionStatus("connecting");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (token) ws.send(JSON.stringify({ type: "auth", token }));
      setConnectionStatus("connected");
      retriesRef.current = 0;
      resetHeartbeatTimer();
      startTaskTimeout();
    };

    ws.onmessage = (ev: MessageEvent<string>) => {
      handleMessage(ev.data);
    };

    ws.onerror = (ev) => {
      console.error("[useTalkPageStream] WebSocket error:", ev);
    };

    ws.onclose = () => {
      clearHeartbeatTimer();
      if (closedRef.current) {
        setConnectionStatus("closed");
        return;
      }
      if (retriesRef.current < MAX_RETRIES) {
        const delay = Math.min(
          INITIAL_BACKOFF_MS * 2 ** retriesRef.current,
          MAX_BACKOFF_MS,
        );
        retriesRef.current += 1;
        setConnectionStatus("reconnecting");
        setTimeout(() => connectRef.current(), delay);
      } else {
        setConnectionStatus("disconnected");
        setIsLoading(false);
        setError("Connection lost after multiple retries.");
      }
    };
  }, [
    taskId, wsBase, daemonToken, handleMessage, resetHeartbeatTimer,
    clearHeartbeatTimer, startTaskTimeout,
  ]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  useEffect(() => {
    closedRef.current = false;
    retriesRef.current = 0;
    const initialConnect = setTimeout(() => {
      connectRef.current();
    }, 0);
    return () => {
      clearTimeout(initialConnect);
      closedRef.current = true;
      clearHeartbeatTimer();
      clearTaskTimeout();
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [taskId, connect, clearHeartbeatTimer, clearTaskTimeout]);

  const cancel = useCallback(() => {
    closedRef.current = true;
    clearHeartbeatTimer();
    clearTaskTimeout();
    if (wsRef.current) {
      wsRef.current.onclose = null;
      wsRef.current.close();
      wsRef.current = null;
    }
    setIsLoading(false);
    setConnectionStatus("closed");
  }, [clearHeartbeatTimer, clearTaskTimeout]);

  return { messages, isLoading, synthesis, error, connectionStatus, messagesEndRef, cancel };
}

export default useTalkPageStream;
