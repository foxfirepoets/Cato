"""
cato/api/routes.py — Central API routing registration.

Import and call register_all_routes(app) to attach every API endpoint
to an aiohttp Application instance.
"""

from __future__ import annotations

import logging
from aiohttp import web

logger = logging.getLogger(__name__)


def register_all_routes(app: web.Application) -> None:
    """Attach all API routes to the given aiohttp Application."""
    from cato.api.websocket_handler import register_routes as register_coding_agent
    from cato.api.workspace_routes import register_routes as register_workspace
    from cato.api.logs_routes import register_routes as register_logs
    from cato.api.memory_routes import register_routes as register_memory
    from cato.api.whatsapp_routes import register_routes as register_whatsapp
    from cato.api.pty_routes import register_routes as register_pty
    from cato.api.integration_routes import register_routes as register_integrations

    register_coding_agent(app)
    register_workspace(app)
    register_logs(app)
    register_memory(app)
    register_whatsapp(app)
    register_integrations(app)
    register_pty(app)
    logger.info("All API routes registered")
