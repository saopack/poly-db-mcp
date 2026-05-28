"""MCP Database Execution Tool — FastAPI 应用入口"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

from .config_manager import ConfigManager
from .routes import mcp_router, oauth_router, client_router, execute_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading configuration...")
    ConfigManager.load_config()

    # Pre-warm containers configured with prewarm: true
    from .container_pool import ContainerPool
    pool = ContainerPool()
    logger.info("Pre-warming database containers (prewarm: true)...")
    pool.prewarm()
    logger.info("Pre-warm complete")

    yield

    logger.info("Shutting down container pool...")
    pool.shutdown()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="MCP Database Execution Tool",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(mcp_router)
app.include_router(oauth_router)
app.include_router(client_router)
app.include_router(execute_router)
