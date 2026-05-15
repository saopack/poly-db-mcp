"""MCP Database Validation Tool — FastAPI 应用入口"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

from .config_manager import ConfigManager
from .routes import mcp_router, oauth_router, client_router, validation_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading configuration...")
    ConfigManager.load_config()
    yield
    logger.info("Shutting down Docker containers...")
    from .docker_manager import DockerManager
    dm = DockerManager()
    dm.stop_all_warm_containers()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="MCP Database Validation Tool",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(mcp_router)
app.include_router(oauth_router)
app.include_router(client_router)
app.include_router(validation_router)
