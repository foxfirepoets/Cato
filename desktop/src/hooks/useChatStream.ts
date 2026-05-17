/**
 * useChatStream.ts — WebSocket hook for the general chat view.
 *
 * - Connects to the daemon's aiohttp WebSocket surface (ws://127.0.0.1:8080/ws) for web chat
 * - Persists messages to localStorage so they survive view navigation
 * - Polls /api/chat/history every 5 s to surface Telegram messages in the UI
 */

import { useState, useEffect, useRef, useCallback } from "react";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  timestamp: number;
  source?: "web" | "telegram" | "cron" | string;
  model?: string;  // AI model used (claude, codex, gemini, cursor, swarmsync, etc.)
}

export type ChatConnectionStatus = "connecting" | "connected" | "disconnected" | "reconnecting";

export interface UseChatStreamResult {
  messages: ChatMessage[];
  connectionStatus: ChatConnectionStatus;
  sendMessage: (text: string) => void;
  isStreaming: boolean;
  clearHistory: () => void;
  /** Ref to the live WebSocket — pass to ActivityIndicator for instant activity events */
  wsRef: React.RefObject<WebSocket | null>;
}

const INITIAL_BACKOFF_MS = 500;
const MAX_BACKOFF_MS     = 30_000;
const HISTORY_POLL_MS   = 5_000;
const STORAGE_KEY       = "cato-chat-messages";
const MAX_STORED        = 500;

function loadStored(): ChatMessage[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ChatMessage[]) : [];
  } catch {
    return [];
  }
}

function saveStored(msgs: ChatMessage[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(msgs.slice(-MAX_STORED)));
  } catch {
    // quota exceeded — silently ignore
  }
}

export function useChatStream(wsBase?: string, httpPort?: number, daemonToken?: string): UseChatStreamResult {
  const [messages, setMessages] = useState<ChatMessage[]>(loadStored);
  const [connectionStatus, setConnectionStatus] = useState<ChatConnectionStatus>("connecting");
  const [isStreaming, setIsStreaming] = useState(false);

  const wsRef       = useRef<WebSocket | null>(null);
  const retriesRef  = useRef(0);
  const mountedRef  = useRef(true);
  const sessionIdRef = useRef(crypto.randomUUID());
  // Track IDs already in state so we don't double-add from history poll
  const knownIdsRef = useRef<Set<string>>(new Set(loadStored().map((m) => m.id)));
  // Latest sinceTs for incremental polling
  const sinceRef    = useRef<number>(0);

  // Persist whenever messages change
  useEffect(() => {
    saveStored(messages);
    messages.forEach((m) => knownIdsRef.current.add(m.id));
    if (messages.length > 0) {
      sinceRef.current = Math.max(...messages.map((m) => m.timestamp));
    }
  }, [messages]);

  // Content-based dedup key: role + first 200 chars + timestamp within 30s window.
  // The wider window prevents the same WS response and a history-poll echo from
  // getting different bucket keys when they arrive near a 5-second boundary.
  const contentKeysRef = useRef<Set<string>>(new Set());

  const makeContentKey = (m: ChatMessage): string => {
    const ts = Math.floor(m.timestamp / 30000); // 30s window
    return `${m.role}:${m.text.slice(0, 200)}:${ts}`;
  };

  const addMessages = useCallback((incoming: ChatMessage[]) => {
    const novel = incoming.filter((m) => {
      if (knownIdsRef.current.has(m.id)) return false;
      const ck = makeContentKey(m);
      if (contentKeysRef.current.has(ck)) return false;
      return true;
    });
    if (novel.length === 0) return;
    novel.forEach((m) => {
      knownIdsRef.current.add(m.id);
      contentKeysRef.current.add(makeContentKey(m));
    });
    setMessages((prev) => [...prev, ...novel].sort((a, b) => a.timestamp - b.timestamp));
  }, []);

  // Poll /api/chat/history to pull in Telegram messages
  useEffect(() => {
    const apiBase = httpPort ? `http://127.0.0.1:${httpPort}` : "http://127.0.0.1:8080";
    const poll = async () => {
      try {
        const token = daemonToken || (window as Window & { __CATO_DAEMON_TOKEN__?: string }).__CATO_DAEMON_TOKEN__;
        const headers = token ? { "X-Cato-Token": token } : undefined;
        const res = await fetch(`${apiBase}/api/chat/history?since=${sinceRef.current}`, { headers });
        if (!res.ok) return;
        const entries = await res.json() as Array<{
          id: string; role: string; text: string; channel: string;
          session_id: string; timestamp: number;
        }>;
        // Skip "web" channel entries — those messages already arrive through the
        // WebSocket connection and would otherwise appear twice.
        const mapped: ChatMessage[] = entries
          .filter((e) => e.channel !== "web")
          .map((e) => ({
            id:        e.id,
            role:      e.role === "user" ? "user" : "assistant",
            text:      e.text,
            timestamp: e.timestamp,
            source:    e.channel,
          }));
        addMessages(mapped);
      } catch {
        // daemon not running — silently skip
      }
    };
    const timer = setInterval(poll, HISTORY_POLL_MS);
    poll(); // immediate first fetch
    return () => clearInterval(timer);
  }, [httpPort, daemonToken, addMessages]);

  const connect = useCallback(() => {
    const rawHost = wsBase ?? "127.0.0.1:8080";
    const host = /^127\.0\.0\.1:\d+$/.test(rawHost) ? rawHost : "127.0.0.1:8080";
    const token = daemonToken || (window as Window & { __CATO_DAEMON_TOKEN__?: string }).__CATO_DAEMON_TOKEN__;
    const qs = token ? `?token=${encodeURIComponent(token)}` : "";
    const url = `ws://${host}/ws${qs}`;

    setConnectionStatus("connecting");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      setConnectionStatus("connected");
      retriesRef.current = 0;
    };

    ws.onmessage = (ev: MessageEvent<string>) => {
      try {
        const data = JSON.parse(ev.data.trimEnd());

        if (data.type === "health" || data.type === "heartbeat") return;

        // Handle incoming user messages (from Telegram/WhatsApp)
        if (data.type === "message" && data.role === "user") {
          const msg: ChatMessage = {
            id:        crypto.randomUUID(),
            role:      "user",
            text:      data.text ?? "",
            timestamp: Date.now(),
            source:    data.channel ?? "web",
          };
          addMessages([msg]);
          return;
        }

        // Handle assistant responses
        if (data.type === "response" || data.text || data.reply) {
          const rawText = data.text ?? data.reply ?? data.message ?? "";
          const text = rawText.trim()
            ? rawText
            : "I didn't get a response from the model. Please try again.";
          const msg: ChatMessage = {
            id:        crypto.randomUUID(),
            role:      "assistant",
            text,
            timestamp: Date.now(),
            source:    data.channel ?? "web",
            model:     data.model,  // Include model attribution
          };
          addMessages([msg]);
          setIsStreaming(false);
        }
      } catch {
        if (ev.data.trim()) {
          addMessages([{
            id:        crypto.randomUUID(),
            role:      "assistant",
            text:      ev.data.trim(),
            timestamp: Date.now(),
            source:    "web",
          }]);
          setIsStreaming(false);
        }
      }
    };

    ws.onerror = () => {
      console.error("[useChatStream] WebSocket error");
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      retriesRef.current += 1;
      const backoff = Math.min(INITIAL_BACKOFF_MS * Math.pow(2, retriesRef.current - 1), MAX_BACKOFF_MS);
      setConnectionStatus("reconnecting");
      setTimeout(connect, backoff);
    };
  }, [wsBase, daemonToken, addMessages]);

  useEffect(() => {
    connect();
    return () => {
      mountedRef.current = false;
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const sendMessage = useCallback((text: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

    const userMsg: ChatMessage = {
      id:        crypto.randomUUID(),
      role:      "user",
      text,
      timestamp: Date.now(),
      source:    "web",
    };
    addMessages([userMsg]);
    setIsStreaming(true);

    wsRef.current.send(
      JSON.stringify({
        type:       "message",
        text,
        session_id: sessionIdRef.current,
      }),
    );
  }, [addMessages]);

  const clearHistory = useCallback(() => {
    setMessages([]);
    knownIdsRef.current.clear();
    sinceRef.current = 0;
    localStorage.removeItem(STORAGE_KEY);
  }, []);

  return { messages, connectionStatus, sendMessage, isStreaming, clearHistory, wsRef };
}

export default useChatStream;
