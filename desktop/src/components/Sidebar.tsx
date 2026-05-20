/**
 * Sidebar.tsx — Left navigation sidebar for Cato Desktop.
 *
 * Four nav groups: Workspace / Automation / Monitoring / Settings
 * Renders brand header, grouped nav items, and daemon status at bottom.
 */
import React from "react";
import logoSrc from "../assets/cato-logo.png";

export type View =
  | "dashboard"
  | "chat"
  | "inbox"
  | "coding-agent"
  | "interactive-cli"
  | "skills"
  | "cron"
  | "sessions"
  | "usage"
  | "logs"
  | "audit"
  | "memory"
  | "settings"
  | "config"
  | "budget"
  | "alerts"
  | "auth-keys"
  | "identity"
  | "flows"
  | "nodes"
  | "system"
  | "diagnostics";

interface NavItem {
  id: View;
  label: string;
  icon: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    label: "Workspace",
    items: [
      { id: "dashboard",       label: "Dashboard",       icon: "⊞" },
      { id: "chat",           label: "Chat",           icon: "💬" },
      { id: "inbox",          label: "Inbox",          icon: "✉" },
      { id: "coding-agent",   label: "Coding Agent",   icon: "⌨" },
      { id: "interactive-cli", label: "Interactive CLIs", icon: "🖥" },
    ],
  },
  {
    label: "Automation",
    items: [
      { id: "skills", label: "Skills",    icon: "🧩" },
      { id: "cron",   label: "Cron Jobs", icon: "⏱" },
      { id: "flows",  label: "Flows",     icon: "⚡" },
    ],
  },
  {
    label: "Monitoring",
    items: [
      { id: "sessions", label: "Sessions",     icon: "👥" },
      { id: "nodes",    label: "Remote Nodes", icon: "🖥" },
      { id: "memory",   label: "Memory",       icon: "🧠" },
      { id: "usage",    label: "Usage",        icon: "📊" },
      { id: "logs",        label: "Logs",        icon: "📄" },
      { id: "audit",       label: "Audit Log",   icon: "🛡" },
      { id: "diagnostics", label: "Diagnostics", icon: "🔬" },
    ],
  },
  {
    label: "Settings",
    items: [
      { id: "system",    label: "System",     icon: "⚙️" },
      { id: "identity",  label: "Identity",   icon: "🪪" },
      { id: "settings",  label: "Settings",   icon: "☑" },
      { id: "config",    label: "Config",     icon: "⚙" },
      { id: "budget",    label: "Budget",     icon: "$" },
      { id: "alerts",    label: "Alerts",     icon: "🔔" },
      { id: "auth-keys", label: "Auth & Keys", icon: "🔑" },
    ],
  },
];

type DaemonStatus = "starting" | "ready" | "stopped" | "error";

interface SidebarProps {
  activeView: View;
  onNavigate: (view: View) => void;
  daemonStatus: DaemonStatus;
}

export const Sidebar: React.FC<SidebarProps> = ({ activeView, onNavigate, daemonStatus }) => {
  const statusLabel =
    daemonStatus === "ready"    ? "Connected" :
    daemonStatus === "starting" ? "Starting..." :
    daemonStatus === "error"    ? "Error" : "Stopped";

  return (
    <aside className="sidebar">
      {/* Brand header */}
      <div className="sidebar-brand">
        <img src={logoSrc} alt="Cato" className="sidebar-logo" />
        <span className="sidebar-brand-name">Cato</span>
      </div>

      {/* Nav groups */}
      <nav className="sidebar-nav" aria-label="Main navigation">
        {NAV_GROUPS.map((group) => (
          <div className="sidebar-group" key={group.label}>
            <span className="sidebar-group-label">{group.label}</span>
            <ul className="sidebar-group-list">
              {group.items.map((item) => (
                <li key={item.id}>
                  <button
                    className={`sidebar-nav-item${activeView === item.id ? " active" : ""}`}
                    onClick={() => onNavigate(item.id)}
                    aria-current={activeView === item.id ? "page" : undefined}
                  >
                    <span className="sidebar-nav-icon" aria-hidden="true">{item.icon}</span>
                    <span className="sidebar-nav-label">{item.label}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      {/* Daemon status at bottom */}
      <div className="sidebar-status">
        <span className={`status-dot status-${daemonStatus}`} />
        <span className="status-label">{statusLabel}</span>
      </div>
    </aside>
  );
};

export default Sidebar;
