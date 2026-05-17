/**
 * TerminalPane.tsx — xterm.js terminal connected to a PTY WebSocket.
 * Renders terminal output and sends user input and resize events to the daemon.
 */

import "@xterm/xterm/css/xterm.css";
import React, { useEffect, useRef, useCallback } from "react";

interface TerminalPaneProps {
  sessionId: string | null;
  wsBase: string;
  className?: string;
}

export const TerminalPane: React.FC<TerminalPaneProps> = ({
  sessionId,
  wsBase,
  className = "",
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<{ terminal: import("@xterm/xterm").Terminal; fitAddon: import("@xterm/addon-fit").FitAddon } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);

  const connect = useCallback(() => {
    if (!sessionId || !containerRef.current) return;

    import("@xterm/xterm").then((xterm) => {
      import("@xterm/addon-fit").then(({ FitAddon }) => {
        if (!containerRef.current || termRef.current) return;

        const term = new xterm.Terminal({
          cursorBlink: true,
          theme: { background: "#1e1e1e", foreground: "#d4d4d4" },
          fontSize: 14,
          fontFamily: "Consolas, 'Courier New', monospace",
        });
        const fitAddon = new FitAddon();
        term.loadAddon(fitAddon);
        term.open(containerRef.current);
        fitAddon.fit();
        termRef.current = { terminal: term, fitAddon };

        const protocol = wsBase.startsWith("https") ? "wss" : "ws";
        const host = wsBase.replace(/^https?:\/\//, "").replace(/^\/+/, "");
        const token = (window as Window & { __CATO_DAEMON_TOKEN__?: string }).__CATO_DAEMON_TOKEN__;
        const qs = token ? `?token=${encodeURIComponent(token)}` : "";
        const wsUrl = `${protocol}://${host}/ws/pty/${sessionId}${qs}`;
        const ws = new WebSocket(wsUrl);
        wsRef.current = ws;

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.type === "output" && msg.data != null) {
              term.write(msg.data);
            }
          } catch {
            // ignore
          }
        };

        term.onData((data) => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "input", data }));
          }
        });

        const resizeObserver = new ResizeObserver(() => {
          fitAddon.fit();
          const { cols, rows } = term;
          if (ws.readyState === WebSocket.OPEN && cols && rows) {
            ws.send(JSON.stringify({ type: "resize", cols, rows }));
          }
        });
        resizeObserver.observe(containerRef.current);
        resizeObserverRef.current = resizeObserver;

        ws.onopen = () => {
          fitAddon.fit();
          const { cols, rows } = term;
          if (cols && rows) {
            ws.send(JSON.stringify({ type: "resize", cols, rows }));
          }
        };
      });
    });
  }, [sessionId, wsBase]);

  useEffect(() => {
    if (!sessionId) {
      if (termRef.current) {
        termRef.current.terminal.dispose();
        termRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      return;
    }
    connect();
    return () => {
      if (resizeObserverRef.current && containerRef.current) {
        resizeObserverRef.current.disconnect();
        resizeObserverRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (termRef.current) {
        termRef.current.terminal.dispose();
        termRef.current = null;
      }
    };
  }, [sessionId, connect]);

  return (
    <div
      ref={containerRef}
      className={`terminal-pane ${className}`}
      style={{ width: "100%", height: "100%", minHeight: 300 }}
      aria-label="Terminal output"
    />
  );
};

export default TerminalPane;
