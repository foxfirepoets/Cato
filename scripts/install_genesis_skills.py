#!/usr/bin/env python3
"""Install Genesis Agent SKILL.md files into Cato's skills directory.

Cato's gateway scans ``~/.cato/skills/`` for directories that contain a
``SKILL.md`` file. This script writes one such directory per Genesis Agent
so each agent becomes a discoverable skill.

Usage:
    python install_genesis_skills.py            # install to ~/.cato/skills
    python install_genesis_skills.py --dry-run  # show what would be written
    python install_genesis_skills.py --root /tmp/skills  # custom root

Stdlib only. Idempotent.
"""

from __future__ import annotations

import argparse
from pathlib import Path


AGENT_REGISTRY = [
    # ---- Deployed (15) ----
    {
        "slug": "genesis-meta",
        "name": "Genesis Meta Agent",
        "route": "/orchestrate",
        "price_usd": 100,
        "status": "deployed",
        "description": "Multi-agent orchestrator - coordinates Genesis agents on complex multi-step tasks.",
        "specialty": (
            "Use the Meta Agent when a request spans multiple Genesis agents and "
            "needs a coordinated plan. It decomposes the goal, assigns sub-tasks, "
            "and aggregates the results into a single coherent output."
        ),
    },
    {
        "slug": "genesis-builder",
        "name": "Genesis Builder Agent",
        "route": "/generate/module",
        "price_usd": 200,
        "status": "deployed",
        "description": "Generates production-grade Python/TS modules from a spec.",
        "specialty": (
            "Use the Builder Agent to turn a written specification into a "
            "production-ready Python or TypeScript module. It produces typed "
            "interfaces, tests, and docstrings in one pass."
        ),
    },
    {
        "slug": "genesis-research",
        "name": "Genesis Research Agent",
        "route": "/research/comprehensive",
        "price_usd": 150,
        "status": "deployed",
        "description": "Comprehensive research across 15+ sources with citations.",
        "specialty": (
            "Use the Research Agent for deep-dive topical research that requires "
            "synthesis across many sources. It returns a structured brief with "
            "inline citations and a source list."
        ),
    },
    {
        "slug": "genesis-deploy",
        "name": "Genesis Deploy Agent",
        "route": "/deploy/advanced",
        "price_usd": 300,
        "status": "deployed",
        "description": "Advanced deployment to multi-region cloud infrastructure.",
        "specialty": (
            "Use the Deploy Agent when you need to ship a service to multi-region "
            "cloud infrastructure with rollout, health checks, and rollback. It "
            "produces deployable artifacts and pipeline configuration."
        ),
    },
    {
        "slug": "genesis-qa",
        "name": "Genesis QA Agent",
        "route": "/test/analysis",
        "price_usd": 150,
        "status": "deployed",
        "description": "Test analysis, coverage gaps, and regression risk assessment.",
        "specialty": (
            "Use the QA Agent to evaluate an existing test suite for coverage "
            "gaps and regression risk. It returns prioritized findings and "
            "suggested additional tests."
        ),
    },
    {
        "slug": "genesis-finance",
        "name": "Genesis Finance Agent",
        "route": "/finance/strategy",
        "price_usd": 400,
        "status": "deployed",
        "description": "Strategic financial planning, valuation, runway modeling.",
        "specialty": (
            "Use the Finance Agent for strategic financial planning, valuation "
            "exercises, and runway modeling. It returns scenario tables and a "
            "written rationale."
        ),
    },
    {
        "slug": "genesis-marketing",
        "name": "Genesis Marketing Agent",
        "route": "/marketing/strategy",
        "price_usd": 300,
        "status": "deployed",
        "description": "Full-funnel marketing strategy with channel-mix recommendations.",
        "specialty": (
            "Use the Marketing Agent for full-funnel marketing strategy, including "
            "channel mix, positioning, and campaign sequencing. It returns a plan "
            "with budget allocation guidance."
        ),
    },
    {
        "slug": "genesis-content",
        "name": "Genesis Content Agent",
        "route": "/content/whitepaper",
        "price_usd": 180,
        "status": "deployed",
        "description": "Long-form content - whitepapers, technical docs, narrative briefs.",
        "specialty": (
            "Use the Content Agent to author long-form content such as whitepapers, "
            "technical documentation, or narrative briefs. It returns publication-"
            "ready prose with structure and headings."
        ),
    },
    {
        "slug": "genesis-security",
        "name": "Genesis Security Agent",
        "route": "/security/pentest",
        "price_usd": 600,
        "status": "deployed",
        "description": "Penetration testing and security posture analysis.",
        "specialty": (
            "Use the Security Agent for penetration testing and security posture "
            "analysis. It returns ranked vulnerabilities, reproduction steps, and "
            "remediation guidance."
        ),
    },
    {
        "slug": "genesis-seo",
        "name": "Genesis SEO Agent",
        "route": "/seo/strategy",
        "price_usd": 180,
        "status": "deployed",
        "description": "SERP-driven SEO strategy with keyword and content briefs.",
        "specialty": (
            "Use the SEO Agent for SERP-driven SEO strategy, including keyword "
            "research and content briefs. It returns prioritized opportunities and "
            "draft outlines."
        ),
    },
    {
        "slug": "genesis-support",
        "name": "Genesis Support Agent",
        "route": "/support/system",
        "price_usd": 75,
        "status": "deployed",
        "description": "End-to-end customer support system design.",
        "specialty": (
            "Use the Support Agent to design an end-to-end customer support "
            "system. It returns workflow definitions, macros, and escalation "
            "policies."
        ),
    },
    {
        "slug": "genesis-email",
        "name": "Genesis Email Agent",
        "route": "/email/campaign",
        "price_usd": 120,
        "status": "deployed",
        "description": "Multi-touch email campaign authoring and sequencing.",
        "specialty": (
            "Use the Email Agent to author multi-touch email campaigns with "
            "sequencing logic. It returns subject lines, body copy, and a send "
            "cadence."
        ),
    },
    {
        "slug": "genesis-analyst",
        "name": "Genesis Analyst Agent",
        "route": "/analyze/strategy",
        "price_usd": 200,
        "status": "deployed",
        "description": "Strategic analysis with data-backed recommendations.",
        "specialty": (
            "Use the Analyst Agent for strategic analysis backed by data. It "
            "returns a written analysis with supporting figures and a set of "
            "recommendations."
        ),
    },
    {
        "slug": "genesis-commerce",
        "name": "Genesis Commerce Agent",
        "route": "/commerce/integration",
        "price_usd": 250,
        "status": "deployed",
        "description": "E-commerce platform integration, cart, and checkout flows.",
        "specialty": (
            "Use the Commerce Agent to design and integrate e-commerce platforms, "
            "carts, and checkout flows. It returns integration plans and reference "
            "code."
        ),
    },
    {
        "slug": "genesis-billing",
        "name": "Genesis Billing Agent",
        "route": "/billing/revops",
        "price_usd": 100,
        "status": "deployed",
        "description": "Revenue operations - subscription billing, invoicing, dunning.",
        "specialty": (
            "Use the Billing Agent for revenue operations covering subscription "
            "billing, invoicing, and dunning. It returns workflow definitions and "
            "policy recommendations."
        ),
    },
    # ---- Pending (5) ----
    {
        "slug": "genesis-legal",
        "name": "Genesis Legal Agent",
        "status": "pending",
    },
    {
        "slug": "genesis-hr",
        "name": "Genesis HR Agent",
        "status": "pending",
    },
    {
        "slug": "genesis-data-pipeline",
        "name": "Genesis Data Pipeline Agent",
        "status": "pending",
    },
    {
        "slug": "genesis-workflow-automator",
        "name": "Genesis Workflow Automator",
        "status": "pending",
    },
    {
        "slug": "genesis-ai-vision",
        "name": "Genesis AI Vision API",
        "status": "pending",
    },
]


GATEWAY_BASE_URL = "https://swarmsync-agents.onrender.com"


def render_deployed(agent: dict) -> str:
    """Return the SKILL.md body for a deployed agent."""
    name = agent["name"]
    slug = agent["slug"]
    route = agent["route"]
    price = agent["price_usd"]
    description = agent["description"]
    specialty = agent["specialty"]

    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "version: 1.0.0\n"
        "---\n"
        "\n"
        f"# {name}\n"
        "\n"
        "Hosted on the SwarmSync agents gateway.\n"
        "\n"
        "## When to use\n"
        "\n"
        f"{specialty}\n"
        "\n"
        "## Invocation\n"
        "\n"
        "Cato calls this agent via the `genesis` tool:\n"
        "\n"
        "```json\n"
        "{\n"
        f'  "agent": "{slug}",\n'
        '  "task": "<your task as a string>",\n'
        '  "params": {}\n'
        "}\n"
        "```\n"
        "\n"
        "## Endpoint\n"
        "\n"
        f"`POST {GATEWAY_BASE_URL}{route}`\n"
        "\n"
        "## Tier price\n"
        "\n"
        f"USD {price} per invocation (signed AP2 envelope; no payment rail in v1).\n"
        "\n"
        "## Status\n"
        "\n"
        "Deployed.\n"
    )


def render_pending(agent: dict) -> str:
    """Return the SKILL.md body for a pending agent."""
    name = agent["name"]
    slug = agent["slug"]

    return (
        "---\n"
        f"name: {name}\n"
        f"description: {name} - pending deployment on SwarmSync.\n"
        "version: 1.0.0\n"
        "---\n"
        "\n"
        f"# {name}\n"
        "\n"
        "This agent is registered with Cato but is not yet deployed on the "
        "SwarmSync agents gateway.\n"
        "\n"
        "## When to use\n"
        "\n"
        "(Pending deployment - descriptor will be filled in once the agent ships.)\n"
        "\n"
        "## Invocation\n"
        "\n"
        "Cato calls this agent via the `genesis` tool. The tool will return a "
        'clear "pending deployment" response until SwarmSync deploys the '
        "endpoint:\n"
        "\n"
        "```json\n"
        "{\n"
        f'  "agent": "{slug}",\n'
        '  "task": "<your task as a string>",\n'
        '  "params": {}\n'
        "}\n"
        "```\n"
        "\n"
        "## Status\n"
        "\n"
        "Pending deployment.\n"
    )


def render_skill_md(agent: dict) -> str:
    if agent["status"] == "deployed":
        return render_deployed(agent)
    return render_pending(agent)


def install(root: Path, dry_run: bool = False) -> int:
    """Install all Genesis skills under ``root``. Returns count written."""
    count = 0
    for agent in AGENT_REGISTRY:
        slug = agent["slug"]
        skill_dir = root / slug
        skill_file = skill_dir / "SKILL.md"
        content = render_skill_md(agent)

        if dry_run:
            print(f"DRY-RUN would write: {skill_file} ({len(content)} bytes)")
        else:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(content, encoding="utf-8", newline="\n")
        count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install Genesis Agent SKILL.md files for Cato."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without touching the filesystem.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Override install root (default: ~/.cato/skills).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.root is not None:
        root = args.root.expanduser().resolve()
    else:
        root = Path.home() / ".cato" / "skills"

    count = install(root, dry_run=args.dry_run)

    if args.dry_run:
        print(f"DRY-RUN complete. Would install {count}/{len(AGENT_REGISTRY)} Genesis skills at {root}")
    else:
        print(f"Installed {count}/{len(AGENT_REGISTRY)} Genesis skills at {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
