/**
 * ProgressFeed.tsx — Claude-Code-style live work indicator.
 *
 * Renders as a "ghost" assistant message inside the chat while Cato is
 * processing.  Consumes the structured state produced by useProgressStream
 * and displays:
 *
 *   - Turn N / max_turns header (slate, bold)
 *   - "Thinking…" or streaming token preview (slate, italic)
 *   - "Calling <tool>: <args_preview>" lines (slate, monospace)
 *   - "<symbol> success (0.12s)"   in green
 *   - "<symbol> error: <summary>"  in red
 *
 * The feed is capped at MAX_EVENTS rendered lines; older lines scroll off.
 * On session_end the parent (ChatView) tears the ghost down and the real
 * final answer renders normally as a regular assistant bubble.
 *
 * Styling intentionally uses inline styles keyed off the existing
 * --bg-card / --text-primary CSS variables so the feed picks up the
 * desktop app's dark theme without needing a new stylesheet.
 */

import React from "react";
import type { ProgressFeedState, TurnFeedItem, ToolFeedItem } from "../hooks/useProgressStream";

interface ProgressFeedProps {
  state: ProgressFeedState;
  /** Hide the ghost entirely (e.g. when the final answer has rendered). */
  hidden?: boolean;
  /** Hard cap on rendered lines so a runaway loop doesn't fill the DOM. */
  maxEvents?: number;
}

const MAX_EVENTS_DEFAULT = 200;

const COLOR_THINKING = "#94a3b8"; // slate-500
const COLOR_TOOL     = "#cbd5e1"; // slate-300
const COLOR_OK       = "#86efac"; // green-300 (matches activity-idle palette)
const COLOR_ERROR    = "#fca5a5"; // red-300
const COLOR_HEADER   = "#e2e8f0"; // text-primary
const COLOR_MUTED    = "#64748b"; // slate-500

const FONT_MONO =
  '"JetBrains Mono", "SF Mono", Consolas, ui-monospace, monospace';

function formatElapsed(s: number): string {
  if (!s || s < 0) return "0.00s";
  if (s < 1) return `${s.toFixed(2)}s`;
  if (s < 60) return `${s.toFixed(2)}s`;
  const m = Math.floor(s / 60);
  const rem = s - m * 60;
  return `${m}m ${rem.toFixed(1)}s`;
}

function truncate(s: string, max: number): string {
  if (!s) return "";
  if (s.length <= max) return s;
  return s.slice(0, max) + "…";
}

const ToolLine: React.FC<{ tool: ToolFeedItem }> = ({ tool }) => {
  const isRunning = tool.status === "running";
  const isError = tool.status === "error";
  const color = isError ? COLOR_ERROR : isRunning ? COLOR_TOOL : COLOR_OK;
  const headLabel = isRunning
    ? `> Calling ${tool.tool}: ${truncate(tool.argsPreview, 120)}`
    : isError
      ? `> error (${formatElapsed(tool.elapsedS)}): ${truncate(tool.summary, 140)}`
      : `> success (${formatElapsed(tool.elapsedS)})${tool.summary ? `: ${truncate(tool.summary, 140)}` : ""}`;
  return (
    <div
      style={{
        fontFamily: FONT_MONO,
        fontSize: 12,
        lineHeight: 1.55,
        color,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      {headLabel}
    </div>
  );
};

const TurnBlock: React.FC<{ turn: TurnFeedItem }> = ({ turn }) => {
  const showThinkingPlaceholder = turn.llmStarted && !turn.thinking;
  const showLlmDone = turn.llmEnded && turn.llmElapsedS > 0;
  return (
    <div style={{ marginBottom: 6 }}>
      <div
        style={{
          fontWeight: 700,
          fontSize: 11,
          color: COLOR_HEADER,
          textTransform: "uppercase",
          letterSpacing: "0.05em",
          marginBottom: 2,
        }}
      >
        Turn {turn.turn}
        {turn.maxTurns ? (
          <span style={{ color: COLOR_MUTED, fontWeight: 500 }}>
            {" "}
            / {turn.maxTurns}
          </span>
        ) : null}
        {turn.llmModel ? (
          <span style={{ color: COLOR_MUTED, marginLeft: 8, fontWeight: 500 }}>
            {turn.llmModel}
          </span>
        ) : null}
      </div>

      {showThinkingPlaceholder && (
        <div
          style={{
            fontStyle: "italic",
            color: COLOR_THINKING,
            fontSize: 12,
            lineHeight: 1.55,
          }}
        >
          {"> Thinking…"}
        </div>
      )}
      {turn.thinking && (
        <div
          style={{
            fontStyle: "italic",
            color: COLOR_THINKING,
            fontSize: 12,
            lineHeight: 1.55,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {"> " + turn.thinking}
        </div>
      )}
      {showLlmDone && !turn.thinking && (
        <div
          style={{
            fontStyle: "italic",
            color: COLOR_MUTED,
            fontSize: 11,
            lineHeight: 1.55,
          }}
        >
          {`> Thought for ${formatElapsed(turn.llmElapsedS)}`}
        </div>
      )}

      {turn.tools.map((tool) => (
        <ToolLine key={tool.callId} tool={tool} />
      ))}
    </div>
  );
};

export const ProgressFeed: React.FC<ProgressFeedProps> = ({
  state,
  hidden,
  maxEvents = MAX_EVENTS_DEFAULT,
}) => {
  if (hidden) return null;
  if (!state.active && state.turns.length === 0) return null;

  // Count the rendered "lines" so we can stop adding turns once the cap
  // is hit.  Each turn contributes: 1 header + (1 thinking) + N tools.
  let lineBudget = maxEvents;
  const visibleTurns: TurnFeedItem[] = [];
  // Walk turns from oldest → newest so the newest rows are always visible.
  for (let i = state.turns.length - 1; i >= 0; i--) {
    const t = state.turns[i];
    const cost = 1 + (t.thinking || t.llmStarted ? 1 : 0) + t.tools.length;
    if (lineBudget <= 0) break;
    visibleTurns.unshift(t);
    lineBudget -= cost;
  }

  const currentTurn = state.turns[state.turns.length - 1];
  const summaryParts: string[] = [];
  if (state.active && currentTurn) {
    summaryParts.push(`turn ${currentTurn.turn} / ${currentTurn.maxTurns}`);
    if (currentTurn.tools.some((t) => t.status === "running")) {
      const running = currentTurn.tools.find((t) => t.status === "running");
      if (running) summaryParts.push(`running ${running.tool}`);
    } else if (currentTurn.llmStarted && !currentTurn.llmEnded) {
      summaryParts.push("waiting on model");
    }
  }

  return (
    <div
      className="chat-bubble chat-bubble-assistant progress-feed-ghost"
      data-progress-feed="1"
      style={{
        background: "var(--bg-card, #1e2433)",
        border: "1px dashed var(--border-color, #1e2433)",
        opacity: 0.95,
      }}
    >
      <div className="chat-bubble-header">
        <span className="chat-bubble-role">
          Cato{" "}
          <span
            style={{
              fontSize: 10,
              fontWeight: 600,
              padding: "1px 6px",
              borderRadius: 8,
              background: "#fbbf2422",
              color: "#fbbf24",
              border: "1px solid #fbbf2455",
              marginLeft: 6,
              textTransform: "none",
              letterSpacing: 0,
            }}
          >
            working
          </span>
        </span>
        {summaryParts.length > 0 && (
          <span
            style={{
              fontSize: 11,
              color: COLOR_MUTED,
              marginLeft: "auto",
            }}
          >
            {summaryParts.join(" — ")}
          </span>
        )}
      </div>

      <div
        style={{
          marginTop: 4,
          padding: "6px 4px 2px",
          fontSize: 12,
          lineHeight: 1.55,
        }}
      >
        {visibleTurns.map((t) => (
          <TurnBlock key={t.turn} turn={t} />
        ))}
        {visibleTurns.length === 0 && state.active && (
          <div
            style={{
              fontStyle: "italic",
              color: COLOR_THINKING,
              fontSize: 12,
            }}
          >
            {"> Starting…"}
          </div>
        )}
      </div>
    </div>
  );
};

export default ProgressFeed;
