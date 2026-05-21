"""SQL执行和数据库信息路由"""
import os
import asyncio
import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..config_manager import ConfigManager
from ..executor import MCPExecutor
from ..dependencies import get_client_registry

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")
router = APIRouter()

_QUERY_TIMEOUT = int(os.environ.get("MCP_QUERY_TIMEOUT", "300"))


class ExecuteSqlRequest(BaseModel):
    db_type: str
    version: str
    query: str = Field(..., min_length=1, max_length=5000)
    db_compatibility: Optional[str] = Field(None, description="数据库兼容性模式，支持通用名(oracle/pg/mysql/sqlserver)或Vastbase编码(A/B/C/PG/MSSQL)，自动转换为目标库格式")
    explain: bool = Field(False, description="是否使用EXPLAIN模式查看执行计划而不实际执行")

    @field_validator('query')
    @classmethod
    def query_must_not_be_whitespace(cls, v):
        if not v.strip():
            raise ValueError('Query cannot be empty or whitespace only')
        return v


class ExecuteSqlResponse(BaseModel):
    status: str
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    elapsed_ms: Optional[float] = None


def _extract_bearer_token(api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    return api_key[7:] if api_key.startswith("Bearer ") else api_key


def _require_api_key(api_key: Optional[str]):
    token = _extract_bearer_token(api_key)
    client_info = get_client_registry().validate_api_key(token) if token else None
    if not client_info:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API key")
    return client_info


def _audit_log(client_info, db_type: str, version: str, query: str, result_status: str) -> None:
    audit_logger.info(
        "SQL executed",
        extra={
            "client_id": client_info.client_id,
            "client_name": client_info.name,
            "db_type": db_type,
            "version": version,
            "query_preview": query[:200],
            "status": result_status,
        }
    )


@router.get("/api/databases", summary="获取支持的数据库类型及版本列表")
async def get_databases():
    databases = []
    for db_type in ConfigManager.get_supported_databases():
        versions = ConfigManager.get_db_versions(db_type)
        databases.append({"type": db_type, "versions": versions})
    return {"databases": databases}


@router.get("/api/databases/{db_type}/versions", summary="获取指定数据库类型支持的版本列表")
async def get_db_versions(db_type: str):
    versions = ConfigManager.get_db_versions(db_type)
    if not versions:
        raise HTTPException(status_code=404, detail=f"Database type {db_type} not found")
    return {"db_type": db_type, "versions": versions}


@router.post("/api/execute_sql", summary="执行SQL", response_model=ExecuteSqlResponse)
async def execute_sql(request: ExecuteSqlRequest, api_key: Optional[str] = Header(None, alias="Authorization")):
    client_info = _require_api_key(api_key)
    executor = MCPExecutor()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                executor.execute,
                request.db_type, request.version, request.query,
                db_compatibility=request.db_compatibility,
                explain=request.explain,
            ),
            timeout=_QUERY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Query timed out after {_QUERY_TIMEOUT}s: db_type={request.db_type}, version={request.version}")
        _audit_log(client_info, request.db_type, request.version, request.query, "timeout")
        return JSONResponse(
            status_code=504,
            content={"status": "error", "message": f"Query timed out after {_QUERY_TIMEOUT}s"},
        )
    _audit_log(client_info, request.db_type, request.version, request.query, result.get("status", "error"))
    return result


@router.post("/api/shutdown", summary="停止服务")
async def shutdown():
    """Stop Docker containers and shut down the server."""
    logger.info("Shutdown requested via API")
    try:
        from ..container_pool import ContainerPool
        pool = ContainerPool()
        pool.shutdown()
    except Exception as e:
        logger.warning(f"Error during container cleanup: {e}")
    import os
    import signal
    import sys
    if sys.platform == "win32":
        os._exit(0)
    else:
        os.kill(os.getpid(), signal.SIGTERM)
    return {"status": "shutting_down"}


@router.get("/api/health", summary="健康检查")
async def health_check():
    checks = {"config": "ok", "docker": "ok", "databases": []}
    healthy = True

    # config check
    if not ConfigManager.is_config_valid():
        checks["config"] = "invalid"
        healthy = False

    # docker check
    try:
        import docker
        client = docker.from_env()
        client.ping()
    except Exception:
        checks["docker"] = "unavailable"
        healthy = False

    # per-database container and port status
    if checks["docker"] == "ok":
        try:
            from ..container_pool import ContainerPool
            pool = ContainerPool()
            checks["containers"] = pool.get_status()
        except Exception:
            pass

    return {"status": "healthy" if healthy else "degraded", "checks": checks}


@router.post("/api/dify/execute_sql", summary="Dify专用SQL执行接口", response_model=ExecuteSqlResponse)
async def dify_execute_sql(request: ExecuteSqlRequest, api_key: Optional[str] = Header(None, alias="Authorization")):
    client_info = _require_api_key(api_key)
    executor = MCPExecutor()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                executor.execute,
                request.db_type, request.version, request.query,
                db_compatibility=request.db_compatibility,
                explain=request.explain,
            ),
            timeout=_QUERY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Dify query timed out after {_QUERY_TIMEOUT}s: db_type={request.db_type}, version={request.version}")
        _audit_log(client_info, request.db_type, request.version, request.query, "timeout")
        return JSONResponse(
            status_code=504,
            content={"status": "error", "message": f"Query timed out after {_QUERY_TIMEOUT}s"},
        )
    _audit_log(client_info, request.db_type, request.version, request.query, result.get("status", "error"))
    return result
