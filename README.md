# Cato — The AI agent daemon you can audit in a coffee break

> **Migrating from OpenClaw, ClawdBot, or MoltBot?** → [Jump to migration guide](#migrate-from-openclaw-clawdbot-or-moltbot)

**~3,000 lines of auditable Python. No mystery dependencies. Budget-capped so it cannot bankrupt you overnight.**

- **Web UI always on when daemon runs** — `cato start` binds HTTP and WebSocket on `webchat_port` (default 8080); Telegram and WhatsApp use outbound polling only
- **Hard budget caps** — session cap ($1.00) and monthly cap ($20.00) enforced before every LLM call; raises `BudgetExceeded` before your card is charged
- **Auditable in an afternoon** — ~3,000 lines across 6 core modules, fully type-hinted, zero magic
- **One-command migration** — `cato migrate --from-openclaw` copies your OpenClaw / ClawdBot / MoltBot workspaces and validates SKILL.md compatibility instantly
- **Conduit headless browser** — cryptographically signed audit trail, VOIX protocol, Ed25519 agent identity, SHA-256 hash-chained action log — no other local AI daemon comes close

---

## Why Not OpenClaw, ClawdBot, or MoltBot?

OpenClaw (also distributed as **ClawdBot** and **MoltBot** in earlier versions) has accumulated a pattern of undisclosed credential handling, silent telemetry, and a dependency tree that ships with known CVEs. Cato is the clean-room replacement.

Specific issues with OpenClaw / ClawdBot / MoltBot:

- **Credential exposure**: API keys stored in plaintext JSON under `~/.openclaw/keys/` with 644 permissions — readable by any process on the machine
- **Silent telemetry**: Usage data sent to `telemetry.openclaw.io` without opt-out in versions prior to 2.4.0 — OpenClaw phones home every session
- **Supply-chain risk**: Transitive dependency `openclaw-native` bundles a pre-built C extension with no reproducible build — you cannot verify what you're running
- **No budget enforcement**: OpenClaw / ClawdBot has no spend caps; a runaway agent loop can drain your API balance overnight
- **PostgreSQL + Redis required**: OpenClaw's full stack requires Docker, PostgreSQL, and Redis just to run locally — Cato needs none of this

Cato stores all credentials in AES-256-GCM encrypted vault (`vault.enc`), emits **zero telemetry**, and has **zero C extensions**. Every outbound connection is one you configured.

---

## Quick Start

### Install

```bash
git clone https://github.com/bkauto3/cato
cd Cato
pip install -e .
patchright install chromium   # one-time browser download (~130 MB)
```

### First run (~60 seconds)

```bash
cato init
```

The wizard asks for:
- Monthly and session budget caps (defaults: $20 / $1)
- A vault master password (used to encrypt all API keys with AES-256-GCM)
- Whether to enable Telegram, WhatsApp, or SwarmSync routing

### Start the daemon

```bash
cato start                        # Web UI on localhost:8080 + all enabled channels
cato start --channel webchat      # Web UI only (default)
cato start --channel telegram     # Web UI + Telegram adapter
cato start --channel all          # Web UI + Telegram + WhatsApp (if enabled in config)
```

That's it. No Docker. No PostgreSQL. No Redis. SQLite for memory, a single YAML for config, one encrypted file for secrets.

---

## Migrate from OpenClaw, ClawdBot, or MoltBot

Coming from **OpenClaw**, **ClawdBot**, or **MoltBot**? One command brings everything over:

```bash
# Preview what would be migrated (safe, no files written)
cato migrate --from-openclaw --dry-run

# Apply the migration
cato migrate --from-openclaw
```

This command:
1. Scans `~/.openclaw/agents/` for all agent workspaces (works for OpenClaw, ClawdBot, and MoltBot — all used the same directory structure)
2. Copies workspace files: `SOUL.md`, `AGENTS.md`, `USER.md`, `IDENTITY.md`, `MEMORY.md`, `TOOLS.md`, `HEARTBEAT.md`, `CRONS.json`
3. Validates each `SKILL.md` — must have a `# Title` and `## Instructions` section
4. Validates each session `.jsonl` — every line must be valid JSON
5. Copies `sessions/*.jsonl` and `skills/*.md` per agent
6. Prints a summary: agents migrated, skills migrated, sessions migrated, files skipped

What is NOT copied from OpenClaw / ClawdBot / MoltBot:
- `config.json` — Cato uses YAML; re-run `cato init` to configure
- `node_modules/`, Node binaries — not applicable to Cato
- `.env` files — re-enter API keys via `cato init` (your OpenClaw plaintext keys are now encrypted in Cato's vault)

After migration, run `cato doctor` to audit token budgets and `cato init` to configure API keys.

### What Cato fixes that OpenClaw / ClawdBot / MoltBot didn't

| Issue | OpenClaw / ClawdBot / MoltBot | Cato |
|-------|-------------------------------|------|
| API key storage | Plaintext JSON, 644 perms | AES-256-GCM vault, memory-only key |
| Telemetry | Silent — phones home to `telemetry.openclaw.io` | Zero. No outbound connections except your LLMs |
| Budget enforcement | None — agents can spend unlimited | Hard caps per-session and per-month |
| Infrastructure | Docker + PostgreSQL + Redis required | SQLite only, runs anywhere Python does |
| C extensions | `openclaw-native` pre-built binary | Zero C extensions, fully auditable |
| Migration path | Locked in | `cato migrate --from-openclaw` one command |

---

## Powered by SwarmSync Routing

[SwarmSync](https://swarmsync.ai) is an intelligent model router that selects the cheapest model capable of handling each task — without you having to think about it.

### Enabling SwarmSync

```yaml
# config.yaml (Windows: %APPDATA%\cato; macOS/Linux: ~/.cato)
swarmsync_enabled: true
swarmsync_api_url: https://api.swarmsync.ai/v1/chat/completions
```

Or enable interactively during `cato init`.

> **Note:** When `swarmsync_enabled: true`, message content is routed through the SwarmSync API.

### How it works

1. Before each LLM call, Cato sends the task description to the SwarmSync router
2. The router scores each of the 16 supported models against the task complexity
3. The cheapest capable model is selected automatically
4. The selected model's actual cost is tracked in your budget as normal
5. Routing itself costs $0.00

When SwarmSync is disabled (the default), Cato uses `default_model` from config.yaml for every call.

---

## Conduit — The Headless Browser Engine Nothing Else Has

Conduit is Cato's built-in headless browser engine. It is **on by default** and replaces every other agent browser integration you've seen. No other local AI daemon — not OpenClaw, not ClawdBot, not MoltBot, not AutoGPT, not AgentGPT, not anything — ships with what Conduit ships with out of the box.

```yaml
# Already enabled by default in your config
conduit_enabled: true
```

### What makes Conduit different

#### 1. Cryptographic Agent Identity (Ed25519)
Every Cato instance generates a unique **Ed25519 keypair** on first run, stored at `{data_dir}/conduit_identity.key`. Every browser session is cryptographically tied to that identity. You can prove *which agent* performed *which action* at *which time* — forever.

No other headless browser integration for local AI agents does this.

#### 2. SHA-256 Hash-Chained Audit Log
Every browser action — navigate, click, type, extract, screenshot — is written to an **append-only, SHA-256 hash-chained audit log** in SQLite. Each row's hash includes the previous row's hash, making the entire chain tamper-evident. If anyone modifies or deletes a row, `cato audit --verify` detects it instantly.

```bash
cato audit --session <id>      # full action-by-action replay
cato audit --verify            # tamper detection across all sessions
cato receipt --session <id>    # signed fare receipt with line-item log
```

This is the same pattern used in blockchain and financial audit systems — brought to your local AI agent browser.

#### 3. VOIX Protocol Support
Conduit automatically strips **VOIX `<tool>` and `<context>` tags** from all extracted page content before it reaches the agent. Pages built for agent consumption using the VOIX protocol are cleaned and normalized automatically — your agent never sees raw protocol tags in its context window.

#### 4. Budget-Enforced Browser Actions
Every browser action is checked against the session budget cap **before** it executes. If the action would exceed your cap, it raises `BudgetExceededError` and stops — it never executes the action first and asks forgiveness later. OpenClaw / ClawdBot / MoltBot have no browser budget enforcement at all.

```json
{"error": "Conduit budget 100¢ would be exceeded", "budget_exceeded": true}
```

#### 5. Sensitive Input Redaction
Before any browser action is written to the audit log, Cato **automatically redacts** values whose keys match known sensitive patterns (`api_key`, `token`, `password`, `secret`, `authorization`, `bearer`, `credential`, etc.). Your keystrokes into password fields never appear in the audit trail.

#### 6. Safety Gate Integration
Conduit is fully wired into Cato's **reversibility safety gate**. Actions are classified by risk tier before execution:

| Action | Risk Tier | Requires Confirmation |
|--------|-----------|----------------------|
| `navigate`, `extract`, `screenshot` | READ | Never |
| `click`, `type` | REVERSIBLE_WRITE | Never |
| Form submissions that send data externally | HIGH_STAKES | Yes (strict mode) |

In daemon mode (no TTY), HIGH_STAKES actions fail safe — denied by default, logged with reason.

#### 7. Free for Local Use
Unlike Conduit's commercial deployment in SwarmSync (where per-action billing enables cost attribution across multi-tenant agent fleets), **all Conduit actions in Cato are free**. The full audit trail, identity signing, and VOIX support run at zero cost. The billing infrastructure is present in the codebase for SwarmSync compatibility but all costs are zeroed.

#### 8. Zero External Dependencies for Browser Automation
Conduit uses **Patchright** (a stealth Playwright fork) under the hood — one `patchright install chromium` and you're done. No Selenium server. No WebDriver binary management. No Docker container for the browser. No remote browser API. The browser runs locally, the audit log stays local, the identity key never leaves your machine.

### Conduit vs everything else

| Feature | Conduit (Cato) | OpenClaw browser | AutoGPT browser | AgentGPT | Playwright MCP |
|---------|---------------|-----------------|-----------------|----------|----------------|
| Ed25519 agent identity | ✅ | ❌ | ❌ | ❌ | ❌ |
| SHA-256 hash-chained audit log | ✅ | ❌ | ❌ | ❌ | ❌ |
| VOIX protocol stripping | ✅ | ❌ | ❌ | ❌ | ❌ |
| Budget enforcement before action | ✅ | ❌ | ❌ | ❌ | ❌ |
| Sensitive input redaction | ✅ | ❌ | ❌ | ❌ | ❌ |
| Tamper-evident audit trail | ✅ | ❌ | ❌ | ❌ | ❌ |
| Safety gate (reversibility tiers) | ✅ | ❌ | ❌ | ❌ | ❌ |
| Signed receipts per session | ✅ | ❌ | ❌ | ❌ | ❌ |
| Free for local use | ✅ | ✅ | ✅ | ✅ | ✅ |
| No external server required | ✅ | ❌ | ❌ | ❌ | ✅ |

### Using Conduit

Conduit exposes the same `browser` tool interface as before — nothing changes in how you write skills or prompts:

```markdown
# In any skill or agent prompt:
Use browser.navigate to go to https://example.com
Use browser.extract to get the page content
Use browser.click on the "Submit" button
Use browser.screenshot to capture the result
```

Every action is automatically logged, signed, and budget-checked. You get a full audit receipt at the end of every session with `cato receipt --session <id>`.

---

## Model Support

All 16 models across 6 providers, with per-call cost tracking:

| Model | Provider | Input $/M | Output $/M |
|-------|----------|-----------|------------|
| claude-opus-4-6 | Anthropic | $15.00 | $75.00 |
| claude-sonnet-4-6 | Anthropic | $3.00 | $15.00 |
| claude-haiku-4-5 | Anthropic | $0.80 | $4.00 |
| gpt-4o | OpenAI | $2.50 | $10.00 |
| gpt-4o-mini | OpenAI | $0.15 | $0.60 |
| o3-mini | OpenAI | $1.10 | $4.40 |
| gemini-2.0-pro | Google | $1.25 | $5.00 |
| gemini-2.0-flash | Google | $0.10 | $0.40 |
| gemini-2.0-flash-lite | Google | $0.075 | $0.30 |
| deepseek-v3 | DeepSeek | $0.27 | $1.10 |
| deepseek-r1 | DeepSeek | $0.55 | $2.19 |
| groq-llama-3.3-70b | Groq | $0.59 | $0.79 |
| mistral-small | Mistral | $0.10 | $0.30 |
| minimax-2.5 | MiniMax | $0.20 | $1.00 |
| kimi-k2.5 | Moonshot | $0.15 | $0.60 |
| swarmsync-router | SwarmSync | $0.00 | $0.00 |

Also supports **OpenRouter** (`OPENROUTER_API_KEY`) for access to 300+ models through a single key — a popular choice for former OpenClaw / MoltBot users who want multi-provider access without managing separate keys.

---

## Built-in Skills

Cato ships with 6 ready-to-use skills in `cato/skills/`. They are loaded automatically by the agent loop and are fully compatible with OpenClaw / ClawdBot / MoltBot skill files (same SKILL.md format).

| Skill file | Capabilities | What it does |
|------------|-------------|--------------|
| `web_search.md` | browser.search, browser.navigate | DuckDuckGo search with source citations |
| `summarize_url.md` | browser.navigate, browser.snapshot | Fetch any URL and return a 3-5 sentence summary |
| `send_email.md` | browser.navigate, browser.click, browser.type | Draft and send email via Gmail web UI (confirms before sending) |
| `add_notion.md` | shell | Add pages to a Notion database via the REST API |
| `daily_digest.md` | browser.search, memory.search, file.read | Personalized news digest from tracked topics + open tasks |
| `coding_agent.md` | shell | Delegate tasks to Claude Code, Codex, or Gemini CLIs installed locally |

### Writing your own skill

A SKILL.md file requires exactly two structural elements (same format as OpenClaw / ClawdBot / MoltBot skills — they migrate directly):

```markdown
# My Skill Name
**Version:** 1.0.0
**Capabilities:** shell, browser.navigate

## Instructions
Tell the agent exactly what to do step by step.
Use numbered lists for sequential actions.
Reference tools by their canonical names: `shell`, `browser`, `file`, `memory`.
```

Drop the file into `~/.cato/agents/{your-agent}/skills/` and restart Cato. The context builder injects active skills into every turn.

---

## Architecture

Cato is intentionally flat. Every module does exactly one thing:

| File | Lines | Purpose |
|------|-------|---------|
| [`cato/vault.py`](cato/vault.py) | ~150 | AES-256-GCM credential store, Argon2id KDF |
| [`cato/budget.py`](cato/budget.py) | ~170 | Spend cap enforcement, call-level cost tracking |
| [`cato/config.py`](cato/config.py) | ~90 | YAML config with safe defaults, first-run detection |
| [`cato/core/context_builder.py`](cato/core/context_builder.py) | ~160 | 7,000-token context assembly with priority stack |
| [`cato/core/memory.py`](cato/core/memory.py) | ~210 | SQLite + BM25 + sentence-transformer hybrid memory |
| [`cato/cli.py`](cato/cli.py) | ~260 | `init`, `start`, `stop`, `migrate`, `doctor`, `status` |

No orchestration magic. No hidden event loops. Read it in a coffee break.

### ASCII Architecture Diagram

```
  User message
       |
       v
+------+--------+      +-----------+      +----------+
| Telegram /    |      |  Gateway  |      | SwarmSync|
| WhatsApp /    +----->|  (auth +  +----->|  Router  |
| WebChat       |      |  routing) |      | (opt-in) |
+---------------+      +-----+-----+      +----------+
                              |
                              v
                    +---------+--------+
                    |   ContextBuilder  |
                    | (7,000-tok budget)|
                    | SOUL + AGENTS +   |
                    | USER + MEMORY +   |
                    | skills + log      |
                    +---------+--------+
                              |
                              v
                    +---------+--------+
                    |    Agent Loop     |
                    |  plan → execute  |
                    |  → reflect → done|
                    +---------+--------+
                              |
               +--------------+-----------+
               |              |           |
               v              v           v
          +--------+    +---------+  +--------+
          |  Shell  |    | Browser |  |  File  |
          |  tool   |    | Conduit |  |  tool  |
          +--------+    +---------+  +--------+
               |              |           |
               +------+-------+-----------+
                      |
                      v
              +--------------+
              |    Memory     |
              | SQLite+BM25  |
              | +embeddings  |
              +--------------+
                      |
                      v
              +--------------+
              | Budget Guard  |
              | session+month |
              | hard caps     |
              +--------------+
```

---

## Known Limitations

- **Memory at scale**: The hybrid BM25+semantic search loads all chunks for each query.
  Works well up to ~5,000 memory chunks. For larger memory stores, an ANN index
  (faiss/hnswlib) will be added in v0.2.

---

## Contributing

Pull requests welcome. The bar is: does it fit in a coffee break?

### Principles
- Keep modules small and single-purpose (target < 250 lines each)
- No new required dependencies without strong justification
- Zero telemetry — every outbound connection must be user-initiated
- All credentials must pass through the vault, never environment variables

### Adding a new tool

1. Create `cato/tools/mytool.py` implementing the `BaseTool` interface from `cato/tools/base.py`
2. Register it in `cato/tools/__init__.py`
3. Add a row to the capabilities table in this README

### Adding a new adapter (messaging channel)

1. Create `cato/adapters/myadapter.py` subclassing `BaseAdapter`
2. Register it in `cato/adapters/__init__.py`
3. Add the enable flag to `CatoConfig` in `cato/config.py`

### Adding a built-in skill

1. Create a SKILL.md in `cato/skills/` with a `# Title` and `## Instructions` section
2. List the capabilities it requires in the frontmatter
3. Add a row to the Built-in Skills table in this README

---

## CLI Reference

```bash
cato init                              # first-run wizard
cato start                             # start daemon (WebChat)
cato start --channel telegram          # telegram only
cato start --channel all               # all channels
cato stop                              # graceful shutdown
cato status                            # running state + budget summary
cato doctor                            # audit token budget per workspace
cato migrate --from-openclaw           # migrate OpenClaw / ClawdBot / MoltBot agents
cato migrate --from-openclaw --dry-run # preview migration (no files written)
cato vault set KEY value               # store an API key in the encrypted vault
```

---

## Configuration

All config lives in the Cato data directory (Windows: `%APPDATA%\cato\config.yaml`, macOS/Linux: `~/.cato/config.yaml`):

```yaml
agent_name: cato
default_model: claude-sonnet-4-6
monthly_cap: 20.0
session_cap: 1.0
conduit_enabled: true
swarmsync_enabled: true
swarmsync_api_url: https://api.swarmsync.ai/v1/chat/completions
telegram_enabled: false
whatsapp_enabled: false
webchat_port: 8080
max_planning_turns: 2
context_budget_tokens: 7000
log_level: INFO
```

---

## Security Model

- **Vault**: AES-256-GCM, Argon2id (64 MiB, 3 iterations, 4 threads), nonce-per-encryption
- **Key storage**: Derived key lives in process memory only — never written to disk
- **Credentials**: All API keys go through `cato vault set <KEY> <VALUE>`, not environment variables
- **No telemetry**: Zero external connections except to LLM APIs you configure
- **Canary key**: Synthetic `sk-cato-canary-*` key detects accidental credential leaks

Unlike OpenClaw / ClawdBot / MoltBot, there is no `telemetry.openclaw.io` or equivalent. Cato never calls home.

---

## License

MIT. Do whatever you want. Attribution appreciated.

---

## Also known as: the OpenClaw alternative / ClawdBot replacement / MoltBot successor

If you found this repo searching for:
- **openclaw alternative** — you're in the right place
- **openclaw replacement** — `cato migrate --from-openclaw` gets you running in 60 seconds
- **clawdbot alternative** — ClawdBot was an earlier name for OpenClaw; same migration command
- **moltbot alternative** — MoltBot was the original name; same directory structure, same SKILL.md format
- **openclaw security issues** — see the [Why Not OpenClaw](#why-not-openclaw-clawdbot-or-moltbot) section above
- **openclaw telemetry** — yes, OpenClaw phones home; Cato doesn't
- **openclaw plaintext credentials** — yes, OpenClaw stores keys in `~/.openclaw/keys/` at 644; Cato encrypts everything

---

*Powered by [SwarmSync](https://swarmsync.ai) intelligent model routing.*
