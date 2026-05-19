/**
 * ChatView.tsx — Chat interface with file upload. Persists history, shows Telegram messages.
 */

import React, { useState, useRef, useEffect, useCallback, type FormEvent } from "react";
import { useChatStream, type ChatMessage, type ChatConnectionStatus } from "../hooks/useChatStream";
import { useProgressStream } from "../hooks/useProgressStream";
import { ActivityIndicator } from "../components/ActivityIndicator";
import { ProgressFeed } from "../components/ProgressFeed";
import logoSrc from "../assets/cato-logo.png";

interface ChatViewProps {
  wsBase?: string;
  httpPort?: number;
  daemonToken?: string;
  onConnectionStatusChange?: (status: ChatConnectionStatus) => void;
}

interface BadgeProps {
  source?: string;
  model?: string;
}

interface UploadedFile {
  filename: string;
  path: string;
  size: number;
  type: string;
}

const DEFAULT_MODELS = new Set([
  "openrouter/minimax/minimax-m2.5",
  "openrouter/minimax/minimax-2.5",
  "minimax/minimax-m2.5",
  "minimax/minimax-2.5",
  "abab7-chat-preview",
]);

function normalizeModelLabel(model: string): string {
  const raw = model.trim();
  if (!raw) return "";
  if (DEFAULT_MODELS.has(raw.toLowerCase())) return "";
  const upper = raw.toUpperCase();
  if (upper.includes("CLAUDE")) return "CLAUDE";
  if (upper.includes("CODEX")) return "CODEX";
  if (upper.includes("GEMINI")) return "GEMINI";
  if (upper.includes("CURSOR")) return "CURSOR";
  if (upper.includes("SWARMSYNC")) return "SWARMSYNC";
  if (upper.includes("MINIMAX")) return "MINIMAX";
  if (upper.includes("GPT")) return "GPT";
  const parts = upper.split("/");
  if (parts.length > 1) return parts[parts.length - 1];
  return upper;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const FILE_TYPE_ICONS: Record<string, string> = {
  image: "\u{1F5BC}",      // framed picture
  pdf: "\u{1F4C4}",        // page facing up
  spreadsheet: "\u{1F4CA}", // bar chart
  code: "\u{1F4BB}",       // laptop
  document: "\u{1F4C1}",   // file folder
};

const SourceBadge: React.FC<BadgeProps> = ({ source, model }) => {
  const badges = [];
  if (source && source !== "web") {
    const label = source === "telegram" ? "Telegram" : source;
    const color = source === "telegram" ? "#229ED9" : "#94a3b8";
    badges.push(
      <span key={`source-${source}`} style={{
        fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 8,
        background: `${color}22`, color, border: `1px solid ${color}55`,
        marginLeft: 6, lineHeight: 1.4,
      }}>
        {label}
      </span>
    );
  }
  if (model) {
    const modelLabel = normalizeModelLabel(model);
    if (modelLabel) {
      const modelColors: Record<string, string> = {
        "CLAUDE": "#9B5DE5", "CODEX": "#00D9FF", "GEMINI": "#F77F00",
        "CURSOR": "#06FFA5", "SWARMSYNC": "#FF006E",
      };
      const modelColor = Object.entries(modelColors).find(([key]) => modelLabel.includes(key))?.[1] || "#64748B";
      badges.push(
        <span key={`model-${model}`} style={{
          fontSize: 10, fontWeight: 700, padding: "1px 6px", borderRadius: 8,
          background: `${modelColor}22`, color: modelColor, border: `1px solid ${modelColor}55`,
          marginLeft: 6, lineHeight: 1.4,
        }}>
          {modelLabel}
        </span>
      );
    }
  }
  return badges.length > 0 ? <>{badges}</> : null;
};

const ChatBubble: React.FC<{ message: ChatMessage }> = ({ message }) => {
  const isUser = message.role === "user";
  return (
    <div className={`chat-bubble ${isUser ? "chat-bubble-user" : "chat-bubble-assistant"}`}>
      <div className="chat-bubble-header">
        <span className="chat-bubble-role">
          {isUser ? "You" : "Cato"}
          <SourceBadge source={message.source} model={message.model} />
        </span>
        <time className="chat-bubble-time">
          {new Date(message.timestamp).toLocaleTimeString([], {
            hour: "2-digit", minute: "2-digit",
          })}
        </time>
      </div>
      <div className="chat-bubble-text">{message.text}</div>
    </div>
  );
};

/* ── File attachment chip ── */
const FileChip: React.FC<{ file: UploadedFile; onRemove: () => void }> = ({ file, onRemove }) => (
  <div style={{
    display: "inline-flex", alignItems: "center", gap: 6,
    padding: "4px 10px", borderRadius: 8, fontSize: 12,
    background: "var(--bg-tertiary, #1e1e2e)",
    border: "1px solid var(--border-secondary, #2a2a3e)",
    color: "var(--text-primary, #e2e8f0)",
  }}>
    <span>{FILE_TYPE_ICONS[file.type] || "\u{1F4CE}"}</span>
    <span style={{ maxWidth: 150, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
      {file.filename}
    </span>
    <span style={{ color: "var(--text-muted, #64748b)", fontSize: 11 }}>
      {formatFileSize(file.size)}
    </span>
    <button
      onClick={onRemove}
      style={{
        background: "none", border: "none", cursor: "pointer",
        color: "var(--text-muted, #64748b)", fontSize: 14,
        padding: "0 2px", lineHeight: 1,
      }}
      title="Remove attachment"
    >
      ×
    </button>
  </div>
);

export const ChatView: React.FC<ChatViewProps> = ({ wsBase, httpPort, daemonToken, onConnectionStatusChange }) => {
  const { messages, connectionStatus, sendMessage, isStreaming, clearHistory, wsRef } =
    useChatStream(wsBase, httpPort, daemonToken);
  // Claude-Code-style live work feed — listens for the gateway's `progress`
  // WS events and exposes a structured per-turn view.  Renders as a ghost
  // assistant bubble while a session is active; cleared on session_end.
  const { state: progressState, reset: resetProgress } = useProgressStream(wsRef);
  const [input, setInput] = useState("");
  const [attachedFiles, setAttachedFiles] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const apiBase = `http://127.0.0.1:${httpPort ?? 8080}`;

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (onConnectionStatusChange) onConnectionStatusChange(connectionStatus);
  }, [connectionStatus, onConnectionStatusChange]);

  const uploadFile = useCallback(async (file: File) => {
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const token = daemonToken || (window as Window & { __CATO_DAEMON_TOKEN__?: string }).__CATO_DAEMON_TOKEN__;
      const headers: Record<string, string> = {};
      if (token) headers["X-Cato-Token"] = token;
      const res = await fetch(`${apiBase}/api/chat/upload`, {
        method: "POST",
        headers,
        body: form,
      });
      const data = await res.json();
      if (data.status === "ok") {
        setAttachedFiles((prev) => [...prev, {
          filename: data.filename,
          path: data.path,
          size: data.size,
          type: data.type,
        }]);
      } else {
        alert(`Upload failed: ${data.error || "unknown error"}`);
      }
    } catch (e) {
      alert(`Upload failed: ${e}`);
    } finally {
      setUploading(false);
    }
  }, [apiBase, daemonToken]);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    for (let i = 0; i < files.length; i++) {
      uploadFile(files[i]);
    }
    // Reset so the same file can be re-selected
    e.target.value = "";
  }, [uploadFile]);

  const handleSubmit = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      const text = input.trim();
      if (!text && attachedFiles.length === 0) return;

      // Build message with file references
      let fullText = text;
      if (attachedFiles.length > 0) {
        const fileRefs = attachedFiles.map(
          (f) => `[Attached file: ${f.filename} (${f.type}, ${formatFileSize(f.size)}) at ${f.path}]`
        ).join("\n");
        fullText = fileRefs + (text ? "\n\n" + text : "\n\nPlease review the attached file(s).");
      }

      // Reset the live work feed so the new turn starts from an empty
      // ghost bubble; the gateway will repopulate it via WS `progress` events.
      resetProgress();
      sendMessage(fullText);
      setInput("");
      setAttachedFiles([]);
      inputRef.current?.focus();
    },
    [input, attachedFiles, sendMessage, resetProgress],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSubmit(e as unknown as FormEvent);
      }
    },
    [handleSubmit],
  );

  // Drag and drop support
  const [dragOver, setDragOver] = useState(false);
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const files = e.dataTransfer.files;
    for (let i = 0; i < files.length; i++) {
      uploadFile(files[i]);
    }
  }, [uploadFile]);

  return (
    <div
      className="chat-view"
      onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
    >
      {/* Drag overlay */}
      {dragOver && (
        <div style={{
          position: "absolute", inset: 0, zIndex: 50,
          background: "rgba(99, 102, 241, 0.08)",
          border: "2px dashed var(--accent-primary, #6366f1)",
          borderRadius: 12, display: "flex", alignItems: "center", justifyContent: "center",
          pointerEvents: "none",
        }}>
          <span style={{ fontSize: 16, color: "var(--accent-primary, #6366f1)", fontWeight: 600 }}>
            Drop files here to attach
          </span>
        </div>
      )}

      <header className="chat-header">
        <h1 className="chat-title">Cato Chat</h1>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <ActivityIndicator httpPort={httpPort ?? 8080} wsRef={wsRef} />
          <span className={`chat-status chat-status-${connectionStatus}`}>
            {connectionStatus === "connected"    ? "Connected"      :
             connectionStatus === "connecting"   ? "Connecting..."  :
             connectionStatus === "reconnecting" ? "Reconnecting..."
                                                 : "Disconnected"}
          </span>
          {messages.length > 0 && (
            <button
              className="btn-cancel-sm"
              onClick={clearHistory}
              title="Clear conversation history"
              style={{ fontSize: 11 }}
            >
              Clear
            </button>
          )}
        </div>
      </header>

      <div className="chat-messages" role="log" aria-live="polite" aria-label="Chat messages">
        {messages.length === 0 && (
          <div className="chat-empty">
            <img src={logoSrc} alt="Cato" className="chat-empty-logo" />
            <p>Start a conversation with Cato</p>
            <p className="chat-empty-hint">
              Ask questions, get help with code, or explore ideas.
              Drag &amp; drop files or use the + button to attach documents.
            </p>
          </div>
        )}
        {messages.map((msg) => (
          <ChatBubble key={msg.id} message={msg} />
        ))}
        {/*
          Claude-Code-style live feed.  Visible while a session is active
          (progressState.active), or while local isStreaming flag is set but
          no progress events have arrived yet (e.g. very first turn before
          the agent loop emits turn_start).  Falls back to the existing
          three-dot typing indicator in the latter case so the user still
          sees *something* immediately.
        */}
        {progressState.active || progressState.turns.length > 0 ? (
          <ProgressFeed state={progressState} hidden={false} />
        ) : (
          isStreaming && (
            <div className="chat-bubble chat-bubble-assistant">
              <div className="chat-bubble-header">
                <span className="chat-bubble-role">Cato</span>
              </div>
              <div className="chat-typing">
                <span /><span /><span />
              </div>
            </div>
          )
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Attached files bar */}
      {attachedFiles.length > 0 && (
        <div style={{
          display: "flex", gap: 6, flexWrap: "wrap",
          padding: "6px 16px", borderTop: "1px solid var(--border-secondary, #2a2a3e)",
          background: "var(--bg-secondary, #13131f)",
        }}>
          {attachedFiles.map((f, i) => (
            <FileChip
              key={`${f.filename}-${i}`}
              file={f}
              onRemove={() => setAttachedFiles((prev) => prev.filter((_, j) => j !== i))}
            />
          ))}
        </div>
      )}

      <form className="chat-input-form" onSubmit={handleSubmit}>
        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={handleFileSelect}
          accept=".txt,.md,.py,.js,.ts,.json,.yaml,.yml,.toml,.html,.css,.csv,.tsv,.xlsx,.xls,.pdf,.png,.jpg,.jpeg,.gif,.webp,.svg,.log,.xml,.sql,.sh,.ps1,.bat,.docx,.pptx"
        />
        {/* Upload button */}
        <button
          type="button"
          className="chat-upload-btn"
          onClick={() => fileInputRef.current?.click()}
          disabled={uploading || connectionStatus !== "connected"}
          title="Attach a file"
          style={{
            display: "flex", alignItems: "center", justifyContent: "center",
            width: 36, height: 36, minWidth: 36,
            borderRadius: 8, border: "1px solid var(--border-secondary, #2a2a3e)",
            background: uploading ? "var(--accent-primary, #6366f1)" : "var(--bg-tertiary, #1e1e2e)",
            color: uploading ? "#fff" : "var(--text-muted, #94a3b8)",
            cursor: uploading ? "wait" : "pointer",
            fontSize: 18, fontWeight: 700,
            transition: "all 0.15s ease",
          }}
        >
          {uploading ? (
            <span style={{ display: "inline-block", width: 14, height: 14, border: "2px solid #fff", borderTopColor: "transparent", borderRadius: "50%", animation: "spin 0.8s linear infinite" }} />
          ) : (
            "+"
          )}
        </button>
        <textarea
          ref={inputRef}
          className="chat-input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={attachedFiles.length > 0
            ? "Add a message about your file(s)... (Enter to send)"
            : "Type a message... (Enter to send, Shift+Enter for newline)"}
          aria-label="Type a message"
          rows={2}
          disabled={connectionStatus !== "connected"}
          autoFocus
        />
        <button
          type="submit"
          className="chat-send-btn"
          disabled={(!input.trim() && attachedFiles.length === 0) || connectionStatus !== "connected" || isStreaming}
        >
          {isStreaming ? "Working..." : "Send"}
        </button>
      </form>
    </div>
  );
};

export default ChatView;
