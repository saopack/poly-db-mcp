"""MCP JSON-RPC 协议路由：initialize、tools/list、tools/call、SSE"""
import os
import json
import asyncio
import logging
from typing import Dict, Any, Optional
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..dependencies import get_mcp_handler

logger = logging.getLogger(__name__)
router = APIRouter()

MCP_SERVER_INFO = {"name": "db-mcp", "version": "0.0.2"}
MCP_CAPABILITIES = {"tools": {}}
_QUERY_TIMEOUT = int(os.environ.get("MCP_QUERY_TIMEOUT", "3600"))


def _get_server_base_url() -> str:
    return os.environ.get("MCP_BASE_URL", "http://localhost:8000").rstrip("/")


def _handle_mcp_jsonrpc(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    jsonrpc = body.get("jsonrpc", "2.0")
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    if method == "notifications/initialized":
        return None

    if method == "initialize":
        return {
            "jsonrpc": jsonrpc,
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "serverInfo": MCP_SERVER_INFO,
                "capabilities": MCP_CAPABILITIES,
            }
        }

    if method == "tools/list":
        tools = get_mcp_handler().get_tools()
        tool_list = []
        for tool in tools:
            param_schema = {}
            required_params = []
            for p in tool.parameters:
                param_schema[p.name] = {"type": p.type, "description": p.description}
                if p.required:
                    required_params.append(p.name)
            tool_list.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {
                    "type": "object",
                    "properties": param_schema,
                    "required": required_params,
                }
            })
        return {"jsonrpc": jsonrpc, "id": req_id, "result": {"tools": tool_list}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = get_mcp_handler().call_tool(tool_name, arguments)
        if result.status == "success":
            content_text = json.dumps(result.content, ensure_ascii=False, default=str) if isinstance(result.content, dict) else str(result.content)
        else:
            content_text = result.error.message if result.error else "Unknown error"
        return {
            "jsonrpc": jsonrpc,
            "id": req_id,
            "result": {"content": [{"type": "text", "text": content_text}]}
        }

    return {
        "jsonrpc": jsonrpc,
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


@router.get("/", summary="MCP根路径")
async def mcp_root():
    base_url = _get_server_base_url()
    return {
        "name": "MCP Database Execution Tool",
        "version": "0.0.2",
        "description": "Database SQL execution service for Dify MCP",
        "oauth_metadata": {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/authorize",
            "token_endpoint": f"{base_url}/token",
            "registration_endpoint": f"{base_url}/register",
        }
    }


@router.post("/", summary="MCP JSON-RPC 入口")
async def mcp_jsonrpc_root(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "Request body must be valid JSON"})
    if body.get("method") == "tools/call":
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_handle_mcp_jsonrpc, body), timeout=_QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            req_id = body.get("id")
            return JSONResponse(content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"Request timed out after {_QUERY_TIMEOUT}s. If this is a first-time ephemeral build, retry after the image is cached."},
            })
    else:
        result = _handle_mcp_jsonrpc(body)
    if result is None:
        return Response(status_code=204)
    return JSONResponse(content=result)


@router.post("/mcp", summary="MCP JSON-RPC 入口 (/mcp)")
@router.post("/mcp/", summary="MCP JSON-RPC 入口 (/mcp/)", include_in_schema=False)
async def mcp_jsonrpc(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "Request body must be valid JSON"})
    if body.get("method") == "tools/call":
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_handle_mcp_jsonrpc, body), timeout=_QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            req_id = body.get("id")
            return JSONResponse(content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"Request timed out after {_QUERY_TIMEOUT}s. If this is a first-time ephemeral build, retry after the image is cached."},
            })
    else:
        result = _handle_mcp_jsonrpc(body)
    if result is None:
        return Response(status_code=204)
    return JSONResponse(content=result)


@router.get("/sse", summary="MCP SSE 端点")
async def mcp_sse():
    base_url = _get_server_base_url()

    async def event_stream():
        yield f"event: endpoint\ndata: {base_url}/messages\n\n"
        while True:
            try:
                await asyncio.sleep(30)
                yield ": heartbeat\n\n"
            except asyncio.CancelledError:
                break

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )


@router.post("/messages", summary="MCP SSE 消息端点")
async def mcp_messages(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "Request body must be valid JSON"})
    if body.get("method") == "tools/call":
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_handle_mcp_jsonrpc, body), timeout=_QUERY_TIMEOUT)
        except asyncio.TimeoutError:
            req_id = body.get("id")
            return JSONResponse(content={
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": f"Request timed out after {_QUERY_TIMEOUT}s. If this is a first-time ephemeral build, retry after the image is cached."},
            })
    else:
        result = _handle_mcp_jsonrpc(body)
    if result is None:
        return Response(status_code=204)
    return JSONResponse(content=result)


@router.get("/.well-known/oauth-authorization-server", summary="OAuth服务发现")
async def oauth_authorization_server():
    base_url = _get_server_base_url()
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "code_challenge_methods_supported": ["S256", "plain"],
    }


@router.get("/mcp", summary="MCP服务信息")
async def mcp_info():
    base_url = _get_server_base_url()
    return {
        "protocol_version": "1.0",
        "capabilities": {"tools": True, "resources": False, "prompts": False},
        "services": ["execute_sql", "list_databases", "list_db_versions"],
        "oauth_metadata": {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/authorize",
            "token_endpoint": f"{base_url}/token",
            "registration_endpoint": f"{base_url}/register",
        }
    }


@router.get("/mcp/tools", summary="MCP工具列表")
async def mcp_tools_list():
    tools = get_mcp_handler().get_tools()
    dify_tools = []
    for tool in tools:
        dify_params = []
        for param in tool.parameters:
            dify_params.append({
                "name": param.name, "type": param.type,
                "required": param.required, "description": param.description,
                "default": param.default
            })
        dify_tools.append({"name": tool.name, "description": tool.description, "parameters": dify_params})
    return {"tools": dify_tools}
