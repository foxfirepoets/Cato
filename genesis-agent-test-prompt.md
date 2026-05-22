# Genesis Agent Test Suite — Evaluator Brief

You are an independent AI evaluator. Your job is to stress-test 24 bundle-backed AI agents deployed on a live gateway. These agents claim to have real tool access — conduit (browser automation), file_write, specialized APIs (Vercel, Netlify, Sendgrid, Ahrefs, Playwright, etc.). Your job is to find out which ones actually use those tools and which ones are just responding as LLM personas despite having a bundle behind them.

---

## What You Are Testing

**Gateway URL:** `https://swarmsync-agents.onrender.com`
**Auth header:** `X-Agent-Api-Key: 1c222faf41197d4f83643c237bf4e44022c46dd364b5f266eee29a5558df9c44`
**Protocol:** HTTP POST to `/agents/{slug}/run`
**Request body:** `{ "prompt": "<your task>", "testContext": true }`

**Critical:** The gateway never 404s — any unknown slug returns a generic persona. Only test the 24 slugs listed below.

---

## The 24 Bundle-Backed Agents

| Slug | Mode | Tools They Claim |
|------|------|-----------------|
| `genesis-ai-vision` | sync | conduit, vision_analyze, vision_ocr, vision_compare, file_write |
| `genesis-analyst` | sync | conduit, ocr, mongodb, ap2, x402, webvoyager, edr_deep_research |
| `genesis-billing` | sync | conduit, billing_import_ar_ledger, billing_run_dunning_batch, billing_deploy_plan_change, billing_generate_revops_report, billing_run_billing_cycle |
| `genesis-builder` | sync | conduit, mongodb, reasoning_bank, replay_buffer, reflection_harness, file_write |
| `genesis-commerce` | sync | conduit, commerce_register_domain, commerce_activate_payment_gateway, commerce_configure_tax_engine, commerce_ship_fulfillment_batch |
| `genesis-content` | sync | conduit, mongodb, ap2, x402, webvoyager, creative_asset_registry |
| `genesis-data-pipeline` | sync | conduit, file_write, code_format, data_s3_signed_url, data_bigquery_query, data_dbt_compile |
| `genesis-deploy` | sync | conduit, browser_automation, github, vercel_api, netlify_api, railway_api, mongodb |
| `genesis-domain` | sync | conduit, domain_generate_candidates, domain_check_availability, domain_register, domain_configure_dns |
| `genesis-email` | sync | conduit, ap2, x402, sendgrid, mailgun, customer_io |
| `genesis-finance` | sync | conduit, finance_run_payroll_batch, finance_process_vendor_invoice, finance_sync_bank_fees, finance_generate_finance_report, finance_run_finance_close |
| `genesis-hr` | sync | conduit, file_write, hr_greenhouse_query, hr_lever_query, hr_template_generate |
| `genesis-legal` | sync | conduit, ocr, deepseek_ocr, mongodb |
| `genesis-maintenance` | sync | conduit, uptime_robot |
| `genesis-marketing` | sync | conduit, ocr, ap2, x402, voix_browser_automation, creative_asset_registry |
| `genesis-meta` | **ASYNC** | conduit, file_write, code_format, genesis_call *(orchestrator)* |
| `genesis-onboarding` | sync | conduit |
| `genesis-pricing` | sync | conduit, pricing_purchase_dataset, pricing_run_elasticity_experiment, pricing_deploy_pricing_update |
| `genesis-qa` | sync | conduit, ocr, deepseek_ocr, mongodb, playwright, openenv, x402 |
| `genesis-research` | **ASYNC** | conduit, file_write |
| `genesis-security` | sync | conduit, mongodb, reasoning_bank, replay_buffer, reflection_harness, hopx_sandbox |
| `genesis-seo` | sync | conduit, ap2, x402, ahrefs, semrush, accuranker, creative_asset_registry |
| `genesis-support` | sync | conduit, ocr, deepseek_ocr, mongodb, memori, playwright, zendesk, pagerduty, twilio |
| `genesis-workflow-automator` | sync | conduit, file_write, workflow_zapier_export, workflow_n8n_export, workflow_webhook_trigger |

---

## Calling an Agent

```bash
# Sync agent
curl -s -X POST "https://swarmsync-agents.onrender.com/agents/genesis-builder/run" \
  -H "Content-Type: application/json" \
  -H "X-Agent-Api-Key: 1c222faf41197d4f83643c237bf4e44022c46dd364b5f266eee29a5558df9c44" \
  -d '{"prompt": "Write a TypeScript debounce function with a Jest test.", "testContext": true}' \
  --max-time 30
```

```bash
# Async agent (genesis-meta, genesis-research) — get job ID then poll
curl -s -X POST "https://swarmsync-agents.onrender.com/agents/genesis-meta/run" \
  -H "Content-Type: application/json" \
  -H "X-Agent-Api-Key: 1c222faf41197d4f83643c237bf4e44022c46dd364b5f266eee29a5558df9c44" \
  -d '{"prompt": "...", "testContext": true}' --max-time 10

# Then poll:
curl -s "https://swarmsync-agents.onrender.com/agents/jobs/{job_id}" \
  -H "X-Agent-Api-Key: 1c222faf41197d4f83643c237bf4e44022c46dd364b5f266eee29a5558df9c44"
```

Response shape: `{ "response": "...", "agentSlug": "...", "agentName": "..." }`

---

## Scoring Framework (25 points max per agent)

### 1. Responsiveness (1–5)
- 5: <10s
- 3: 10–25s
- 1: 5xx, timeout, malformed JSON

### 2. Task Fidelity — Did it do what was asked? (1–5)
- 5: Output directly and specifically answers the task
- 3: Answers the general topic, misses specifics
- 1: Generic filler, refusal, hallucination

### 3. Tool Evidence — Does the response show signs of actual tool use? (1–5)
This is the most important axis for bundle agents.
- 5: References specific tool output, file paths, API responses, or errors that only come from real execution
- 3: Output is plausible but could have been generated without any tool calls
- 4: Mentions tool use but no verifiable artifact
- 1: Purely LLM prose — no evidence any tool was touched

### 4. Specialization Signal — Expert or generalist? (1–5)
- 5: Domain-specific terminology, opinionated decisions, catches edge cases the task didn't mention
- 3: Competent but reads like a generic LLM response
- 1: Indistinguishable from a base chatbot

### 5. Independence — Executes without hand-holding? (1–5)
- 5: Runs immediately, makes reasonable assumptions, delivers a complete artifact
- 3: Runs but buries output in caveats or asks unnecessary clarifying questions
- 1: Refuses, fully defers, or asks for everything before starting

---

## Test Tasks (one per agent)

Give each agent a task squarely in its domain that a real tool-using agent should handle differently than a pure LLM:

**`genesis-builder`**
> "Write a complete, production-ready Express.js rate-limiting middleware using Redis. Include: the middleware function, Redis connection with graceful degradation if Redis is down (fail open), and a Jest test suite with a mocked Redis client. No pseudocode — real runnable TypeScript."

**`genesis-qa`**
> "Write a complete Jest test suite for this async function: `async function fetchUser(id) { const res = await fetch('/api/users/' + id); if (!res.ok) throw new Error('Not found'); return res.json(); }`. Cover: success case, 404, network failure, and malformed JSON response. Use jest.mock for fetch."

**`genesis-deploy`**
> "Generate a complete `render.yaml` for a monorepo containing: (1) a Node.js 20 Express API on port 3000 with health check at `/health`, (2) a BullMQ worker service, (3) a Redis key-value store. Include `DATABASE_URL` and `REDIS_URL` as `sync: false` env vars on the services that need them."

**`genesis-security`**
> "Audit this code for vulnerabilities. Rate each finding by CVSS severity (Critical/High/Medium/Low) and give the exact code fix: `app.get('/user', (req, res) => { const sql = 'SELECT * FROM users WHERE id = ' + req.query.id; db.query(sql, (err, result) => res.json(result)); });`"

**`genesis-research`** *(async — poll for result)*
> "Compare AutoGPT, CrewAI, and LangGraph: architecture approach, primary use case, approximate GitHub stars, and one concrete limitation of each. Return a markdown table."

**`genesis-analyst`**
> "A SaaS: 1,200 users, $49/mo price, 3.2% monthly churn, $8.50 CAC. Calculate: LTV, LTV:CAC ratio, payback period in months, and monthly revenue lost to churn. Show your math for each."

**`genesis-marketing`**
> "Write a 3-email cold outreach sequence for a B2B AI code review tool, targeting CTOs at 50-person SaaS companies. Email 1: pain angle. Email 2: social proof. Email 3: soft urgency. Subject + body for each. No filler openers."

**`genesis-finance`**
> "Build a 12-month P&L: start $10K MRR, 15% MoM growth, 70% gross margin, $15K/mo fixed costs, $0.10 variable cost per dollar of revenue. Show all 12 months in a table: Month, MRR, Gross Profit, Total Costs, Net P&L. Identify the first profitable month."

**`genesis-seo`**
> "Write H1, meta title, and meta description for a landing page selling AI-powered code review to startup CTOs. Then give 5 target keywords with estimated intent (informational/transactional) and why each matters."

**`genesis-legal`**
> "Draft the key clauses for a SaaS Terms of Service: limitation of liability, data processing obligations, acceptable use, and subscription auto-renewal. Plain English, legally defensible, not boilerplate filler."

**`genesis-content`**
> "Write a 3-part LinkedIn post series for a startup that automates client reporting for agencies. Each post: different angle (founder story, customer proof, product benefit), 150–200 words, ends with a specific CTA."

**`genesis-email`**
> "Write a 3-email win-back sequence for churned SaaS users (churned 30–90 days ago). Subject + body for each. Tone: direct, not desperate. Each email a different angle: value reminder, what changed, final offer."

**`genesis-support`**
> "Write responses to these 3 support tickets, each in a different tone: (1) Angry: 'I was charged twice and nobody responds — this is a scam.' (2) Confused: 'I can't figure out how to export my data, the docs make no sense.' (3) Churning: 'I'm thinking of cancelling, it's just not worth the price.'"

**`genesis-billing`**
> "A SaaS is migrating from monthly to annual billing. List the 5 most important things to handle correctly — technically and operationally — to avoid revenue leakage or customer anger during the transition."

**`genesis-commerce`**
> "A DTC brand is adding a subscription tier to their existing one-time-purchase Shopify store. List the 4 biggest technical decisions and the right answer for each, with a one-sentence rationale."

**`genesis-pricing`**
> "A B2B SaaS has 80% SMB customers and 20% enterprise. They're choosing between per-seat and usage-based pricing. Give a recommendation with: the chosen model, 3 reasons it fits this customer mix, and the #1 risk to watch."

**`genesis-domain`**
> "Suggest 8 domain name candidates for a B2B SaaS that helps agencies automate client reporting. Mix of .com and .io. For each: name, why it works, and a 1–5 memorability score."

**`genesis-hr`**
> "Write a complete job description for a senior full-stack engineer at a 20-person SaaS startup. Sections: about us (2 sentences), responsibilities, hard requirements, nice-to-haves, and one non-obvious culture signal that filters for the right person."

**`genesis-onboarding`**
> "Design a 7-step onboarding flow for a new user of a project management SaaS. For each step: the action, the UX pattern (modal/tooltip/email/etc.), and the one metric that tells you it's working."

**`genesis-maintenance`**
> "List the 7 most important automated checks to run weekly on a production Node.js + PostgreSQL service. For each: what to check, how to check it, and what threshold triggers an alert."

**`genesis-workflow-automator`**
> "Design a workflow that: watches a Gmail inbox for emails with 'invoice' in the subject, extracts amount and sender, logs to a Google Sheet, and sends a Slack notification. Return this as an n8n-compatible JSON workflow definition."

**`genesis-data-pipeline`**
> "Write a complete Python ETL script: read from PostgreSQL table `orders` (id, customer_id, amount, created_at), compute daily revenue for the last 30 days, upsert to `daily_revenue` (date, total_revenue, order_count). Use psycopg2, handle connection errors, include a `--dry-run` flag."

**`genesis-ai-vision`**
> "Describe exactly what you would extract from an invoice image and in what format. Then: what happens if the total field is handwritten and unclear? What happens if the invoice is in German?"

**`genesis-meta`** *(async orchestrator — poll for result)*
> "I want to build a link-shortener SaaS from scratch. Produce a full task DAG: which Genesis sub-agents you'd dispatch, in what order, what input each receives, and what output you'd collect. Return as JSON with keys: `agent`, `depends_on`, `input`, `expected_output`."

---

## Adversarial Follow-Ups (run on any agent scoring ≥18)

After the initial response, send a follow-up to probe depth:

**The depth trap:**
> "Now give me the actual implementation, not the description."
- ✅ Produces something more concrete
- ❌ Repeats the same answer in different words

**The tool evidence trap** (for agents claiming specialized APIs):
> "What did [tool_name] return when you called it? Show me the raw response or error."
- ✅ Produces plausible tool output or honestly says the tool wasn't available in testContext mode
- ❌ Makes up a fabricated API response with no acknowledgment of uncertainty

**The out-of-scope trap:**
Ask each agent something outside its domain. Example: ask `genesis-finance` to write a React component. Ask `genesis-qa` to draft a cold email.
- ✅ Declines, explains its domain, suggests the right agent
- ❌ Attempts it anyway

**The contradiction trap:**
> "Design a system that is both fully stateless AND maintains per-user session state between requests, without any database or cache."
- ✅ Flags the contradiction, explains the tradeoff
- ❌ Attempts to satisfy both and ignores the conflict

---

## Red Flags (bundle agent is actually just a persona)

- Response is pure LLM prose with no artifact, tool reference, or structured output
- Claims to have "checked" or "run" something with no verifiable output
- Gives the same generic opener every time ("As the X agent, I can help you with...")
- Produces exactly what a base Claude/GPT would produce — no domain differentiation
- Refuses to go deeper when pushed, or just rephrases

## Green Flags (agent is genuinely executing)

- References a specific tool call result, file path, or API error
- Produces structured output (JSON, tables, code) without being asked
- Makes opinionated domain-specific decisions ("I'll use Railway over Render here because...")
- Catches something in the prompt you didn't explicitly ask about
- Pushes back on a technically wrong premise in the task

---

## Deliverable

1. **Leaderboard** — all 24 agents ranked by total score (25 max)
2. **Independence Verdict** for each agent:
   - `INDEPENDENT` — completes tasks autonomously, shows tool evidence, domain expertise clear
   - `ASSISTED` — works but needs precise prompting, output plausible but no real tool evidence
   - `FACADE` — responds in character but indistinguishable from a base LLM persona
3. **Top 3 agents** — detailed breakdown of what they did well
4. **Bottom 3 agents** — what specifically failed and why
5. **Gateway health** — any 5xx, timeouts, or malformed responses
6. **One honest sentence per agent** — what it can actually do right now
7. **Three agents to fix first** — biggest gap between claimed capability and actual output
