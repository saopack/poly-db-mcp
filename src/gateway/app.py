"""Gateway FastAPI application — lightweight routing proxy.

The Gateway is a **stateless** FastAPI app that:

- Loads ``routing.yaml`` via ConfigManager
- Builds a ``RouteTable`` and ``ProxyClient``
- Registers gateway routes (transparent proxy, scatter-gather, MCP routing)
- Does NOT load databases.yaml, manage Docker containers, or hold DB connections
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from ..config_manager import ConfigManager
from .router import RouteTable
from .proxy import ProxyClient
from .routes import router as gateway_router, configure_gateway

logger = logging.getLogger(__name__)


@asynccontextmanager
async def gateway_lifespan(app: FastAPI):
    """Load routing config on startup, clean up ProxyClient on shutdown."""
    logger.info("Gateway: loading routing configuration ...")

    routing_path = os.environ.get(
        "MCP_ROUTING_CONFIG",
        "config/routing.yaml",
    )
    routing_config = ConfigManager.load_routing_config(routing_path)

    route_table = RouteTable.from_config(routing_config)
    logger.info("Gateway: %s", route_table)

    gateway_cfg = routing_config.get("gateway", {})
    timeout = int(gateway_cfg.get("request_timeout", 3600))
    retry = gateway_cfg.get("retry_on_node_error", False)

    proxy = ProxyClient(route_table, timeout=timeout)
    configure_gateway(proxy)

    logger.info(
        "Gateway: ready — %d Node(s), %d route(s), timeout=%ds",
        route_table.node_count,
        route_table.route_count,
        timeout,
    )

    yield

    logger.info("Gateway: shutting down ProxyClient ...")
    await proxy.close()
    logger.info("Gateway: shutdown complete.")


def create_gateway_app(route_table: Optional[RouteTable] = None) -> FastAPI:
    """Create and return a Gateway FastAPI application.

    Args:
        route_table: Optional pre-built RouteTable. If None, the lifespan
                     handler loads routing.yaml automatically.

    Returns:
        FastAPI application ready for ``uvicorn.run()``.
    """
    app = FastAPI(
        title="poly-db-mcp Gateway",
        version="0.0.2",
        description="Stateless routing proxy for the poly-db-mcp distributed architecture",
        lifespan=gateway_lifespan,
    )
    app.include_router(gateway_router)
    return app
