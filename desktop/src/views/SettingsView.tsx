import { useState, useEffect, useCallback } from 'react'
import '../styles/SettingsView.css'

interface SettingsTab {
  id: 'general' | 'approval' | 'memory' | 'channels' | 'scheduling' | 'workspace'
  label: string
  icon: string
}

interface WhatsAppConfig {
  configured: boolean
  phone_number_id?: string
}

interface MemorySettings {
  chunks_indexed: number
  model: string
}

interface IntegrationMetadata {
  vault_keys_present?: string[]
  env_keys_present?: string[]
  required_vault_keys?: string[]
  config?: Record<string, unknown>
}

interface IntegrationStatus {
  id: string
  name: string
  category: string
  description: string
  enabled: boolean
  configured: boolean
  connected: boolean
  connection_source?: string
  status: string
  metadata?: IntegrationMetadata
}

interface IntegrationsResponse {
  live_checks_performed: boolean
  secrets_returned: boolean
  vault_status: string
  config_guidance?: {
    restart_required: boolean
    message: string
  }
  integrations: IntegrationStatus[]
}

interface SettingsViewProps {
  httpPort: number
}

export function SettingsView({ httpPort }: SettingsViewProps) {
  const [activeTab, setActiveTab] = useState<'general' | 'approval' | 'memory' | 'channels' | 'scheduling' | 'workspace'>('general')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const base = `http://127.0.0.1:${httpPort}`

  // State for each tab
  const [whatsappConfig, setWhatsappConfig] = useState<WhatsAppConfig | null>(null)
  const [integrations, setIntegrations] = useState<IntegrationsResponse | null>(null)
  const [memoryStats, setMemoryStats] = useState<MemorySettings | null>(null)
  const [workspacePath, setWorkspacePath] = useState('')
  const [defaultModel, setDefaultModel] = useState('')
  const [strictApproval, setStrictApproval] = useState(false)
  const [autoApprovedToolsText, setAutoApprovedToolsText] = useState('')
  const [heartbeatInterval, setHeartbeatInterval] = useState(30)

  const alwaysGatedTools = [
    'shell.exec',
    'python.execute',
    'file writes',
    'GitHub writes',
    'integration.action',
    'email send',
    'payments',
  ]

  const tabs: SettingsTab[] = [
    { id: 'general', label: 'General', icon: '⚙️' },
    { id: 'approval', label: 'Approval', icon: '✓' },
    { id: 'memory', label: 'Memory', icon: '🧠' },
    { id: 'channels', label: 'Channels', icon: '📱' },
    { id: 'scheduling', label: 'Scheduling', icon: '🕐' },
    { id: 'workspace', label: 'Workspace', icon: '📁' },
  ]

  const loadSettings = useCallback(async () => {
    setLoading(true)
    setError(null)

    try {
      switch (activeTab) {
        case 'general': {
          const configRes = await fetch(`${base}/api/config`)
          if (configRes.ok) {
            const data = await configRes.json()
            setDefaultModel(data.default_model || '')
            setWorkspacePath(data.workspace_dir || '')
          }
          break
        }

        case 'approval': {
          const configRes = await fetch(`${base}/api/config`)
          if (configRes.ok) {
            const data = await configRes.json()
            setStrictApproval(Boolean(data.strict_approval))
            setAutoApprovedToolsText(
              Array.isArray(data.auto_approved_tools) ? data.auto_approved_tools.join('\n') : ''
            )
          }
          break
        }

        case 'memory': {
          const memRes = await fetch(`${base}/api/memory/stats`)
          if (memRes.ok) {
            const data = await memRes.json()
            setMemoryStats(data.stats)
          }
          break
        }

        case 'channels': {
          const [whatsRes, integrationsRes] = await Promise.all([
            fetch(`${base}/api/whatsapp/config`),
            fetch(`${base}/api/integrations/status`),
          ])
          if (whatsRes.ok) {
            const data = await whatsRes.json()
            setWhatsappConfig(data)
          }
          if (integrationsRes.ok) {
            const data = await integrationsRes.json()
            setIntegrations(data)
          }
          break
        }

        case 'workspace': {
          const configRes = await fetch(`${base}/api/config`)
          if (configRes.ok) {
            const data = await configRes.json()
            setWorkspacePath(data.workspace_dir || '')
            setDefaultModel(data.default_model || '')
          }
          break
        }
      }
    } catch (err) {
      setError(`Failed to load ${activeTab} settings: ${err instanceof Error ? err.message : 'Unknown error'}`)
    } finally {
      setLoading(false)
    }
  }, [activeTab, base])

  useEffect(() => {
    void loadSettings()
  }, [loadSettings])

  const handleReindexMemory = async () => {
    setLoading(true)
    setError(null)

    try {
      const res = await fetch(`${base}/api/memory/index`, { method: 'POST' })
      if (res.ok) {
        const data = await res.json()
        setMemoryStats(data.stats)
        setSuccess(`Re-indexed ${data.chunks_indexed} chunks`)
      } else {
        setError('Failed to re-index memory')
      }
    } catch (err) {
      setError(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`)
    } finally {
      setLoading(false)
    }
  }

  const handleSaveConfig = async () => {
    setLoading(true)
    setError(null)

    try {
      const res = await fetch(`${base}/api/config`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...(workspacePath ? { workspace_dir: workspacePath } : {}),
          ...(defaultModel ? { default_model: defaultModel } : {}),
        }),
      })

      if (res.ok) {
        setSuccess('Configuration saved')
      } else {
        setError('Failed to save configuration')
      }
    } catch (err) {
      setError(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`)
    } finally {
      setLoading(false)
    }
  }

  const handleSaveApprovalPolicy = async () => {
    setLoading(true)
    setError(null)

    const autoApprovedTools = autoApprovedToolsText
      .split(/\r?\n/)
      .map(tool => tool.trim())
      .filter(Boolean)

    try {
      const res = await fetch(`${base}/api/config`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strict_approval: strictApproval,
          auto_approved_tools: Array.from(new Set(autoApprovedTools)),
        }),
      })

      if (res.ok) {
        setSuccess('Approval policy saved')
      } else {
        setError('Failed to save approval policy')
      }
    } catch (err) {
      setError(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`)
    } finally {
      setLoading(false)
    }
  }

  const handleSaveSchedule = async () => {
    setLoading(true)
    setError(null)

    try {
      const res = await fetch(`${base}/api/config`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ heartbeat_interval: heartbeatInterval }),
      })

      if (res.ok) {
        setSuccess('Schedule saved')
      } else {
        setError('Failed to save schedule')
      }
    } catch (err) {
      setError(`Error: ${err instanceof Error ? err.message : 'Unknown error'}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="settings-view">
      <div className="settings-header">
        <h1>Settings</h1>
      </div>

      <div className="settings-container">
        {/* Tab Navigation */}
        <div className="settings-tabs">
          {tabs.map(tab => (
            <button
              key={tab.id}
              className={`tab-button ${activeTab === tab.id ? 'active' : ''}`}
              onClick={() => setActiveTab(tab.id)}
            >
              <span className="tab-icon">{tab.icon}</span>
              <span className="tab-label">{tab.label}</span>
            </button>
          ))}
        </div>

        {/* Tab Content */}
        <div className="settings-content">
          {loading && <div className="loading">Loading...</div>}
          {error && <div className="error">{error}</div>}
          {success && <div className="success">{success}</div>}

          {/* General Tab */}
          {activeTab === 'general' && (
            <div className="tab-pane">
              <h2>General Settings</h2>
              <div className="setting-group">
                <label>Default Model</label>
                <input
                  type="text"
                  value={defaultModel}
                  onChange={e => setDefaultModel(e.target.value)}
                  placeholder="e.g., openrouter/minimax/minimax-m2.5"
                />
              </div>
              <button onClick={handleSaveConfig} className="button-primary">
                Save Settings
              </button>
            </div>
          )}

          {/* Approval Tab */}
          {activeTab === 'approval' && (
            <div className="tab-pane">
              <h2>Approval Policy</h2>
              <div className="setting-group approval-toggle-row">
                <div>
                  <label>Strict Approval</label>
                  <p className="hint">
                    Require approval for every tool call. The whitelist remains saved but is ignored while strict approval is on.
                  </p>
                </div>
                <label className="toggle-switch">
                  <input
                    type="checkbox"
                    checked={strictApproval}
                    onChange={e => setStrictApproval(e.target.checked)}
                  />
                  <span />
                </label>
              </div>

              <div className="setting-group policy-summary">
                <strong>Effective Policy:</strong>{' '}
                {strictApproval
                  ? 'Every tool call asks for approval.'
                  : 'Listed reversible tools can run without approval; high-risk tools still ask.'}
              </div>

              <div className="setting-group">
                <label>Reversible Tool Whitelist</label>
                <textarea
                  value={autoApprovedToolsText}
                  onChange={e => setAutoApprovedToolsText(e.target.value)}
                  placeholder="memory.search&#10;web.search&#10;read_file"
                  rows={10}
                />
                <p className="hint">
                  Enter one tool name per line. Use this only for reversible or read-only tools.
                </p>
              </div>

              <div className="setting-group">
                <label>Always Approval-Gated</label>
                <div className="tool-chip-list">
                  {alwaysGatedTools.map(tool => (
                    <span key={tool} className="tool-chip">
                      {tool}
                    </span>
                  ))}
                </div>
              </div>

              <button onClick={handleSaveApprovalPolicy} className="button-primary">
                Save Approval Policy
              </button>
            </div>
          )}

          {/* Memory Tab */}
          {activeTab === 'memory' && (
            <div className="tab-pane">
              <h2>Memory & Search</h2>
              {memoryStats ? (
                <div className="setting-group">
                  <p>
                    <strong>Indexed Chunks:</strong> {memoryStats.chunks_indexed}
                  </p>
                  <p>
                    <strong>Embedding Model:</strong> {memoryStats.model}
                  </p>
                  <button onClick={handleReindexMemory} className="button-secondary">
                    Re-index Memory
                  </button>
                </div>
              ) : (
                <p>No memory data available</p>
              )}
            </div>
          )}

          {/* Channels Tab */}
          {activeTab === 'channels' && (
            <div className="tab-pane">
              <h2>Channel Configuration</h2>
              {integrations && (
                <div className="channel-section">
                  <h3>Integration Status</h3>
                  <p className="hint">
                    {integrations.config_guidance?.message}
                  </p>
                  {integrations.integrations.map(integration => (
                    <div key={integration.id} className="setting-group">
                      <p>
                        <strong>{integration.name}:</strong>{' '}
                        <span className={integration.connected ? 'status-active' : 'status-inactive'}>
                          {integration.connected ? 'Connected' : integration.configured ? 'Configured' : 'Not Configured'}
                        </span>
                      </p>
                      <p className="hint">{integration.description}</p>
                      <p>
                        <strong>Credentials:</strong>{' '}
                        {(integration.metadata?.vault_keys_present?.length || integration.metadata?.env_keys_present?.length)
                          ? [
                              ...(integration.metadata?.vault_keys_present || []).map(key => `vault:${key}`),
                              ...(integration.metadata?.env_keys_present || []).map(key => `env:${key}`),
                            ].join(', ')
                          : `Add ${integration.metadata?.required_vault_keys?.join(' or ') || 'required credentials'} in vault/env`}
                      </p>
                    </div>
                  ))}
                  <p className="hint">
                    Live checks: {integrations.live_checks_performed ? 'on' : 'off'} · Secrets returned: {integrations.secrets_returned ? 'yes' : 'no'} · Vault: {integrations.vault_status}
                  </p>
                </div>
              )}
              <div className="channel-section">
                <h3>WhatsApp</h3>
                {whatsappConfig ? (
                  <div className="setting-group">
                    <p>
                      <strong>Status:</strong>{' '}
                      <span className={whatsappConfig.configured ? 'status-active' : 'status-inactive'}>
                        {whatsappConfig.configured ? 'Configured' : 'Not Configured'}
                      </span>
                    </p>
                    {whatsappConfig.configured && whatsappConfig.phone_number_id && (
                      <p>
                        <strong>Phone ID:</strong> {whatsappConfig.phone_number_id}
                      </p>
                    )}
                    <p className="hint">
                      Configure WhatsApp credentials in the vault to enable messaging.
                    </p>
                  </div>
                ) : (
                  <p>Unable to load WhatsApp configuration</p>
                )}
              </div>
            </div>
          )}

          {/* Scheduling Tab */}
          {activeTab === 'scheduling' && (
            <div className="tab-pane">
              <h2>Scheduling</h2>
              <p className="hint">
                Manage scheduled tasks, heartbeat checks, and periodic jobs here.
              </p>
              <div className="setting-group">
                <label>Heartbeat Interval (minutes)</label>
                <input
                  type="number"
                  value={heartbeatInterval}
                  onChange={e => setHeartbeatInterval(Number(e.target.value))}
                  min={1}
                />
              </div>
              <button onClick={handleSaveSchedule} className="button-primary">Save Schedule</button>
            </div>
          )}

          {/* Workspace Tab */}
          {activeTab === 'workspace' && (
            <div className="tab-pane">
              <h2>Workspace</h2>
              <div className="setting-group">
                <label>Workspace Directory</label>
                <input
                  type="text"
                  value={workspacePath}
                  onChange={e => setWorkspacePath(e.target.value)}
                  placeholder="~/.cato/workspace"
                  readOnly
                />
              </div>
              <p className="hint">
                This directory contains AGENTS.md, MEMORY.md, SOUL.md, and other workspace files.
              </p>
              <button onClick={handleSaveConfig} className="button-primary">
                Update Workspace
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
