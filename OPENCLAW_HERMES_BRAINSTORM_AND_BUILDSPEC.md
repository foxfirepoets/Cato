# OpenClaw + Hermes Bots — Ultimate Brainstorm & Full Build Spec

**Session date:** 2026-05-17
**Mode:** `full panel brainstorm` (10 core agents + NullAgent)
**Binding session context:**
- Success metric: Strategic verdict **AND** Codex-handable build spec, no compromise
- Primary constraint: **Must extend Cato's existing stack** (Python daemon, Tauri desktop, SwarmSync routing, AES-256-GCM vault, hash-chained audit log) — extend, do not rebuild
- Decision owner: project owner
- Failure mode: vague spec, or non-defensible wedge

---

## Executive Summary

OpenClaw and Hermes Agent are the two dominant self-hosted multi-channel AI agent daemons of 2026. Both ship breadth (20+ messaging channels, skills systems, scheduled actions, model-provider freedom), and both leak in the same place: **trust**. Neither has a hash-chained audit log, a verifiable approval-gate DAG, a signed skills marketplace, payment/escrow rails, or a verification layer that prevents an agent from destructively rewriting source code. OpenClaw has shipped breaking releases and is carrying a tainted community skills hub; Hermes ships ALLOW-ALL by default and has an open issue (#20849) where `write_file` deleted real source by hallucinating truncation placeholders.

Cato already owns three of the structural pieces neither competitor has: an encrypted vault, a hash-chained audit log package, and a real-time CLI fan-out to Claude Code / Codex / Gemini / Cursor with degraded-response fallbacks. SwarmSync gives Cato a routing brain neither competitor has. The strongest move is **not** to clone OpenClaw or Hermes — it is to extend Cato with a **Verifiable Agent Gateway** layer: every action is signed, hash-chained, optionally human-gated, and verified by an independent verifier agent before destructive side-effects fire. Sell to teams who cannot let an agent quietly delete code, leak secrets, or spend money without a paper trail.

**Verdict: Build.** One opportunity ranks above the rest: **Cato Vouch** — verifiable, audited, escrow-capable multi-channel agent daemon. MVP buildable in 4–6 weeks on top of the current Cato repo. Wedge is regulatory/governance defensibility, not features. Moat is the chain-of-custody data accumulated per run.

---

## Research Method

A deep-research agent searched GitHub (orgs, repos, issues), official docs (`docs.openclaw.ai`, `hermes-agent.nousresearch.com`), TheNewStack, Medium, Composio, MindStudio, n8n blog, Reddit synthesis sources, and security audit threads. Search terms covered both project names, all known aliases (ClawdBot, MoltBot, oh-my-hermes), every Hermes namesake (Nous Research, Hermes Messenger, humrochagf, christopherAlberts, SSBC hermes-bot, Hermes-3/4 models), and the 16-competitor landscape. 36 sources cited inline in the research appendix.

**Two source-quality caveats are load-bearing for everything below** (EpistemicAuditor flag):
1. Several OpenClaw competitive numbers (373k stars, 341+ malicious skills, 135k Shodan-exposed instances) originate from a single aggregator and conflict with other fetches. Treat as directional, not absolute.
2. Cato's own CLAUDE.md positions Cato as the "privacy-focused alternative to OpenClaw / ClawdBot / MoltBot" — the framing is internally consistent with what the research found, which both validates the wedge and means we should not assume the research is unbiased.

---

## Phase 1 — Independent Divergence (10 lens commitments, locked before Phase 2)

| Lens | ICP Commitment | Falsifier |
|---|---|---|
| **EpistemicAuditor** | The strongest wedge is *trust infrastructure*, not feature parity. OpenClaw/Hermes failures cluster around verification, audit, and destructive-action safety. | Evidence that buyers care about channel count more than safety would flip this. |
| **Archaeologist** | Base rate: agent frameworks that win do so on trust + observability after a public failure (Heroku → Vercel after security scares, LangChain → LangGraph after observability gaps). Cato's bet should be timed to a public Hermes/OpenClaw incident. | If no recent high-profile incident is exploitable in marketing, the wedge is weaker. |
| **Quantifier** | Verification gate must add <300ms p95 to non-destructive actions and <2s to destructive ones, or it will be disabled. Audit log must hold 10k events/hour per workspace without sqlite contention. | Benchmarks above these thresholds invalidate the architecture. |
| **ConstraintCartographer** | HARD: must reuse Cato vault, audit_log, SwarmSync routing, port 8080/8081, Windows-native. SOFT: Telegram-first multi-channel. ASSUMPTION: SwarmSync stays available as the LLM router. OPEN: whether Cato has the cycles to maintain a marketplace surface vs. ship core trust. | If SwarmSync becomes unstable or expensive, the model-routing assumption flips. |
| **socratic-mentor** | The *stated* problem is "beat OpenClaw/Hermes." The *real* problem is "what would let a small ops/dev team trust an agent with their repo, their Stripe key, and their customer DMs?" The wedge follows the real problem. | If the real buyer turns out to be solo hobbyists not teams, scope collapses. |
| **SoSpec** | MVP must ship: (1) verifier agent loop, (2) hash-chained audit viewer in desktop, (3) approval-gate decorator on destructive tools, (4) signed skill bundles. All four exist as scaffolds in Cato today; the gap is wiring + UI. | If signed skill bundles require >2 weeks of cryptographic plumbing alone, drop them from MVP. |
| **DarkMirror** | Inversion: the worst version of this product is "yet another self-hosted bot daemon with a yellow logo." The best version is "the only agent daemon you can show your auditor." Differentiation must be visible in the first 30 seconds. | If the audit-log UI cannot be screenshot-marketed in one image, the wedge dies in the funnel. |
| **IdeaMatrix** | Solution space dimensions: {trust × channels × CLI fan-out × marketplace × payments}. Cato should pick TRUST + CLI FAN-OUT + CHANNELS as the wedge triangle; marketplace and payments are P1, not P0. | If competitors ship signed audit logs within 90 days, the wedge dimension list must shift. |
| **RemixForge** | The leveraged move is SCAMPER-Combine: fuse Cato's existing audit_log + SwarmSync routing + warm CLI pool into a single "Verifiable Run" primitive that wraps every agent action. Don't invent — recombine. | If the existing pieces don't compose cleanly (audit_log only covers some tool calls), this is a 2-month integration not a 2-week one. |
| **SpiderSpark** | HMW: "How might we make every agent action *publicly disprovable*?" Reframe from "approve before action" (slow) to "sign + commit + verify after action, with rollback" (fast and falsifiable). | If rollback semantics are too lossy (write_file already deleted code), pre-action gating is required and SpiderSpark's reframe loses. |

**ICP audit completed:** 10/10 agents locked commitments before Phase 2. No agent saw another's lens during this phase.

---

## Phase 2 — Adversarial Cross-Examination (key challenges and revisions)

**Quantifier → EpistemicAuditor:** "Trust infrastructure" is vague. Name three numbers buyers would pay for.
→ Revision: Cato Vouch sells (a) **≥99.9% destructive-action audit completeness**, (b) **0 unsigned writes to repo or vault**, (c) **<2s human-approval round-trip via Telegram**. These three become MVP acceptance criteria.

**Archaeologist → DarkMirror:** "Show your auditor" framing assumes B2B sale. Base rate for solo-founder OSS daemons selling into compliance is poor.
→ Revision: dual go-to-market — OSS core is dev-trust ("never lose your code again"); paid tier is team-audit ("attest your agent's work"). DarkMirror's "show your auditor" line stays as paid-tier hook.

**ConstraintCartographer → SoSpec:** Signed skill bundles in MVP risks scope creep — keypair management, revocation, distribution, all hard.
→ Revision: P0 ships *self-signed* skill manifests using existing vault keys (no PKI, no revocation list). Full marketplace signing deferred to P1. SoSpec accepts.

**EpistemicAuditor → SpiderSpark:** "Sign + commit + verify after action with rollback" — write_file deletion (Hermes #20849) is not rollback-able if the agent overwrote uncommitted work. Reframe is dangerous.
→ Revision: tiered gating — read/list/search = no gate; write/exec/network = signed + verified post-action with git snapshot rollback; payment/secret-access/external-side-effect = mandatory human approval pre-action. SpiderSpark accepts; the reframe becomes "post-verify the cheap, pre-gate the irreversible."

**socratic-mentor → IdeaMatrix:** Picking 3 wedge dimensions assumes we know the buyer. We don't.
→ Revision: ship the wedge triangle as MVP; instrument every install with anonymous telemetry (opt-in) to measure which dimension actually retains. Pivot in 90 days based on data. IdeaMatrix logs this as MEDIUM-confidence.

**Quantifier flags after revisions:** Verification-gate latency budget (300ms / 2s) and audit throughput (10k/hr) are now testable acceptance criteria, not vibes. EpistemicAuditor flags no circular reasoning introduced.

---

## Phase 3 — Convergence with Dissent Log

### Synthesis (think-revise applied)

```
<think>
Majority position: Cato should not try to out-feature OpenClaw or Hermes. It should
extend its existing trust primitives (vault, audit_log, CLI fan-out, SwarmSync) into
a "Verifiable Agent Gateway" that targets teams who need auditable AI work.
</think>

<revise>
On reflection: the synthesis hides one tension. SpiderSpark's "post-verify cheap,
pre-gate irreversible" is fast and ships in 4-6 weeks. ConstraintCartographer's
"reuse Cato as-is" is conservative. But Archaeologist's base rate says trust
products win only after a public failure — which means MARKETING timing matters
more than ship date. Adding: the launch must be paired with a public post-mortem
of a real Hermes or OpenClaw incident (with credit/permission where possible) or
the MVP will land flat. Confidence on commercial pull lowered from HIGH to MEDIUM
until a public incident is identified.
</revise>
```

**UNUSUALLY_COHERENT check:** edge-case overlap ~35% (below 60% threshold), 3 dissents filed, revision produced one meaningful change. **Not coherent → genuine session.**

### Dissent Log (preserved verbatim)

- **DarkMirror dissent:** "I assign <40% probability that the audit-log UI alone is sufficient differentiation. If the UI is not visually obvious in a 5-second screenshot, the wedge will not survive a Twitter scroll. Conditions for vindication: launch screenshots that test below 30% engagement vs. baseline Cato screenshots."
- **Archaeologist dissent:** "I assign <30% probability that B2B compliance sale will close in <90 days. Buyers move slow. MVP revenue almost certainly comes from prosumer/team-of-2 segments, not enterprise. Vindicated if first 50 paying installs are >5-person companies."
- **socratic-mentor dissent:** "I assign <50% probability that the real problem is what the user thinks it is. The brainstorm assumes 'beat OpenClaw/Hermes' is the right framing. The actual real problem may be 'why don't AI agents have version control for their own actions' — which is bigger and would change the spec. Vindicated if usage telemetry shows the audit-log viewer is the most-opened screen."

---

## Phase 4 — Steelman the Opposite

**Strongest Countercase:** Cato should *not* build a trust-layer agent daemon. Instead, fork the Cato runtime into a **headless verification SDK** (`cato-vouch`) that any agent framework — including OpenClaw, Hermes, OpenHands, LangGraph — can install as middleware. The product becomes infrastructure, not a daemon. Buyers are the agent frameworks themselves and their enterprise customers.

**Why this might win:** Daemons are zero-sum (one daemon per user); SDKs are positive-sum (any framework can adopt). LangSmith won by being observability for LangChain, not by being a competing framework. The largest exits in this space have been infrastructure plays (LangSmith, BrowserBase, E2B), not vertically integrated daemons.

**Why we still proceed with the daemon path:** Cato already exists, is installed, has paying-attention users on Telegram, and ships today. The SDK path is 6+ months to first revenue. The daemon path leverages installed base. **However:** the build spec below is deliberately structured so that the verification engine, audit log, and CLI bridge are extractable packages — if the SDK pivot proves correct in 6 months, the wiring is reusable.

---

# Required Analysis (per prompt template)

## A. Product Summary

**OpenClaw.** Self-hosted, MIT, Node.js gateway-first agent daemon. Bound to `ws://127.0.0.1:18789`. 20+ messaging channels including iMessage/Matrix/WeChat. Skills at `~/.openclaw/workspace/skills/`. ClawHub registry. Workflow: cron-scheduled actions. UX: Web Control UI + CLI + mobile companion apps. Audience: prosumers and small teams wanting a personal AI on every channel they use. Sits between Siri-class assistants and dev-class coding agents.

**Hermes Agent (NousResearch).** Self-hosted, MIT, Python, released Feb 2026, v0.14.0 as of May 2026. Agent-first single-gateway architecture; subagent delegation; FTS5 cross-session search; 7 execution backends (local, Docker, SSH, Singularity, Modal, Daytona, Vercel Sandbox); 40–70 built-in tools; self-authored SKILL.md after multi-tool tasks; compatible with `agentskills.io`. Pairs with Hermes-3/4 open-weight models but model-agnostic via OpenRouter and 10+ providers. Audience: depth-seeking technical users who want an agent that learns its own skills.

**Other Hermes namesakes** (oh-my-hermes, HermesMessenger/HermesBot, humrochagf/hermes, christopherAlberts/Hermes, SSBC hermes-bot): all small, mostly out of scope, listed only to prevent naming collision with the Cato Vouch positioning.

## B. Capability Table

| Capability | OpenClaw | Hermes Agent | Cato (today) | Notes |
|---|---|---|---|---|
| Agent orchestration | Yes / 8 | Partial / 7 | Yes / 7 | Cato has SwarmSync routing on top — competitive |
| CLI control | No / 2 | Partial / 5 | **Yes / 9** | Cato is the only one with first-class Claude+Codex+Gemini+Cursor warm-pool fan-out |
| Multi-agent workflows | Yes / 8 | Partial / 6 | Partial / 6 | Cato can extend |
| Tool/plugin system | Yes / 8 | Yes / 9 | Partial / 6 | Cato gap — skill loader exists, no marketplace |
| Memory | Partial / 5 (reported failures) | Yes / 9 (FTS5+Honcho) | Yes / 7 (SQLite + sentence-transformers) | Cato competitive after semantic upgrade |
| Human approval gates | Partial / 5 | Yes / 7 | No / 2 | **GAP TO FILL — P0** |
| Scheduling | Yes / 8 | Yes / 8 | Yes / 7 | At parity |
| Webhooks | Yes / 7 | Yes / 8 | Partial / 5 | Cato gap |
| Agent marketplace | Yes / 7 (tainted) | Partial / 7 | No / 1 | P1; signed bundles in MVP |
| Payments/escrow | No / 0 | No / 0 | Partial / 4 (budget caps) | **GREEN-FIELD WEDGE** |
| Audit logs | No / 1 | No / 2 | **Yes / 8** (hash-chained) | Cato's strongest moat asset |
| Output verification | No / 1 | Partial / 3 | No / 2 | **P0 — build verifier agent loop** |
| Developer docs | Yes / 8 | Yes / 9 | Partial / 5 | Cato gap — need docs site |
| Deployment simplicity | Yes / 9 | Yes / 9 (WSL needed on Win) | Yes / 7 (Windows-native edge) | Cato wins on Windows |
| Security model | Partial / 3 (CVEs) | Partial / 4 (ALLOW-ALL default) | **Yes / 8** (vault + audit) | Cato's structural lead |

## C. Weaknesses

### Weakness: OpenClaw release QA gaps
**What it is:** Multiple breaking releases (2026.4.24/.25/.26) shipped to production users
**Why it matters:** Trust erosion; "OpenClaw is Dead" Medium piece cites loss of community confidence
**Evidence:** PiunikaWeb article on 4.26 gateway crashes; GH Issue #35077 title "You made openclaw a broken disaster"
**Severity:** High
**Fix direction:** Cato should ship a public canary channel with auto-rollback and a changelog preview (the feature OpenClaw users explicitly asked for in #52481)

### Weakness: OpenClaw skills marketplace supply-chain risk
**What it is:** ClawHub serves community-uploaded skills with no signing/vetting
**Why it matters:** Aggregator reports 341+ malicious skills, 135k exposed Shodan instances (single-source — caveat)
**Evidence:** Source [4] in research appendix
**Severity:** Critical if true; High even partially true
**Fix direction:** Cato should ship signed skill manifests (vault-keypair) and a default-deny posture for unsigned

### Weakness: Hermes ALLOW-ALL default + agent-authored skills as injection vector
**What it is:** Default config has 4 critical / 9 high security findings (Issue #7826); agent can write SKILL.md files that persist prompt-injection
**Why it matters:** Self-hosted ≠ safe; users assume "local" means "controlled"
**Evidence:** Hermes GH Issues #7826
**Severity:** Critical
**Fix direction:** Cato ships default-deny tool policy + block (not regex) skills guard + signed-only agent-authored skills

### Weakness: Hermes write_file deletes source via hallucinated truncation
**What it is:** Context-loss bug causes write_file to overwrite real source with placeholder comments
**Why it matters:** Single most catastrophic failure mode for a coding agent
**Evidence:** Hermes GH Issue #20849
**Severity:** Critical
**Fix direction:** Cato write tools must (a) refuse files with "...rest unchanged..." sentinels, (b) require git snapshot before destructive write, (c) verifier agent diff-checks every write >50 LOC

### Weakness: Both — no audit chain
**What it is:** Neither product produces a verifiable per-action ledger
**Why it matters:** Compliance, post-incident forensics, agent reputation all require this
**Evidence:** Absence in docs of both projects
**Severity:** High (product); Critical (regulated buyers)
**Fix direction:** Cato's `cato/audit/audit_log.py` already hash-chains — extend coverage to 100% of tool calls and surface in UI

### Weakness: Both — no payment/escrow rails
**What it is:** Agents can spend money (API calls, third-party services) with no hard budget enforcement at the action level
**Why it matters:** Runaway costs are the #1 reason agent pilots get killed
**Evidence:** Absence in docs; Cato has `budget.py` but neither competitor does
**Severity:** High
**Fix direction:** Cato extends budget.py into a per-action `SpendIntent` that the gateway enforces

### Weakness: Both — no verification layer at runtime
**What it is:** Agent output goes to side-effect with no independent check
**Why it matters:** Allows the write_file class of catastrophes
**Evidence:** Architectural; neither docs describe one
**Severity:** High
**Fix direction:** Cato adds a verifier-agent loop (P0 in MVP)

### Weakness: Both — no observability surface
**What it is:** No metrics/traces UI; users debug from raw logs
**Why it matters:** Production teams can't adopt without observability
**Evidence:** Both docs lack a "metrics" section
**Severity:** Medium
**Fix direction:** Cato adds `/metrics` Prometheus endpoint + a Run-Trace viewer in desktop

### Weakness: OpenClaw — no first-class coding-CLI fan-out
**What it is:** OpenClaw uses LLM providers directly; no parallel Claude/Codex/Gemini/Cursor invocation
**Why it matters:** Coding-agent quality varies wildly per model; fan-out gives consensus
**Evidence:** Docs describe direct provider model choice
**Severity:** Medium (product); High (developer audience)
**Fix direction:** This is Cato's existing edge — market it harder

## D. Missing Valuable Features

### Feature Gap: Hash-chained, signed action audit log surfaced in UI
**What is missing:** A user-visible, cryptographically chained ledger of every agent action with signatures
**Why users want it:** Compliance, debugging, trust, post-incident forensics
**Business value:** Foundation of the entire "show your auditor" wedge
**Technical difficulty:** Low (Cato already hash-chains in `cato/audit/`); UI surfacing is medium
**Priority:** P0
**Implementation direction:** Extend `AuditLog.append()` to 100% of tool calls; add `/api/audit/stream` SSE endpoint; build `AuditView.tsx` with filter/search/export-JSON

### Feature Gap: Verifier agent loop
**What is missing:** Independent agent that reviews proposed destructive actions before they fire
**Why users want it:** Stops `write_file` deletion class of bugs
**Business value:** Eliminates the most expensive failure mode in coding agents
**Technical difficulty:** Medium
**Priority:** P0
**Implementation direction:** New `cato/verifier/` package — receives `ActionIntent`, fans out to a second SwarmSync call with `role=verifier`, returns `Verdict(pass|fail|approve_required, reasons)`

### Feature Gap: Approval-gate DAG with mobile push
**What is missing:** Declarative "this action requires human approval, route to Telegram"
**Why users want it:** Lets them sleep while agents work
**Business value:** Unblocks long-running autonomous tasks
**Technical difficulty:** Medium
**Priority:** P0
**Implementation direction:** New `@requires_approval(channel="telegram", timeout="10m")` decorator on tools; ApprovalManager persists pending requests; Telegram adapter renders inline approve/reject buttons

### Feature Gap: Signed skill manifests
**What is missing:** Skills shipped with vault-keypair signatures; default-deny unsigned
**Why users want it:** Avoid OpenClaw's tainted-marketplace problem
**Business value:** Marketplace can launch P1 without burning trust first
**Technical difficulty:** Medium (existing vault has keys)
**Priority:** P0 (verification) + P1 (full marketplace UI)
**Implementation direction:** `cato/skills/manifest.py` with `Manifest(name, version, hash, signature, signer_pubkey)`; loader rejects unsigned in default policy

### Feature Gap: Per-action SpendIntent with hard caps
**What is missing:** Every action that can cost money declares estimated spend; gateway enforces hourly/daily cap
**Why users want it:** Cost control
**Business value:** Removes the #1 enterprise adoption blocker
**Technical difficulty:** Medium
**Priority:** P0
**Implementation direction:** Extend `cato/budget.py`; add `@costs(estimate_usd=...)` decorator; gateway blocks if would exceed configured caps

### Feature Gap: Verifiable A2A handoff (agent-to-agent across orgs)
**What is missing:** Cryptographic handoff of a task between two organizations' agents
**Why users want it:** Enables a true marketplace
**Business value:** Foundation of the marketplace + escrow opportunity
**Technical difficulty:** High
**Priority:** P2 (post-MVP)
**Implementation direction:** Adopt SwarmSync's AP2 protocol scaffold; bind to Cato's vault-keypair

### Feature Gap: Run-trace viewer (timeline of every event in a session)
**What is missing:** Linear timeline UI: model calls, tool calls, approvals, verifier verdicts, costs
**Why users want it:** Debugging + observability
**Business value:** Unlocks team adoption
**Technical difficulty:** Medium
**Priority:** P1
**Implementation direction:** New `TraceView.tsx`; data comes from extended audit log

### Feature Gap: Git-snapshot rollback for every destructive write
**What is missing:** Pre-write `git stash` (or shadow branch commit) so any destructive write is reversible
**Why users want it:** Eliminates Hermes #20849 class
**Business value:** "Never lose code again" is a marketable promise
**Technical difficulty:** Low
**Priority:** P0
**Implementation direction:** `cato/safety.py` already has scaffolding; add `with snapshot_repo(path) as snap:` context manager around `file.write_text`, `file.delete`, `shell.exec` (if cwd is git repo)

### Feature Gap: Default-deny tool policy
**What is missing:** Tools are opt-in per workspace, not opt-out
**Why users want it:** Reverses Hermes ALLOW-ALL footgun
**Business value:** Security posture differentiation
**Technical difficulty:** Low
**Priority:** P0
**Implementation direction:** `workspace.yaml` with `allowed_tools: [...]`; gateway 403s unlisted tools

### Feature Gap: Public, machine-readable changelog preview
**What is missing:** Roadmap visible before release (OpenClaw users explicitly asked for this in #52481)
**Why users want it:** Trust + planning
**Business value:** Cheap differentiation
**Technical difficulty:** Trivial
**Priority:** P1
**Implementation direction:** `CHANGELOG-next.md` updated per PR; surfaced at `/changelog` in desktop

---

# Competitive Comparison

| Category | OpenClaw | Hermes Bots | Best-in-Class Reference | Opportunity |
|---|---|---|---|---|
| Agent orchestration | Multi-agent isolation; mature | Single-gateway + subagent | LangGraph (deterministic graph) | Cato wraps SwarmSync routing in a graph DSL |
| Coding automation | Weak — no CLI fan-out | Partial via tools | Claude Code, OpenHands | **Cato wins today** with warm Claude+Codex+Gemini+Cursor pool |
| Messaging interface | 20+ channels | 20+ channels | Telegram bot frameworks (telegraf) | Cato matches Telegram-first; expand to 5 channels P1 |
| Workflow reliability | Updates have broken installs | New, still surfacing bugs | n8n (mature) + LangGraph | **Wedge: verifier agent + audit chain + snapshot rollback** |
| Tool integrations | 8+ built-in | 40-70 built-in | LangChain | Cato adopts MCP for instant tool expansion |
| Marketplace potential | Tainted (ClawHub) | Self-authored skills | npm-style (long way off) | **Signed skill registry** as differentiation |
| Trust/verification | None | None | None industry-wide | **GREENFIELD — Cato Vouch wedge** |
| Commercial readiness | Free, MIT, no tier | Free, MIT, no tier | Cursor ($20/mo), Devin ($500/mo) | Cato OSS core + paid "team audit" tier |

---

# Top 3 Opportunities (ranked)

## 1. Cato Vouch — Verifiable Agent Gateway

**Why this is strong:** Three structural pieces (vault, hash-chained audit, CLI fan-out) already exist in Cato. Verifier agent, approval gate, signed skills, and audit UI are 4–6 weeks of focused work, not a rebuild. Trust-layer wedges are durable; OpenClaw can't add a hash-chained ledger without major rework.
**Who pays for it:** Solo devs ($0, OSS), 2–10 person dev teams ($29/seat/mo, paid tier), regulated SMBs (annual contracts, year two).
**Pain solved:** "I want an agent to do real work on my repo / DMs / Stripe — but I can't trust it not to delete my code or leak my keys, and I can't prove what it did."
**Why OpenClaw/Hermes do not solve it well:** Neither has hash-chained audit, signed skills, or a verifier loop. Hermes has shipped a critical write_file deletion bug (#20849). OpenClaw has shipped breaking releases and carries a tainted marketplace.
**MVP version (P0):** Verifier agent + approval-gate decorator + hash-chained audit UI + git-snapshot rollback + default-deny tools + signed skill manifests. Ships on existing Cato desktop.
**Full version (P1+P2):** Trace viewer + signed marketplace + per-action SpendIntent + A2A handoff via SwarmSync AP2.
**Revenue model:** OSS daemon free; "Cato Vouch Team" tier ($29/seat/mo) adds multi-user audit dashboards, off-host signed audit archival, SAML SSO, support SLAs.
**Risks:** (a) Buyers don't differentiate trust messaging from generic "self-hosted." (b) OpenClaw/Hermes ship something similar within 90 days. (c) Verifier agent latency makes UX feel slow.
**Verdict: Build.**

## 2. Cato Forge — CLI-Fan-Out as a Service

**Why this is strong:** Cato's warm Claude+Codex+Gemini+Cursor pool with degraded-response fallbacks is unique. Wraps as a single API for any other agent framework.
**Who pays for it:** Other agent frameworks (LangGraph, OpenHands), and dev teams who want "best of all CLIs" without managing four installs.
**Pain solved:** Coding-CLI quality varies by task; running all four and consensus-voting is laborious.
**Why OpenClaw/Hermes don't solve it well:** Neither has the warm pool primitive.
**MVP version:** Extract `cato/orchestrator/cli_process_pool.py` + `cli_invoker.py` as a standalone Python package with FastAPI wrapper.
**Full version:** Hosted SaaS that runs the CLIs in containerized workers.
**Revenue model:** Usage-priced API (per-fan-out, per-CLI-second).
**Risks:** Anthropic/OpenAI move against terminal CLI relays; CLIs are nested-execution-blocked.
**Verdict: Delay** — extract the package in P1 of Opportunity 1; reassess in 6 months based on telemetry.

## 3. Cato Bridge — Verified Telegram-to-Repo Bot

**Why this is strong:** Cato already has the Telegram bridge working (`cato_telegram_bridge.py` with claudeoneshot_bot). Narrow productization: "/repo /fix /test /merge" Telegram bot with verifier loop.
**Who pays for it:** Solo devs who want to ship from their phone.
**Pain solved:** Coding from a phone is impossible today.
**Why OpenClaw/Hermes don't solve it well:** Both can route Telegram → agent, but neither has the verifier + approval pattern needed to make phone-shipping safe.
**MVP version:** Polish existing bridge; add approval-gate decorator for write/exec; add `/audit` and `/rollback` commands.
**Full version:** SaaS hosted version with GitHub App OAuth.
**Revenue model:** $9/mo prosumer; $29/mo team.
**Risks:** Niche audience; cannibalizes Opportunity 1's positioning.
**Verdict: Delay** — fold the bridge polish into Opportunity 1's Telegram adapter work.

---

# Recommended Product Direction → **Cato Vouch**

## 1. Product Direction

**5 candidate names:**
1. **Cato Vouch** — *recommended*; pairs with Cato brand; "vouch" telegraphs verification
2. Cato Ledger — too crypto-y
3. Cato Chain — already loaded with crypto meaning
4. Cato Witness — strong but generic
5. ProvenCat — too cute

**Chosen name:** **Cato Vouch**
**One-sentence description:** The self-hosted AI agent daemon you can show your auditor — every action signed, chained, verified, and reversible.
**Hero headline:** *Never lose a line of code to an AI again.*
**Subheadline:** *Cato Vouch wraps every agent action in a signed, hash-chained audit trail with verifier review and one-tap human approval. Self-hosted. Windows-native. Open-source core.*
**Target users:** Solo developers and 2–10 person dev teams running AI coding agents on real repos.
**Core promise:** Every destructive action is verified before it happens, signed when it does, and reversible after.
**Why this wins:** Trust infrastructure is a category nobody else is building; Cato has the structural pieces already; the wedge is visible in a single screenshot of the audit UI.

## 2. MVP Scope

| Feature | Priority | User Story | Acceptance Criteria |
|---|---|---|---|
| Hash-chained audit log covering 100% of tool calls | P0 | As a dev, I can replay every action my agent took in the last 30 days | `AuditLog.append()` called on every `@tool`-decorated function; hash chain integrity verifiable via `cato audit verify` |
| `@requires_approval` decorator with Telegram round-trip | P0 | As a dev, I get a Telegram with approve/reject buttons before my agent merges a PR | Approval request appears in Telegram <2s; reject blocks action; timeout defaults reject |
| Verifier agent loop on destructive actions | P0 | As a dev, my agent's `write_file` is checked by a second model before it runs | Second SwarmSync call w/ `role=verifier`; returns `Verdict`; failed verdict blocks action unless approval override |
| Git-snapshot rollback for every destructive write | P0 | As a dev, I can `cato rollback <action_id>` and get my code back | `with snapshot_repo()` wraps write/delete/shell-exec when cwd is a git repo; snapshot ref stored in audit row |
| Default-deny tool policy | P0 | As a dev, my workspace only runs tools I've explicitly allowed | `workspace.yaml: allowed_tools: [...]`; unlisted tools 403 |
| Signed skill manifests | P0 | As a dev, my workspace refuses unsigned community skills | `Manifest(hash, sig, signer_pubkey)`; loader checks signature against trusted keys |
| Audit-log viewer in desktop (`AuditView.tsx`) | P0 | As a dev, I see a timeline of every action with diff, sig, verdict, cost | New screen; filter by date/actor/type; export JSON |
| Per-action SpendIntent with hard caps | P0 | As a dev, my agent stops when it hits my $10/day budget | `@costs(estimate_usd=...)` on every paid action; gateway blocks at cap |
| Refuse "...rest unchanged..." placeholder writes | P0 | My agent never deletes my code by writing a comment | `file.write_text` rejects content matching known truncation sentinels |
| Run-trace viewer (timeline UI) | P1 | I can see the whole agent run as a horizontal timeline | New `TraceView.tsx`; data from audit log |
| Signed skill registry (HTTP discovery) | P1 | I can browse signed community skills | `/api/registry` endpoint; SkillStore UI |
| `cato audit export --pdf` for compliance reports | P1 | I can hand my auditor a signed PDF of last quarter's actions | CLI command; renders audit chain + signatures |
| MCP tool adoption | P1 | I can use any MCP server as a Cato tool | Extend `cato/mcp/runtime.py` to load MCP servers from workspace.yaml |
| `/audit` and `/rollback` Telegram commands | P1 | I can audit and rollback from my phone | Telegram bridge extension |
| A2A handoff via SwarmSync AP2 | P2 | My agent can subcontract to another team's agent with escrow | New `cato/a2a/` package |
| Hosted "Team Audit" SaaS tier | P2 | My team's audit logs are stored off-host with SSO | Separate Cato Vouch Cloud service |
| Web demo dashboard | P3 | Public visitors see the audit UI without installing | Static export of AuditView with sample data |

## 3. Architecture

### Layer overview

- **Frontend (Tauri/React):** existing `desktop/` extended with `AuditView`, `TraceView`, `ApprovalInbox`, `SkillStore`, `BudgetView`.
- **Backend gateway (aiohttp):** existing `cato/api/` extended with `/api/audit/*`, `/api/approvals/*`, `/api/verifier/*`, `/api/registry/*`.
- **Database:** existing SQLite (`cato/core/memory.py`) extended with new tables: `actions`, `approvals`, `verifications`, `spend_intents`, `snapshots`, `skill_manifests`, `audit_chain`.
- **Agent runtime:** existing `cato/agent_loop.py` extended to call `ActionGate.before(tool)` and `ActionGate.after(tool, result)` around every tool call.
- **Worker queue:** new `cato/workers/verifier.py` (async) — pulls verifier jobs, calls SwarmSync with `role=verifier`, writes verdict.
- **CLI bridge:** existing `cato/orchestrator/cli_process_pool.py` + `cli_invoker.py` — extend with audit hook.
- **Tool registry:** existing `cato/tools/*` — wrap every `@tool` decorator with audit + approval + spend gates.
- **Bot/channel adapter:** existing `cato/adapters/telegram.py` extended with inline keyboard approve/reject + `/audit` `/rollback` commands.
- **Memory layer:** existing `cato/core/memory.py` (SQLite + sentence-transformers) — add semantic search over audit log.
- **Workflow engine:** new `cato/workflow/` — declarative YAML workflow runner with step types, retry, approval, verifier gates.
- **Verification engine:** new `cato/verifier/` — `VerifierAgent.review(ActionIntent) -> Verdict`.
- **Audit logs:** existing `cato/audit/audit_log.py` — extend to cover 100% of tool calls; add Merkle-like chain verifier.
- **Permissions:** new `cato/permissions/` — workspace policy YAML parsing + RBAC for team tier.
- **Billing:** existing `cato/budget.py` extended to enforce per-action SpendIntent.
- **API:** REST surface in `cato/api/v1/`.
- **Webhooks:** new `cato/webhooks/` — outbound webhook on every audit event for team-tier customers.
- **Deployment:** existing `cato_svc_runner.py` + desktop sidecar; team tier ships Docker compose for off-host audit archival.

### Mermaid diagram

```mermaid
flowchart TB
  subgraph Channels
    TG[Telegram]
    DESK[Desktop UI]
    WS[Web Chat / WS 8081]
    CLI[Cato CLI]
  end

  subgraph Gateway[Cato Gateway :8080 / WS :8081]
    GW[Gateway Hub]
  end

  subgraph Core[Cato Agent Runtime]
    LOOP[agent_loop.py]
    GATE[ActionGate<br/>policy + approval + verifier + spend]
    AUDIT[(hash-chained<br/>audit_log)]
    SNAP[snapshot_repo<br/>git stash/branch]
    SS[SwarmSync<br/>LLM routing]
  end

  subgraph Verifier[Verifier Worker]
    VW[verifier.py]
    VD[Verdict store]
  end

  subgraph Tools
    FILE[file.py]
    SHELL[shell.exec]
    PY[python_executor]
    WB[browser]
    GH[github]
    MCP[mcp tools]
    CLIPOOL[CLI Pool<br/>Claude/Codex/Gemini/Cursor]
  end

  subgraph Vault
    V[(AES-256-GCM<br/>vault.enc)]
    K[Signing keypair]
  end

  subgraph DB[(SQLite + FTS5)]
    T1[actions]
    T2[approvals]
    T3[verifications]
    T4[spend_intents]
    T5[snapshots]
    T6[skill_manifests]
    T7[memories]
  end

  TG --> GW
  DESK --> GW
  WS --> GW
  CLI --> GW

  GW --> LOOP
  LOOP --> GATE
  GATE --> SS
  GATE --> AUDIT
  GATE --> SNAP
  GATE --> VW
  VW --> VD
  VD --> GATE

  GATE --> FILE
  GATE --> SHELL
  GATE --> PY
  GATE --> WB
  GATE --> GH
  GATE --> MCP
  GATE --> CLIPOOL

  AUDIT --> DB
  K --> AUDIT
  V --> GATE

  GATE -. approval needed .-> TG
  TG -. approve/reject .-> GATE
```

## 4. Database Schema

> All Postgres-syntax; SQLite-portable equivalent ships in `cato/db/migrations/`.

```sql
-- users (existing token store, extended)
CREATE TABLE users (
  id UUID PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  pubkey BYTEA NOT NULL,
  org_id UUID REFERENCES organizations(id),
  role TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','admin','member','viewer')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_users_org ON users(org_id);

-- organizations (team tier)
CREATE TABLE organizations (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  audit_pubkey BYTEA NOT NULL,                -- org-level signing key (for cross-user attestation)
  plan TEXT NOT NULL DEFAULT 'oss',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- agents (registered agent identities)
CREATE TABLE agents (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  workspace_dir TEXT NOT NULL,
  pubkey BYTEA NOT NULL,                       -- agent identity key for signing actions
  org_id UUID REFERENCES organizations(id),
  policy_yaml TEXT NOT NULL,                   -- allowed_tools, approval_rules, spend_caps
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_agents_org ON agents(org_id);

-- agent_sessions (live conversations)
CREATE TABLE agent_sessions (
  id UUID PRIMARY KEY,
  agent_id UUID NOT NULL REFERENCES agents(id),
  channel TEXT NOT NULL,                       -- telegram | desktop | web | cli
  channel_user_id TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ
);
CREATE INDEX idx_sessions_agent ON agent_sessions(agent_id, started_at DESC);

-- bot_channels (registered channel configs)
CREATE TABLE bot_channels (
  id UUID PRIMARY KEY,
  agent_id UUID NOT NULL REFERENCES agents(id),
  kind TEXT NOT NULL,                          -- telegram | discord | slack | web
  config_jsonb JSONB NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- tools (registry of available tools)
CREATE TABLE tools (
  name TEXT PRIMARY KEY,
  module_path TEXT NOT NULL,
  side_effect_class TEXT NOT NULL CHECK (side_effect_class IN ('read','write','exec','network','payment')),
  default_policy TEXT NOT NULL CHECK (default_policy IN ('allow','verify','approve','deny')),
  cost_estimator TEXT                          -- optional fn path returning estimate_usd
);

-- skills (workspace skills loaded into agent context)
CREATE TABLE skills (
  id UUID PRIMARY KEY,
  agent_id UUID REFERENCES agents(id),
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  manifest_id UUID REFERENCES skill_manifests(id),
  installed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (agent_id, name)
);

-- skill_manifests (signed)
CREATE TABLE skill_manifests (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  content_hash BYTEA NOT NULL,                 -- sha256 of skill bundle
  signature BYTEA NOT NULL,
  signer_pubkey BYTEA NOT NULL,
  signer_label TEXT,                           -- "cato-official", or user-imported trust label
  source_url TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (name, version)
);
CREATE INDEX idx_manifests_signer ON skill_manifests(signer_pubkey);

-- workflows (declarative YAML)
CREATE TABLE workflows (
  id UUID PRIMARY KEY,
  agent_id UUID NOT NULL REFERENCES agents(id),
  name TEXT NOT NULL,
  yaml_src TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (agent_id, name)
);

-- workflow_runs
CREATE TABLE workflow_runs (
  id UUID PRIMARY KEY,
  workflow_id UUID NOT NULL REFERENCES workflows(id),
  session_id UUID REFERENCES agent_sessions(id),
  status TEXT NOT NULL CHECK (status IN ('queued','running','awaiting_approval','succeeded','failed','rolled_back')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ,
  error_text TEXT
);
CREATE INDEX idx_runs_status ON workflow_runs(status, started_at DESC);

-- tasks (high-level task tracking)
CREATE TABLE tasks (
  id UUID PRIMARY KEY,
  run_id UUID REFERENCES workflow_runs(id),
  session_id UUID REFERENCES agent_sessions(id),
  description TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- task_steps (individual steps within a task)
CREATE TABLE task_steps (
  id UUID PRIMARY KEY,
  task_id UUID NOT NULL REFERENCES tasks(id),
  step_index INT NOT NULL,
  action_id UUID REFERENCES actions(id),
  status TEXT NOT NULL,
  UNIQUE (task_id, step_index)
);

-- actions (THE hash-chained ledger — every tool call)
CREATE TABLE actions (
  id UUID PRIMARY KEY,
  session_id UUID NOT NULL REFERENCES agent_sessions(id),
  agent_id UUID NOT NULL REFERENCES agents(id),
  tool_name TEXT NOT NULL REFERENCES tools(name),
  input_jsonb JSONB NOT NULL,
  output_jsonb JSONB,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','verified','failed','succeeded','rolled_back')),
  prev_hash BYTEA NOT NULL,                    -- hash of previous action in this agent's chain
  self_hash BYTEA NOT NULL,                    -- sha256(prev_hash || canonical(input,output,status,ts))
  agent_signature BYTEA NOT NULL,              -- signed by agent.pubkey
  cost_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
  approval_id UUID REFERENCES approvals(id),
  verification_id UUID REFERENCES verifications(id),
  snapshot_id UUID REFERENCES snapshots(id),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at TIMESTAMPTZ
);
CREATE INDEX idx_actions_session ON actions(session_id, started_at);
CREATE INDEX idx_actions_chain ON actions(agent_id, started_at);
CREATE UNIQUE INDEX uniq_action_chain ON actions(agent_id, self_hash);

-- approvals (human-in-the-loop gates)
CREATE TABLE approvals (
  id UUID PRIMARY KEY,
  action_id UUID NOT NULL REFERENCES actions(id),
  channel TEXT NOT NULL,                       -- telegram | desktop | email
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at TIMESTAMPTZ,
  decided_by UUID REFERENCES users(id),
  decision TEXT CHECK (decision IN ('approve','reject','timeout')),
  decision_signature BYTEA,
  timeout_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_approvals_pending ON approvals(decided_at) WHERE decided_at IS NULL;

-- verifications (verifier agent verdicts)
CREATE TABLE verifications (
  id UUID PRIMARY KEY,
  action_id UUID NOT NULL REFERENCES actions(id),
  verifier_model TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail','approve_required')),
  reasons_jsonb JSONB NOT NULL,
  evidence_jsonb JSONB NOT NULL,               -- diff, lint, test result, policy hits
  signature BYTEA NOT NULL,
  verified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- snapshots (git-stash refs for rollback)
CREATE TABLE snapshots (
  id UUID PRIMARY KEY,
  action_id UUID REFERENCES actions(id),
  repo_path TEXT NOT NULL,
  ref TEXT NOT NULL,                           -- git stash sha or shadow branch ref
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL              -- auto-prune after retention
);

-- spend_intents (per-action cost gating)
CREATE TABLE spend_intents (
  id UUID PRIMARY KEY,
  action_id UUID NOT NULL REFERENCES actions(id),
  estimate_usd NUMERIC(10,4) NOT NULL,
  actual_usd NUMERIC(10,4),
  hourly_cap_usd NUMERIC(10,4) NOT NULL,
  daily_cap_usd NUMERIC(10,4) NOT NULL,
  decided TEXT NOT NULL CHECK (decided IN ('within_cap','blocked'))
);

-- memories (existing — extended with audit semantic search)
CREATE TABLE memories (
  id UUID PRIMARY KEY,
  agent_id UUID NOT NULL REFERENCES agents(id),
  kind TEXT NOT NULL,                          -- user | feedback | project | reference
  content TEXT NOT NULL,
  embedding VECTOR(384),                       -- sentence-transformers all-MiniLM-L6-v2
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_memories_kind ON memories(agent_id, kind);

-- audit_logs (rolling cross-chain ledger — optional org-level archival)
CREATE TABLE audit_logs (
  id BIGSERIAL PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  action_id UUID NOT NULL REFERENCES actions(id),
  action_hash BYTEA NOT NULL,
  archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  archive_signature BYTEA NOT NULL
);

-- verification_reports (rollup per workflow_run)
CREATE TABLE verification_reports (
  id UUID PRIMARY KEY,
  run_id UUID NOT NULL REFERENCES workflow_runs(id),
  total_actions INT NOT NULL,
  passed INT NOT NULL,
  failed INT NOT NULL,
  approve_required INT NOT NULL,
  signed_report JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- agent_scores (reputation — feeds marketplace P2)
CREATE TABLE agent_scores (
  agent_id UUID PRIMARY KEY REFERENCES agents(id),
  completion_rate NUMERIC(5,4) NOT NULL DEFAULT 0,
  verification_pass_rate NUMERIC(5,4) NOT NULL DEFAULT 0,
  cost_efficiency_score NUMERIC(5,2) NOT NULL DEFAULT 0,
  speed_score NUMERIC(5,2) NOT NULL DEFAULT 0,
  safety_score NUMERIC(5,2) NOT NULL DEFAULT 0,
  user_rating NUMERIC(3,2),
  retry_rate NUMERIC(5,4) NOT NULL DEFAULT 0,
  dispute_rate NUMERIC(5,4) NOT NULL DEFAULT 0,
  tool_call_accuracy NUMERIC(5,4) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- cli_commands (CLI fan-out call records)
CREATE TABLE cli_commands (
  id UUID PRIMARY KEY,
  action_id UUID REFERENCES actions(id),
  cli TEXT NOT NULL CHECK (cli IN ('claude','codex','gemini','cursor')),
  cmd_jsonb JSONB NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- command_results
CREATE TABLE command_results (
  id UUID PRIMARY KEY,
  command_id UUID NOT NULL REFERENCES cli_commands(id),
  exit_code INT,
  stdout TEXT,
  stderr TEXT,
  duration_ms INT,
  degraded BOOLEAN NOT NULL DEFAULT false,
  confidence NUMERIC(3,2) NOT NULL DEFAULT 1.0
);

-- webhooks (outbound audit events)
CREATE TABLE webhooks (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  url TEXT NOT NULL,
  events TEXT[] NOT NULL,                      -- ['action.completed','approval.requested',...]
  secret BYTEA NOT NULL,                       -- HMAC key
  enabled BOOLEAN NOT NULL DEFAULT true
);

-- billing_events (team tier)
CREATE TABLE billing_events (
  id UUID PRIMARY KEY,
  org_id UUID NOT NULL REFERENCES organizations(id),
  kind TEXT NOT NULL,                          -- seat | usage | audit_archive_gb
  amount_usd NUMERIC(10,4) NOT NULL,
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL
);

-- wallets + transactions (P2 marketplace)
CREATE TABLE wallets (
  id UUID PRIMARY KEY,
  owner_kind TEXT NOT NULL CHECK (owner_kind IN ('user','agent','org')),
  owner_id UUID NOT NULL,
  balance_usd NUMERIC(12,4) NOT NULL DEFAULT 0
);
CREATE TABLE transactions (
  id UUID PRIMARY KEY,
  from_wallet UUID NOT NULL REFERENCES wallets(id),
  to_wallet UUID NOT NULL REFERENCES wallets(id),
  amount_usd NUMERIC(12,4) NOT NULL,
  kind TEXT NOT NULL,                          -- escrow_hold | escrow_release | fee | refund
  ref_action_id UUID REFERENCES actions(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- marketplace_listings (P2)
CREATE TABLE marketplace_listings (
  id UUID PRIMARY KEY,
  agent_id UUID NOT NULL REFERENCES agents(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  price_model TEXT NOT NULL,
  price_usd NUMERIC(10,4) NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 5. API Spec

All endpoints under `/api/v1/`. Authentication: `Authorization: Bearer <token>` for desktop/CLI, HMAC signature for webhooks, agent keypair for agent-to-gateway.

### Auth
- `POST /auth/token` — body: `{email, password}`; resp: `{token, expires_at, user}`; 401 invalid; 429 rate-limited.
- `POST /auth/agent/register` — body: `{name, workspace_dir, pubkey, policy_yaml}`; resp: `{agent_id}`; 409 name exists.

### Agent registration
- `GET /agents` — list; resp: `{agents: [...]}`.
- `PATCH /agents/{id}/policy` — body: `{policy_yaml}`; validates against schema.

### Bot channels
- `POST /channels` — body: `{agent_id, kind, config}`; resp: `{channel_id}`; 400 invalid config for kind.
- `DELETE /channels/{id}` — disables.

### Sessions
- `POST /sessions` — body: `{agent_id, channel}`; resp: `{session_id, ws_url}`.
- `GET /sessions/{id}/events` — SSE stream of actions/approvals/verifications.

### Tasks
- `POST /tasks` — body: `{session_id, description}`; resp: `{task_id}`.
- `GET /tasks/{id}` — full task tree with steps.

### Workflows
- `POST /workflows` — body: `{agent_id, name, yaml_src}`; validates YAML; 400 schema error.
- `POST /workflows/{id}/runs` — starts run; resp: `{run_id}`.
- `GET /workflows/{id}/runs/{run_id}` — status + verification report.

### Tool calls (internal — used by agent runtime)
- `POST /actions` — body: `{session_id, tool_name, input}`; gateway pipes through ActionGate; resp: `{action_id, status, output|approval_id|verification_id|spend_blocked}`.
- `GET /actions/{id}` — full row.
- `POST /actions/{id}/rollback` — invokes snapshot; resp: `{snapshot_ref, rolled_back: true}`.

### CLI commands
- `POST /cli/run` — body: `{prompt, fan_out: ["claude","codex","gemini","cursor"], timeout_ms}`; resp: streaming SSE of per-CLI results with confidence.

### Approvals
- `GET /approvals/pending` — for current user.
- `POST /approvals/{id}` — body: `{decision: "approve"|"reject", signature}`.
- Telegram webhook: inline keyboard callbacks routed to this endpoint.

### Verification reports
- `GET /reports/runs/{run_id}` — signed report; resp: PDF or JSON via `Accept` header.

### Memory search
- `GET /memory/search?q=...&kind=...` — semantic; resp: ranked memories with citations.

### Audit logs
- `GET /audit/stream` — SSE of all actions for current agent.
- `GET /audit/verify` — verifies chain integrity; resp: `{ok: bool, broken_at: action_id|null}`.
- `GET /audit/export?format=json|pdf&since=&until=` — signed archive download.

### Marketplace (P2)
- `GET /marketplace/listings` — list.
- `POST /marketplace/hire` — body: `{listing_id, task}`; opens escrow.

### Billing
- `GET /billing/usage` — current period usage + projection.

### Webhooks
- `POST /webhooks` — register; body: `{url, events, secret}`.
- All outbound posts include `X-Cato-Signature: hmac-sha256(secret, body)`.

## 6. Workflow Engine

### Workflow YAML

```yaml
name: telegram_repo_fix
trigger:
  channel: telegram
  command: /fix
inputs:
  repo_url: { type: string, required: true }
  issue_id: { type: string, required: true }
steps:
  - id: clone
    tool: shell.exec
    args: { cmd: "git clone {{ inputs.repo_url }} {{ workspace }}/work" }
    timeout_ms: 60000

  - id: diagnose
    tool: cli.fan_out
    args:
      prompt: "Read issue {{ inputs.issue_id }} and propose a fix"
      fan_out: [claude, codex, gemini, cursor]
    output_schema:
      type: object
      required: [proposed_patch, confidence]

  - id: verify_proposal
    type: verify
    target: diagnose
    verifier_prompt: "Does this patch address the issue without introducing regressions?"

  - id: human_gate
    type: approval
    channel: telegram
    timeout: 10m
    summary: "About to apply patch:\n{{ steps.diagnose.output.proposed_patch | truncate(500) }}"

  - id: apply
    tool: file.apply_patch
    args: { patch: "{{ steps.diagnose.output.proposed_patch }}" }
    on_failure: rollback

  - id: test
    tool: shell.exec
    args: { cmd: "pytest" }
    retry: { max: 2, backoff_ms: 5000 }

  - id: verify_tests
    type: verify
    target: test
    verifier_prompt: "Did all critical tests pass?"

  - id: report
    tool: telegram.send
    args:
      to: "{{ session.channel_user_id }}"
      template: "Fix applied. Audit: {{ audit_url }} | Trace: {{ trace_url }}"
parallel: false
on_any_failure:
  - rollback_all
  - notify_user
```

### Step types
- `tool` — direct tool invocation
- `approval` — human-in-the-loop gate
- `verify` — verifier agent review
- `parallel` — fan out to N children, gather results
- `loop` — repeat until condition

### Engine rules
- Agent assignment: each step inherits the workflow's agent unless `agent: name` set.
- Retries: per-step `retry: {max, backoff_ms}`; failed retries logged but workflow continues unless `on_failure: stop|rollback`.
- Failure handling: `on_failure` in [`continue`,`stop`,`rollback`,`approval`].
- Approval gates: persist `approval_id`; resumed on decision via webhook.
- Parallel execution: ThreadPoolExecutor up to `gateway.max_parallel` (default 8).
- Timeouts: every step has `timeout_ms` (default 60000); on timeout = failure.
- Logs: every step writes to `actions` table.
- Rollbacks: `rollback_all` walks snapshots in reverse order.
- Verification steps: write to `verifications`, fail closed.
- Output schema enforcement: JSONSchema validation; failure routes to failure handler.

## 7. CLI Bridge

### Allowlist + permissions

```yaml
cli_bridge:
  claude:
    binary: claude
    allowed_subcommands: ["-p", "code", "review"]
    default_timeout_ms: 60000
    sandbox: warm_pool
  codex:
    binary: codex
    allowed_subcommands: ["--full-auto", "-q", "exec"]
    default_timeout_ms: 60000
    sandbox: warm_pool
  gemini:
    binary: gemini
    allowed_subcommands: ["-p"]
    default_timeout_ms: 60000
    sandbox: subprocess
    notes: "hangs on Windows in non-interactive; use cursor instead where possible"
  cursor:
    binary: cursor-agent
    allowed_subcommands: ["-p"]
    default_timeout_ms: 60000
    sandbox: subprocess
  gh:
    binary: gh
    allowed_subcommands: ["pr", "issue", "workflow", "run"]
    default_timeout_ms: 30000
    sandbox: subprocess
  npm:
    binary: npm
    allowed_subcommands: ["install","run","test","ci"]
    default_timeout_ms: 300000
    sandbox: subprocess
  pnpm:
    binary: pnpm
    allowed_subcommands: ["install","run","test"]
    default_timeout_ms: 300000
    sandbox: subprocess
  node:
    binary: node
    allowed_args: ["--version","scripts/*.mjs"]    # paths anchored to cwd
    default_timeout_ms: 60000
    sandbox: subprocess
  python:
    binary: python
    allowed_args: ["-m pytest", "scripts/*.py"]
    default_timeout_ms: 300000
    sandbox: subprocess
  pytest:
    binary: pytest
    allowed_args: ["tests/", "-x", "-q", "--maxfail=*"]
    default_timeout_ms: 600000
    sandbox: subprocess
  playwright:
    binary: playwright
    allowed_subcommands: ["test","install"]
    default_timeout_ms: 600000
    sandbox: subprocess

permissions:
  - working_directory_must_be_under: ["{{ workspace_dir }}"]
  - env_secrets: from_vault_only       # never from process env
  - streaming_output: true
  - audit_logging: mandatory
  - retries: max 1 unless step opts in
  - sandbox_recommendation: docker for untrusted workspaces; warm_pool for Cato-owned dirs
  - error_classification:
      timeout:     transient
      exit_nonzero_with_stderr_match("ECONNRESET|ETIMEDOUT"): transient
      exit_nonzero_with_stderr_match("permission denied|EACCES"): security
      exit_nonzero_other: failure
```

## 8. Bot/Channel Adapters

### Routing
Inbound: channel adapter → `gateway.ingest(channel, channel_user_id, payload)` → identity map to `agent_session` → `agent_loop.handle()`.
Outbound: `gateway.send(session_id, payload)` → adapter.send + WS broadcast to desktop.

### Adapters
- **Telegram** (existing, extend): long-polling; inline keyboards for approvals; commands listed below; file uploads → `workspace/inbox/`.
- **Discord** (P1): bot token; slash commands mirror Telegram.
- **Slack** (P2): Socket Mode; Block Kit for approvals.
- **Web chat** (existing): WS 8081 to gateway; auth via session cookie.
- **CLI**: `cato run "<task>"`; streams events to terminal.
- **Desktop UI**: existing; gains `ApprovalInbox.tsx` and `AuditView.tsx`.

### Identity mapping
`channel_users(id, channel, channel_user_id, user_id)` — first DM creates mapping after a `/pair` flow.

### Session continuity
Cross-channel: same `agent_id` + same `user_id` → same conversation context (memory shared); new `agent_session` row per channel switch with `prev_session_id` link.

### Notifications
Long-running tasks emit `task.progress` every 30s to the channel that started the task + the desktop UI.

### Standard command syntax
```
/run <task>             — start a task
/approve <action_id>    — approve a pending action
/reject <action_id>     — reject
/status                 — current task tree
/logs <task_id>         — last 20 audit rows
/audit                  — link to AuditView
/rollback <action_id>   — invoke snapshot rollback
/hire <listing_id>      — P2 marketplace
/budget                 — show today's spend
/skills                 — list installed signed skills
/skills install <name>  — install signed skill from registry
```

### Error handling
On adapter failure: write to `audit` with `status=failed`; retry transient errors with backoff; surface to desktop notification.

## 9. Verification Layer

### Components
- **VerifierAgent** — independent LLM call via SwarmSync with `role=verifier`, isolated context (no shared memory with the acting agent).
- **Evidence collectors** — diff (git), lint (ruff/eslint), tests (pytest), schema-validator (JSONSchema), policy hits (deny-list match).
- **Scoring** — composite (test_pass × diff_clean × policy_clean × verifier_pass) → `Verdict`.
- **Decision** — `pass` (write through), `fail` (block), `approve_required` (gate to human).
- **Signed record** — verifier signature on the verdict using a dedicated verifier keypair.

### Verification report JSON schema

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "CatoVouchVerificationReport",
  "type": "object",
  "required": ["report_id","action_id","verdict","reasons","evidence","signature","verifier","timestamp"],
  "properties": {
    "report_id": {"type": "string", "format": "uuid"},
    "action_id": {"type": "string", "format": "uuid"},
    "workflow_run_id": {"type": "string", "format": "uuid"},
    "verdict": {"enum": ["pass","fail","approve_required"]},
    "score": {"type": "number", "minimum": 0, "maximum": 1},
    "reasons": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["code","severity","message"],
        "properties": {
          "code": {"type": "string"},
          "severity": {"enum": ["info","warning","error","critical"]},
          "message": {"type": "string"},
          "location": {"type": "string"}
        }
      }
    },
    "evidence": {
      "type": "object",
      "properties": {
        "diff": {"type": "string"},
        "lint": {"type": "object"},
        "tests": {
          "type": "object",
          "properties": {
            "passed": {"type": "integer"},
            "failed": {"type": "integer"},
            "skipped": {"type": "integer"},
            "duration_ms": {"type": "integer"}
          }
        },
        "policy_hits": {"type": "array", "items": {"type": "string"}},
        "schema_validation": {"type": "object"}
      }
    },
    "verifier": {
      "type": "object",
      "required": ["model","pubkey"],
      "properties": {
        "model": {"type": "string"},
        "pubkey": {"type": "string"}
      }
    },
    "signature": {"type": "string"},
    "timestamp": {"type": "string", "format": "date-time"}
  }
}
```

## 10. Agent Reputation

### Score components (each normalized 0–1)
- `completion_rate` = succeeded / attempted
- `verification_pass_rate` = verifier_pass / total_verifications
- `cost_efficiency_score` = 1 − (avg_actual_cost / avg_estimate_cost) clipped to [0,1]
- `speed_score` = 1 − (avg_duration / category_p90_duration)
- `safety_score` = 1 − (critical_policy_hits + rolled_back_actions) / total_actions
- `user_rating` = mean of explicit 1–5 ratings, scaled
- `retry_rate` = retries / actions (penalty)
- `dispute_rate` = disputes / paid_jobs (penalty, P2 only)
- `tool_call_accuracy` = (tool_calls_that_returned_valid_output) / total_tool_calls

### Composite

```
reputation = (
    0.20 * completion_rate +
    0.20 * verification_pass_rate +
    0.10 * cost_efficiency_score +
    0.10 * speed_score +
    0.20 * safety_score +
    0.10 * user_rating +
    0.10 * tool_call_accuracy
) - (0.05 * retry_rate + 0.10 * dispute_rate)
```

### Update rule
After each completed task, EMA with α=0.1:
```
score_new = 0.9 * score_old + 0.1 * task_score
```
Surfaced in `agent_scores` row; visible on marketplace listing P2.

## 11. Marketplace Layer (P2)

### Lifecycle
1. Buyer posts a Request-for-Task (RFT) via `/marketplace/rft`.
2. System scores listings against RFT (capability match × reputation × price).
3. Buyer selects an agent; funds held in escrow.
4. Agent runs the task; verifier checks output.
5. On verifier pass → funds release minus 10% Cato fee.
6. On verifier fail or buyer dispute → mediator agent reviews; refund per policy.
7. Audit chain persists for both sides.
8. Reputation updates per §10.

### Listings
`marketplace_listings`: title, description, price_model (`per_task`,`per_hour`,`per_token`), reputation snapshot, badges (`verified_publisher`,`signed_skills_only`,`fast_response`), success metrics shown.

### Hiring flow
`POST /marketplace/hire` opens escrow via SwarmSync AP2 protocol; returns `escrow_ref`; agent receives task with payment context.

### Refunds/disputes
`POST /marketplace/dispute` triggers mediator panel (verifier agent + neutral org); decision recorded with signed report.

### Marketplace fees
10% flat MVP; sliding scale for high-volume publishers.

### Developer onboarding
`cato vouch publish ./my-agent.yaml` packages, signs, uploads listing.

## 12. Security & Governance

- **Auth:** Argon2id password hashing; refresh tokens (15-min access, 30-day refresh); MFA optional via TOTP.
- **API keys:** scoped tokens (`audit:read`, `actions:write`); displayed once, hashed at rest.
- **OAuth:** P2 — GitHub for repo access, Telegram via deep-link pairing.
- **RBAC:** roles `owner|admin|member|viewer`; team tier; policy enforcement at gateway.
- **Agent permissions:** `policy_yaml` per agent declares `allowed_tools`, `approval_rules`, `spend_caps`, `workspace_paths`.
- **Tool permissions:** every tool declares `side_effect_class`; gateway maps to default policy; workspace can override.
- **Secrets handling:** vault is sole source; CLI bridge injects via env at invocation; never written to disk; redacted from logs (regex + entropy detection).
- **Audit logs:** hash-chained per agent; org archive optional; chain verification CLI command.
- **Rate limits:** per-token sliding window (default 60 req/min for `actions:write`).
- **Abuse prevention:** workspace_dir confinement; path traversal blocked at `cato/safety.py`; shell.exec arg list (no shell=True); refuses truncation-placeholder writes.
- **Sandboxing:** Docker (preferred for untrusted workspaces); subprocess with restricted env for warm pool; `python_executor` uses RestrictedPython subset.
- **Approval gates:** declarative in `policy_yaml`; defaults applied per tool side-effect class.
- **Data retention:** audit rows kept indefinitely OSS; team tier configurable (default 1 year hot, infinite cold archive).
- **Workspace isolation:** one workspace per agent; cross-agent reads require explicit `share:` in workspace.yaml.
- **Org boundaries:** `org_id` foreign key on every multi-tenant table; RLS-style filter at gateway.
- **Admin controls:** org owner can rotate keys, revoke tokens, force-archive, view all audit logs.

## 13. Frontend UX

Existing screens preserved; new screens added in `desktop/src/views/`.

| # | Screen | Purpose | Primary action | Components | States |
|---|---|---|---|---|---|
| 1 | Dashboard (exists) | At-a-glance system status | Start a task | ActivityIndicator, RecentRuns, OpenApprovals | empty: "no runs yet"; loading: skeleton; error: connection lost; success: 3 widgets |
| 2 | AgentRegistry (new) | Manage registered agents | Register new agent | AgentList, PolicyEditor, KeyManager | empty CTA, validation errors |
| 3 | WorkflowBuilder (new) | Visual YAML editor | Save workflow | YamlEditor, StepPalette, ValidatePanel | invalid YAML banner |
| 4 | TaskRunDetail (new) | Drill into one run | Approve / rollback | TaskTree, StepDetails, EvidencePanel | live updating |
| 5 | LiveExecConsole (new) | Streaming view of agent | Stop | EventStream, ToolCallCards, ApprovalPrompt | reconnecting state |
| 6 | ApprovalInbox (new) | Pending human gates | Approve / Reject | ApprovalCard with diff/cost/why | empty zero-state |
| 7 | VerificationReport (new) | Signed report | Export PDF | ReasonsTable, EvidenceTabs, Signature | failed-verdict banner |
| 8 | AuditView (new) | Timeline of every action | Search/filter/export | TimelineList, ChainVerifyButton, ExportMenu | broken-chain alert |
| 9 | BotChannelSettings (existing, extend) | Configure channels | Test connection | TelegramTab, DiscordTab, SlackTab | invalid token |
| 10 | CLIBridgeSettings (new) | Configure CLI allowlists | Save | CLIList, AllowlistEditor, SandboxToggle | binary-not-found warning |
| 11 | Marketplace (P2) | Browse listings | Hire | ListingGrid, Filters, ReputationBadges | empty search |
| 12 | Billing (P1 team tier) | Usage + invoices | Update payment | UsageChart, Invoices, PlanSelector | overage warning |
| 13 | OrgSettings (P1 team tier) | Members + RBAC | Invite | MemberTable, RoleEditor, KeyRotation | invite expired |
| 14 | SkillStore (P1) | Signed skill browser | Install | SkillCards, SignerVerified, InstalledBadge | unsigned-warning |
| 15 | BudgetView (new) | Today / month spend | Set caps | SpendChart, CapEditor, AlertSettings | over-cap blocked |

## 14. Implementation Roadmap

### Phase 0 — Research Validation (3 days)
- **Goal:** Confirm research's controversial numbers; identify 1+ public Hermes/OpenClaw incident to use in launch.
- **Tasks:** verify Hermes #20849 and #7826 directly; gh-api repo stats for both; choose launch incident.
- **Files:** new `docs/research/verification.md`.
- **Dependencies:** none.
- **Tests:** human review.
- **Acceptance:** signed-off launch-incident chosen; numbers double-checked.
- **Risks:** can't find usable incident → delay launch messaging only.

### Phase 1 — Core Backend (5 days)
- **Goal:** ActionGate primitive wrapping every tool call; extend hash-chained audit to 100% coverage.
- **Tasks:**
  - new `cato/gate/action_gate.py` with `before/after` hooks
  - extend `cato/audit/audit_log.py` to record `actions` rows with prev_hash/self_hash
  - extend `cato/tools/__init__.py` so every `@tool` registers `side_effect_class`
  - migrations for `actions`, `approvals`, `verifications`, `snapshots`, `spend_intents`
- **Files:** `cato/gate/`, `cato/audit/`, `cato/tools/`, `cato/db/migrations/0001_vouch.sql`
- **Dependencies:** Phase 0
- **Tests:** unit tests for chain integrity; integration test: 100 actions with deliberate break → `audit verify` catches
- **Acceptance:** 100% of existing tools wrapped; chain verifier CLI passes on fresh DB
- **Risks:** existing tool decorators inconsistent — needs codemod

### Phase 2 — Verifier Loop + Snapshot Rollback (5 days)
- **Goal:** Independent verifier agent; git-snapshot before destructive writes; refusal of truncation sentinels.
- **Tasks:**
  - new `cato/verifier/` package; SwarmSync `role=verifier`
  - new `cato/snapshot/` with `snapshot_repo()` context manager
  - extend `cato/tools/file.py` to refuse truncation patterns
  - wire `ActionGate` to call verifier on `side_effect_class in {write,exec,payment}`
- **Files:** `cato/verifier/agent.py`, `cato/snapshot/git_snap.py`, `cato/tools/file.py`
- **Dependencies:** Phase 1
- **Tests:** Hermes #20849 reproduction test (write_file with placeholder → rejected); rollback round-trip test
- **Acceptance:** Verifier latency <2s p95 on standard write; rollback restores byte-identical file
- **Risks:** verifier latency budget breached → fall back to async verify with eventual rollback

### Phase 3 — CLI Bridge Hardening (3 days)
- **Goal:** Allowlist, secrets-from-vault-only, audit logging, error classification.
- **Tasks:**
  - extend `cato/orchestrator/cli_invoker.py` with allowlist check
  - extend warm pool to record `cli_commands` + `command_results`
  - move env-secret injection through vault
- **Files:** `cato/orchestrator/cli_invoker.py`, `cato/orchestrator/cli_process_pool.py`
- **Dependencies:** Phase 1
- **Tests:** integration tests for each CLI (claude, codex, gemini, cursor, gh, npm, pytest)
- **Acceptance:** all 4 CLIs invoked from a test workflow with audit records
- **Risks:** Gemini Windows hang persists — documented limitation, not blocker

### Phase 4 — Approval-Gate Adapters (4 days)
- **Goal:** `@requires_approval` decorator + Telegram inline keyboards + Desktop ApprovalInbox.
- **Tasks:**
  - new `cato/approvals/manager.py`
  - extend `cato/adapters/telegram.py` with inline approve/reject
  - new `desktop/src/views/ApprovalInboxView.tsx`
  - new `desktop/src/components/ApprovalCard.tsx`
- **Files:** `cato/approvals/`, `cato/adapters/telegram.py`, `desktop/src/...`
- **Dependencies:** Phase 1
- **Tests:** E2E: tool blocked → Telegram message → user taps Approve → action proceeds
- **Acceptance:** round-trip <2s; rejection denies and rolls back
- **Risks:** Telegram callback latency variable — show user a "decided" toast immediately

### Phase 5 — Default-Deny Policy + Signed Skill Manifests (4 days)
- **Goal:** `workspace.yaml` policy parsing; skill loader rejects unsigned by default.
- **Tasks:**
  - new `cato/permissions/policy.py`
  - new `cato/skills/manifest.py` with Ed25519 sign/verify
  - cato CLI: `cato skill sign`, `cato skill verify`
  - bootstrap "Cato Official" signer key bundled
- **Files:** `cato/permissions/`, `cato/skills/`, `cato/cli.py`
- **Dependencies:** Phase 1
- **Tests:** unsigned skill → load rejected; signed-by-untrusted → load rejected; signed-by-trusted → loaded
- **Acceptance:** OSS install ships with default deny + 5 pre-signed core skills
- **Risks:** existing users' skills not signed — provide one-time `cato skill trust-existing` migration

### Phase 6 — Per-action SpendIntent + BudgetView (3 days)
- **Goal:** Hard spend caps per agent and per workspace.
- **Tasks:**
  - extend `cato/budget.py`
  - `@costs(estimate_usd=...)` decorator
  - `desktop/src/views/BudgetView.tsx`
- **Files:** `cato/budget.py`, `cato/cost/`, `desktop/src/views/BudgetView.tsx`
- **Dependencies:** Phase 1
- **Tests:** action exceeds cap → blocked; estimate vs actual logged
- **Acceptance:** chart shows hourly/daily/monthly spend; CAP enforced
- **Risks:** cost estimates wrong → log actuals and self-calibrate

### Phase 7 — Audit UI + Trace Viewer (5 days)
- **Goal:** Desktop AuditView and TraceView; SSE stream.
- **Tasks:**
  - new `desktop/src/views/AuditView.tsx`
  - new `desktop/src/views/TraceView.tsx`
  - `/api/audit/stream` SSE endpoint
  - chain-integrity badge
- **Files:** `desktop/src/views/`, `cato/api/audit.py`
- **Dependencies:** Phase 1
- **Tests:** Playwright: 100 actions stream in <5s; export JSON validates against schema
- **Acceptance:** UI usable on 50k-row chain without freeze
- **Risks:** SQLite contention at 10k/hr — switch to WAL + batched commits

### Phase 8 — QA & Hardening (5 days)
- **Goal:** 1700+ tests staying green + new tests; security audit pass; performance budgets.
- **Tasks:**
  - run `failure-mode-auditor` against Cato Vouch flow
  - run `truth-audit` skill on end-to-end repo
  - Hudson + Kraken pass (per repo CLAUDE.md gate)
- **Files:** `tests/vouch/`
- **Dependencies:** Phases 1–7
- **Tests:** load test (10k actions/hour), chaos test (kill verifier worker mid-flight)
- **Acceptance:** all CLAUDE.md gate criteria met
- **Risks:** failing perf budget → optimize hot path or scope-cut

### Phase 9 — Launch (3 days)
- **Goal:** Public launch with cato-vouch.md landing, X thread, HN Show, docs site.
- **Tasks:**
  - landing page on existing Cato domain
  - HN Show post + X thread referencing Phase 0 incident
  - `cato vouch demo` one-command sandbox install
- **Dependencies:** Phase 8
- **Acceptance:** 100 demo installs in first 7 days; >10 paid waitlist signups
- **Risks:** launch falls flat → 30-day iteration; pivot messaging based on telemetry

## 15. File Structure

```
cato/                              # existing — extended
  __init__.py
  cli.py                           # +cato vouch subcommands
  agent_loop.py                    # +ActionGate hooks
  gateway.py                       # +approval routing
  vault.py                         # +Ed25519 keypair management
  budget.py                        # +SpendIntent
  safety.py                        # +truncation-sentinel refusal
  audit/
    __init__.py
    audit_log.py                   # +actions table writer
    chain_verifier.py              # NEW
  gate/                            # NEW
    __init__.py
    action_gate.py
    policy_runtime.py
  verifier/                        # NEW
    __init__.py
    agent.py
    evidence.py
  snapshot/                        # NEW
    __init__.py
    git_snap.py
  approvals/                       # NEW
    __init__.py
    manager.py
    telegram_inline.py
  permissions/                     # NEW
    __init__.py
    policy.py
    rbac.py
  skills/
    manifest.py                    # NEW
    loader.py                      # extend default-deny
  cost/                            # NEW
    __init__.py
    estimator.py
    spend_intent.py
  workflow/                        # NEW
    __init__.py
    engine.py
    yaml_loader.py
    step_runner.py
  marketplace/                     # NEW (P2)
    __init__.py
    listings.py
    escrow.py
    reputation.py
  webhooks/                        # NEW
    __init__.py
    dispatcher.py
  api/
    v1/                            # NEW namespace
      __init__.py
      auth.py
      agents.py
      sessions.py
      actions.py
      approvals.py
      audit.py
      workflows.py
      verifications.py
      marketplace.py
      billing.py
      webhooks.py
  adapters/
    telegram.py                    # extend
    discord.py                     # NEW (P1)
    slack.py                       # NEW (P2)
  orchestrator/
    cli_invoker.py                 # extend + allowlist
    cli_process_pool.py            # extend + audit
  core/
    memory.py                      # +semantic audit search
    context_builder.py
  ui/
    server.py                      # +/api/v1 mount
    dashboard.html                 # legacy SPA preserved
  db/                              # NEW
    migrations/
      0001_vouch.sql

desktop/                           # existing — extended
  src/
    views/
      DashboardView.tsx
      ChatView.tsx
      CodingAgentView.tsx
      AgentRegistryView.tsx        # NEW
      WorkflowBuilderView.tsx      # NEW
      TaskRunDetailView.tsx        # NEW
      LiveExecConsoleView.tsx      # NEW
      ApprovalInboxView.tsx        # NEW
      VerificationReportView.tsx   # NEW
      AuditView.tsx                # NEW
      TraceView.tsx                # NEW
      BudgetView.tsx               # NEW
      SkillStoreView.tsx           # NEW (P1)
      MarketplaceView.tsx          # NEW (P2)
      BillingView.tsx              # NEW (P1)
      OrgSettingsView.tsx          # NEW (P1)
      BotChannelSettingsView.tsx
      CLIBridgeSettingsView.tsx    # NEW
    components/
      ApprovalCard.tsx             # NEW
      AuditTimeline.tsx            # NEW
      ChainVerifyBadge.tsx         # NEW
      DiffViewer.tsx               # NEW
      SignatureChip.tsx            # NEW
    hooks/
      useAuditStream.ts            # NEW
      useApprovals.ts              # NEW

packages/                          # NEW — extracted reusable packages
  cato-vouch-sdk/                  # extract path for the "SDK pivot" option
    __init__.py
    action_gate.py
    verifier.py
    snapshot.py
    audit_chain.py

tests/                             # existing — extended
  vouch/
    test_action_gate.py
    test_chain_integrity.py
    test_verifier_loop.py
    test_snapshot_rollback.py
    test_approval_roundtrip.py
    test_signed_skill_loader.py
    test_spend_intent.py
    test_workflow_engine.py
    test_audit_stream.py
    test_telegram_inline_approval.py
  e2e/
    test_telegram_repo_fix_flow.py
```

## 16. Testing Plan

- **Unit tests** — chain integrity, signature verify/reject, policy YAML parse, sentinel-write refusal, cost estimator.
- **Integration tests** — ActionGate end-to-end, verifier loop, snapshot rollback round-trip, approval timeout, skill manifest loader, workflow YAML runner.
- **E2E tests** — Playwright on desktop (audit timeline render, approval inbox flow); Telegram bot harness (`/run /approve /reject /audit /rollback`).
- **Bot command tests** — every command in §8 list against a mock Telegram server.
- **CLI execution tests** — fan-out claude+codex+cursor on a fixture repo; assert degraded responses logged with confidence.
- **Workflow retry tests** — flaky step retries; permanent failure routes to rollback.
- **Permission tests** — unsigned skill rejected; out-of-workspace path traversal refused; CLI binary not in allowlist refused.
- **Verification tests** — Hermes #20849 reproduction must fail closed; verifier signature validates with embedded pubkey.
- **Marketplace transaction tests** (P2) — escrow open/release/refund flow; dispute mediation.
- **Load tests** — 10k actions/hour sustained; audit-stream SSE keeps <500ms p99 latency.
- **Failure simulations** — kill verifier worker mid-flight; SQLite WAL conflict; vault corruption recovery.

---

# Instrument Panel

```
SESSION_ID:        ub-2026-05-17-cato-vouch
PROBLEM_STATEMENT: Analyze OpenClaw + Hermes bots; identify gaps; design a superior
                   product built on Cato's existing stack and deliver a build spec.

INTAKE_ANSWERS:
  success_metric:     Both — strategic verdict + Codex-handable build spec, no compromise
  primary_constraint: Must extend Cato's existing stack (no rebuild)
  decision_owner:     Project owner
  already_tried:      Cato runtime itself (daemon, vault, audit, CLI fan-out, Telegram bridge)
  failure_definition: Vague spec, or non-defensible wedge

RED_ZONES_DETECTED:
  - "First mover advantage" framing — partially flagged; OpenClaw and Hermes are entrenched
  - "Build it and they will come" risk — mitigated via incident-paired launch (Phase 0)
  - "Our users are different" exceptionalism — mitigated via telemetry-driven pivot in 90 days

CONSTRAINT_MAP:
  HARD: reuse Cato vault/audit/SwarmSync, Windows-native, port 8080/8081, Python 3.11+
  SOFT: Telegram-first, OSS license, MIT-compatible dependencies
  ASSUMPTION: SwarmSync stays available as LLM router; Anthropic/OpenAI CLIs remain installable
  OPEN_QUESTION: which buyer segment (solo vs team vs SMB) retains best — answered post-launch

SYNTHESIS:
  VERDICT: Build "Cato Vouch" — a verifiable, audited, approval-gated extension of the
           existing Cato daemon. Wedge is trust infrastructure (hash-chained audit + verifier
           agent + signed skills + git-snapshot rollback + per-action SpendIntent),
           positioned against OpenClaw's release QA gaps and Hermes's destructive-write
           bug. MVP buildable in 4-6 weeks of focused work on the existing repo.
  INSIGHTS:
    - Trust infra is greenfield against OpenClaw and Hermes      | CONFIDENCE: HIGH    | FALSIFIABLE_IF: either competitor ships hash-chained audit within 90 days
    - Cato already owns the structural pieces                    | CONFIDENCE: HIGH    | FALSIFIABLE_IF: integration audit reveals audit_log covers <50% of tool calls
    - Verifier latency must stay <2s p95 or UX fails             | CONFIDENCE: MEDIUM  | FALSIFIABLE_IF: benchmark shows SwarmSync verifier round-trip >3s consistently
    - Launch must be paired with a public OpenClaw/Hermes incident| CONFIDENCE: MEDIUM | FALSIFIABLE_IF: incident-free launch achieves >100 installs/week (overrules)
    - B2B compliance sale takes >90 days                          | CONFIDENCE: MEDIUM  | FALSIFIABLE_IF: first 50 paid installs close <60 days
    - Marketplace P2 is downstream, not MVP                       | CONFIDENCE: HIGH    | FALSIFIABLE_IF: pre-launch surveys show buyers demand marketplace day-1
    - Self-signed skill manifests are enough for MVP             | CONFIDENCE: MEDIUM  | FALSIFIABLE_IF: users want PKI/revocation in week-1 feedback
    - Reusing audit_log + SwarmSync composes cleanly             | ASSUMPTION         | VERIFY_BY: Phase 1 spike (5 days)

STRONGEST_COUNTERCASE:
  Don't build a daemon — extract Cato's verification, audit, and approval primitives as a
  framework-agnostic SDK that any agent (OpenClaw, Hermes, OpenHands, LangGraph) can adopt.
  Daemon market is zero-sum; SDK market is positive-sum. LangSmith won as observability for
  LangChain, not as a competing framework. The build spec is structured so the verification
  engine, audit log, and CLI bridge are extractable packages — if 6-month telemetry confirms
  the SDK opportunity is larger, the pivot is engineered-for.

MINIMUM_VIABLE_DISAGREEMENT:
  Does a 2-10-person dev team pay $29/seat/mo for verifiable audit + approval gates if the
  OSS core already gives them the underlying capability?

NEXT_ACTION:
  Phase 0 (3 days). Owner: project owner. Concretely:
  (1) gh-api stats for openclaw/openclaw and NousResearch/hermes-agent
  (2) verify Hermes #20849 (write_file deletion) and #7826 (security audit) directly
  (3) pick the launch-incident to anchor the Phase 9 message
  Decision rule: If Phase 0 cannot confirm a usable public incident AND cannot validate
  Hermes #20849 reproduces, defer launch messaging to "preventive" framing instead of
  "remember when" framing. Build spec is unchanged.

DISSENT_LOG:
  - AGENT: DarkMirror
    CLAIM: "I assign <40% probability that the audit-log UI alone is sufficient
            differentiation. If the UI is not visually obvious in a 5-second screenshot,
            the wedge will not survive a Twitter scroll."
    PROBABILITY_ASSIGNED: 40
    CONDITIONS: "Launch screenshots test below 30% engagement vs baseline Cato screenshots"
  - AGENT: Archaeologist
    CLAIM: "I assign <30% probability that B2B compliance sale closes in <90 days.
            MVP revenue almost certainly comes from prosumer/team-of-2, not enterprise."
    PROBABILITY_ASSIGNED: 30
    CONDITIONS: "First 50 paying installs are >5-person companies"
  - AGENT: socratic-mentor
    CLAIM: "I assign <50% probability the framing is right. The real problem may be
            'why don't AI agents have version control for their own actions' — bigger,
            and would change the spec."
    PROBABILITY_ASSIGNED: 50
    CONDITIONS: "Usage telemetry shows AuditView is the most-opened screen"

AGENT_COMMITMENT_AUDIT:
  - AGENT: EpistemicAuditor       | ORIGINAL: trust-infra wedge | FINAL: same | SHIFTED: no
  - AGENT: Archaeologist          | ORIGINAL: tie launch to public failure | FINAL: same + dissent | SHIFTED: partial | JUSTIFICATION: Quantifier challenge forced specifying segments
  - AGENT: Quantifier             | ORIGINAL: latency budgets must be testable | FINAL: same + numeric acceptance criteria added | SHIFTED: no
  - AGENT: ConstraintCartographer | ORIGINAL: reuse-only HARD constraint | FINAL: same | SHIFTED: no
  - AGENT: socratic-mentor        | ORIGINAL: real problem is team trust | FINAL: same + filed dissent on framing | SHIFTED: no
  - AGENT: SoSpec                 | ORIGINAL: P0 = 4 features inc. signed bundles | FINAL: P0 keeps self-signed only, full PKI -> P1 | SHIFTED: yes | JUSTIFICATION: ConstraintCartographer scope-creep flag
  - AGENT: DarkMirror             | ORIGINAL: "show your auditor" hook | FINAL: same + dissent on UI-as-wedge | SHIFTED: no
  - AGENT: IdeaMatrix             | ORIGINAL: trust+CLI+channels wedge triangle | FINAL: same | SHIFTED: no
  - AGENT: RemixForge             | ORIGINAL: SCAMPER-Combine existing primitives | FINAL: same | SHIFTED: no
  - AGENT: SpiderSpark            | ORIGINAL: post-verify everything | FINAL: tiered — post-verify cheap, pre-gate irreversible | SHIFTED: yes | JUSTIFICATION: EpistemicAuditor flag on write_file irreversibility

CONFIDENCE_TOPOLOGY:
  Q1_SOLID_CONSENSUS:
    - Cato has the structural pieces; this is integration, not invention
    - OpenClaw + Hermes both lack hash-chained audit + verifier loop
    - MVP buildable in 4-6 weeks on existing stack
  Q4_DANGEROUS_CONSENSUS:
    - "Buyers will pay $29/seat for audit infra" — high agreement but evidence-thin; needs
      pre-launch validation via 10 customer interviews before pricing is locked
    - "Public incident-anchored launch will work" — high agreement among panel, but base
      rate for incident-anchored launches is unmeasured

SYNTHESIS_META:
  UNUSUALLY_COHERENT: false
  COHERENT_EDGE_CASE_OVERLAP: ~35%
  COHERENT_INTERPRETATION: "genuine session — 3 formal dissents filed, 1 meaningful think-revise revision"
  COHERENT_RECOMMENDED_ACTION: "proceed with Q4 mitigations (pre-launch interviews)"

CRUX:
  STATEMENT: Will a 2-10 person dev team pay $29/seat/month for verifiable audit +
             approval gates on top of an OSS daemon that already provides them?
  IF_TRUE:   Build Cato Vouch as specified. OSS core free; team tier paid; launch month 3.
  IF_FALSE:  Pivot to either (a) SDK extraction (verification middleware for OpenClaw/Hermes)
             or (b) prosumer-only positioning at $9/mo with team features de-scoped.
  EVIDENCE_THAT_CONFIRMS:  10/10 cold customer-discovery calls in weeks 1-2 say "I'd pay
                           for this today." Or: >10 paid waitlist signups in first 72h post-HN.
  EVIDENCE_THAT_DENIES:    <3/10 calls show willingness to pay. Or: <100 demo installs in
                           first 7 days post-launch.
```

---

# Narrative Brief

**Situation.** OpenClaw and Hermes Agent dominate the self-hosted multi-channel AI agent daemon category in 2026. Both ship breadth — 20+ messaging channels, skills systems, scheduled actions, model-provider freedom. Both fail in the same place: trust. Neither produces a hash-chained audit log. Neither runs a verifier agent on destructive actions. Hermes has shipped a critical write_file deletion bug. OpenClaw has shipped breaking releases and carries a community skills hub with no signing. Buyers who want AI agents to do real work — touch real repos, real Stripe keys, real customer DMs — currently have nowhere safe to go.

**Complication.** Cato sits in the middle of this gap with structural pieces nobody else has: an AES-256-GCM vault, a hash-chained audit log package, a warm CLI pool for Claude+Codex+Gemini+Cursor with degraded-response fallbacks, SwarmSync routing, and a working bidirectional Telegram bridge. The pieces are not wired into a single trust-layer product. The question is not whether to build trust infrastructure. It is whether the buyer (a 2-10 person dev team) will pay for it on top of an OSS core that already gives them most of the capability.

**Key insight.** This is not a feature-parity race against OpenClaw or Hermes. It is a category move. The wedge is verifiable agent work — every action signed, hash-chained, optionally human-gated, verified by an independent agent before destructive side-effects fire, reversible by git snapshot after. The four MVP primitives — ActionGate, Verifier loop, Approval-gate decorator, Signed skill manifests — compose existing Cato modules. Estimated 4-6 weeks of focused work on the existing repo. Launch is anchored to a public OpenClaw/Hermes incident to convert defensive curiosity into adoption.

**What is not yet known.** Three things. First, pricing. The Q4 Dangerous Consensus item is "buyers will pay $29/seat for audit infra" — high panel agreement, low evidence. Ten cold customer-discovery calls in weeks 1-2 will resolve this. Second, segment. Archaeologist's dissent — that B2B compliance closes in <90 days — is below 30%. Plan for prosumer-first revenue, B2B upside. Third, the framing risk. socratic-mentor's dissent — that the deeper problem is "agents need version control for their own actions" — would change the marketing surface but not the spec; telemetry on which screen users open first will tell us.

**Recommended action.** Execute Phase 0 (3 days) — verify Hermes Issue #20849 reproduces, confirm star/release stats, pick the launch incident. Then run the 9-phase roadmap to a public launch in 8-10 weeks. The build spec above is detailed enough to hand to Codex without further translation.

---

# Plain-English Explanation

Two products own this market right now. OpenClaw is the gateway-first one — it gets your AI onto every messaging app you use. Hermes Agent is the agent-first one — it writes its own skills as it learns. Both are good at what they do. Both are scary because neither will tell you what your AI actually did, neither will check its work before it destroys something, and neither will let you take it back.

Cato already has the parts to fix this. It can lock secrets in a vault. It can write a tamper-proof record of every action. It can run four different coding AIs in parallel and compare answers. It can talk to you on Telegram, on the desktop, and in a web chat at the same time. The parts just aren't wired together into a single product yet.

The proposal is to wire them together as **Cato Vouch**. Every action your agent takes goes through a checkpoint that records it, signs it, sometimes asks a second AI "is this safe?", sometimes pings your phone for approval, and always takes a git snapshot so you can roll it back. Free to use for solo developers. Paid tier ($29/seat/month) for teams who want shared audit logs and SSO.

The pitch fits on a screenshot: a timeline of everything your AI did today, each row signed, each one with a "rollback" button. The thing OpenClaw and Hermes users wish they had.

Eight to ten weeks of work on the existing Cato repo. Launch tied to a public incident in one of the competing products so the message lands as "remember when Hermes deleted your code?" instead of as "trust me, this is important."

---

# Final Recommendation

**Build Cato Vouch.** Start Phase 0 immediately (3 days). Run a 10-call customer discovery sprint in parallel during Phases 1–2 to resolve the pricing crux before Phase 5. Architect every new package (verifier, audit chain, action gate, snapshot, approval) as importable standalone so the SDK pivot remains available in 6 months if the daemon path tops out. Do not build the marketplace in MVP. Do not build payments/escrow in MVP. Do not chase channel-count parity with OpenClaw or Hermes. The wedge is trust, the moat is the audit chain accumulating per install, the GTM is incident-anchored launch through Cato's existing Telegram + X presence.

Confidence: **MEDIUM-HIGH** on the spec being correct; **MEDIUM** on commercial pull; **HIGH** on Cato being structurally able to build it. The single assumption that flips the recommendation is the pricing crux above — answer it before Phase 5.
