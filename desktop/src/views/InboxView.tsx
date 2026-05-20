/**
 * InboxView — Gmail draft approvals and Ben Assistant personal-store notes.
 */
import React, { useCallback, useEffect, useState } from "react";

interface InboxViewProps {
  httpPort: number;
}

interface EmailDraft {
  id: number;
  gmail_message_id: string;
  subject?: string | null;
  from_email?: string | null;
  snippet?: string | null;
  draft_reply?: string | null;
  gmail_draft_id?: string | null;
  status: string;
  created_at?: string | null;
}

interface PersonalNote {
  id: number;
  content: string;
  category: "todo" | "memory" | "idea" | "reminder";
  due_date?: string | null;
  status: string;
  created_at?: string | null;
}

interface InboxData {
  email_drafts: EmailDraft[];
  notes: PersonalNote[];
  todos: PersonalNote[];
  reminders: PersonalNote[];
  counts: {
    email_drafts: number;
    notes: number;
    todos: number;
    reminders: number;
  };
}

type EmailAction = "approve" | "dismiss";

function formatTime(value?: string | null): string {
  if (!value) return "—";
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function preview(text?: string | null, max = 220): string {
  const value = (text ?? "").trim();
  if (!value) return "—";
  return value.length > max ? `${value.slice(0, max)}...` : value;
}

export const InboxView: React.FC<InboxViewProps> = ({ httpPort }) => {
  const base = `http://127.0.0.1:${httpPort}`;
  const [data, setData] = useState<InboxData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyEmailId, setBusyEmailId] = useState<number | null>(null);

  const fetchInbox = useCallback(async () => {
    try {
      const r = await fetch(`${base}/api/inbox`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setData(await r.json());
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [base]);

  useEffect(() => {
    fetchInbox();
  }, [fetchInbox]);

  const runEmailAction = async (id: number, action: EmailAction) => {
    setBusyEmailId(id);
    setNotice(null);
    try {
      const r = await fetch(`${base}/api/inbox/email/${id}/${action}`, { method: "POST" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok && r.status !== 409) {
        throw new Error(body.error ?? `HTTP ${r.status}`);
      }
      setNotice(body.message ?? (action === "approve" ? "Draft approved." : "Draft dismissed."));
      await fetchInbox();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyEmailId(null);
    }
  };

  if (loading) return <div className="view-loading"><div className="app-loading-spinner" /></div>;

  const emailDrafts = data?.email_drafts ?? [];
  const notes = data?.notes ?? [];
  const todos = data?.todos ?? [];
  const reminders = data?.reminders ?? [];

  return (
    <div className="page-view inbox-view">
      <div className="page-header">
        <h1 className="page-title">Inbox</h1>
        <div className="page-controls">
          <button className="btn-secondary" onClick={fetchInbox}>Refresh</button>
        </div>
      </div>

      {error && <div className="page-error">{error}</div>}
      {notice && <div className="save-banner save-banner-ok">{notice}</div>}

      <div className="dash-grid">
        <div className="dash-card">
          <div className="dash-card-label">Email Drafts</div>
          <div className="dash-card-value">{data?.counts.email_drafts ?? 0}</div>
          <div className="dash-card-sub">waiting for approval</div>
        </div>
        <div className="dash-card">
          <div className="dash-card-label">Open Todos</div>
          <div className="dash-card-value">{data?.counts.todos ?? 0}</div>
          <div className="dash-card-sub">from personal store</div>
        </div>
        <div className="dash-card">
          <div className="dash-card-label">Reminders</div>
          <div className="dash-card-value">{data?.counts.reminders ?? 0}</div>
          <div className="dash-card-sub">open reminders</div>
        </div>
        <div className="dash-card">
          <div className="dash-card-label">Recent Notes</div>
          <div className="dash-card-value">{data?.counts.notes ?? 0}</div>
          <div className="dash-card-sub">latest captured notes</div>
        </div>
      </div>

      <div className="section-block">
        <div className="section-title">Pending Gmail Draft Replies</div>
        {emailDrafts.length === 0 ? (
          <div className="empty-state">No pending Gmail drafts</div>
        ) : (
          <div className="inbox-email-list">
            {emailDrafts.map((email) => (
              <article className="inbox-email-card" key={email.id}>
                <div className="inbox-email-header">
                  <div className="inbox-email-title">
                    <span>{email.subject || "(no subject)"}</span>
                    <span className="action-badge">{email.status}</span>
                  </div>
                  <div className="inbox-email-meta">
                    <span>{email.from_email || "Unknown sender"}</span>
                    <span>{formatTime(email.created_at)}</span>
                  </div>
                </div>
                <div className="inbox-email-snippet">{preview(email.snippet, 260)}</div>
                <div className="inbox-draft-reply">{preview(email.draft_reply, 900)}</div>
                <div className="inbox-email-actions">
                  <span className="code-cell">
                    {email.gmail_draft_id ? `draft ${email.gmail_draft_id}` : "no Gmail draft id yet"}
                  </span>
                  <button
                    className="btn-primary btn-sm"
                    onClick={() => runEmailAction(email.id, "approve")}
                    disabled={busyEmailId === email.id}
                  >
                    {busyEmailId === email.id ? "Working..." : "Approve"}
                  </button>
                  <button
                    className="btn-danger-sm"
                    onClick={() => runEmailAction(email.id, "dismiss")}
                    disabled={busyEmailId === email.id}
                  >
                    Dismiss
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      <div className="inbox-columns">
        <div className="section-block">
          <div className="section-title">Open Todos</div>
          {todos.length === 0 ? (
            <div className="empty-state">No open todos</div>
          ) : (
            <div className="inbox-note-list">
              {todos.map((note) => (
                <div className="inbox-note-row" key={note.id}>
                  <span className="inbox-note-content">{note.content}</span>
                  <span className="ts-cell">{formatTime(note.due_date || note.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="section-block">
          <div className="section-title">Reminders</div>
          {reminders.length === 0 ? (
            <div className="empty-state">No open reminders</div>
          ) : (
            <div className="inbox-note-list">
              {reminders.map((note) => (
                <div className="inbox-note-row" key={note.id}>
                  <span className="inbox-note-content">{note.content}</span>
                  <span className="ts-cell">{formatTime(note.due_date || note.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="section-block">
        <div className="section-title">Recent Notes</div>
        {notes.length === 0 ? (
          <div className="empty-state">No recent notes</div>
        ) : (
          <div className="table-container">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Category</th>
                  <th>Note</th>
                  <th>Due</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {notes.map((note) => (
                  <tr key={note.id}>
                    <td><span className="action-badge">{note.category}</span></td>
                    <td>{note.content}</td>
                    <td className="ts-cell">{note.due_date || "—"}</td>
                    <td className="ts-cell">{formatTime(note.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};

export default InboxView;
