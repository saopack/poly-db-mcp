"""Gateway FastAPI routes — transparent proxy + scatter-gather + failover + OAuth."""

import json
import logging
import time
from typing import Dict, Optional

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from ..client_registry import ClientRegistry
from .router import Route
from .proxy import ProxyClient, NodeUnreachableError

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_proxy: Optional[ProxyClient] = None
_route_table = None
_auth_registry: Optional[ClientRegistry] = None

# SSE session → (node_name, base_url)
_sse_sessions: Dict[str, tuple] = {}
_last_session_cleanup = time.monotonic()

GATEWAY_INFO = {
    "name": "poly-db-mcp-gateway",
    "version": "0.0.2",
    "description": "MCP Database Execution Gateway",
}


def configure_gateway(proxy: ProxyClient) -> None:
    global _proxy, _route_table, _auth_registry
    _proxy = proxy
    _route_table = proxy._route_table
    if _auth_registry is None:
        _auth_registry = ClientRegistry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cleanup_sessions() -> None:
    global _last_session_cleanup
    now = time.monotonic()
    if now - _last_session_cleanup < 300:
        return
    _last_session_cleanup = now
    if len(_sse_sessions) > 1000:
        for sid in list(_sse_sessions.keys())[:100]:
            _sse_sessions.pop(sid, None)


def _extract_bearer_token(api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    return api_key[7:] if api_key.startswith("Bearer ") else api_key


def _validate_api_key(api_key: Optional[str]) -> bool:
    """True if API key is valid in Gateway's own ClientRegistry."""
    if _auth_registry is None:
        return False
    token = _extract_bearer_token(api_key)
    if not token:
        return False
    return _auth_registry.validate_api_key(token) is not None


def _strip_auth_headers(headers: dict) -> dict:
    """Remove auth-related headers before forwarding to Node."""
    stripped = dict(headers)
    for h in ("authorization", "x-api-key"):
        stripped.pop(h, None)
    return stripped


async def _proxy_request(req: Request, path: str, db_type: str,
                         version: str) -> Response:
    """Forward to Node(s) with failover."""
    if _proxy is None or _route_table is None:
        return JSONResponse({"status": "error", "message": "Gateway not configured"}, status_code=500)

    routes = _route_table.lookup(db_type, version)
    if not routes:
        return JSONResponse(
            {"status": "error", "message": f"No node for {db_type}/{version}"}, status_code=404)

    body_bytes = await req.body() if req.method in ("POST", "PUT", "PATCH") else None
    try:
        resp = await _proxy.forward(
            routes, req.method, path,
            headers=_strip_auth_headers(dict(req.headers)),
            body=body_bytes,
            params=dict(req.query_params),
        )
    except NodeUnreachableError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=502)

    return Response(content=resp.content, status_code=resp.status_code,
                    headers=dict(resp.headers))


async def _scatter_get(path: str, req: Request) -> dict:
    if _proxy is None:
        return {"status": "error", "message": "Gateway not configured"}
    return await _proxy.scatter("GET", path, headers=_strip_auth_headers(dict(req.headers)))


async def _scatter_jsonrpc(body: dict, req: Request) -> dict:
    """Scatter a JSON-RPC request (POST) to all Nodes."""
    if _proxy is None:
        return {"status": "error", "message": "Gateway not configured"}
    body_bytes = json.dumps(body).encode()
    return await _proxy.scatter("POST", "/mcp",
                                 headers=_strip_auth_headers(dict(req.headers)),
                                 body=body_bytes)


# ---------------------------------------------------------------------------
# Gateway info
# ---------------------------------------------------------------------------

@router.get("/", summary="Gateway root")
async def gateway_root():
    nodes_info = {}
    if _route_table:
        for node in _route_table.list_nodes():
            nodes_info[node["name"]] = {"address": node["address"], "databases": node["databases"]}
    return {**GATEWAY_INFO, "nodes": nodes_info,
            "route_count": _route_table.route_count if _route_table else 0}


@router.api_route("/", methods=["POST"], summary="MCP JSON-RPC 入口 (/)")
async def gateway_mcp_root(req: Request, api_key: Optional[str] = Header(None, alias="Authorization")):
    """Alias for POST /mcp — some MCP clients use POST / as the JSON-RPC endpoint."""
    return await proxy_mcp_jsonrpc(req, api_key)


@router.get("/mcp", summary="MCP service info")
async def gateway_mcp_info():
    if _route_table is None:
        return JSONResponse({"status": "error"}, status_code=500)
    nodes = _route_table.list_nodes()
    return {
        "name": GATEWAY_INFO["name"], "version": GATEWAY_INFO["version"],
        "description": GATEWAY_INFO["description"],
        "route_count": _route_table.route_count, "node_count": len(nodes),
        "nodes": {n["name"]: {"address": n["address"], "databases": n["databases"]} for n in nodes},
    }


# ---------------------------------------------------------------------------
# Transparent proxy (with Gateway-level auth)
# ---------------------------------------------------------------------------

@router.api_route("/api/execute_sql", methods=["POST"])
async def proxy_execute_sql(req: Request, api_key: Optional[str] = Header(None, alias="Authorization")):
    body = await req.json()
    db_type = body.get("db_type") or body.get("dbType")
    version = body.get("version")
    if not db_type or not version:
        return JSONResponse({"status": "error", "message": "db_type and version required"}, status_code=400)
    if api_key and not _validate_api_key(api_key):
        return JSONResponse({"status": "error", "message": "Unauthorized: invalid API key"}, status_code=401)
    return await _proxy_request(req, "/api/execute_sql", db_type, version)


@router.api_route("/api/dify/execute_sql", methods=["POST"])
async def proxy_dify_execute_sql(req: Request, api_key: Optional[str] = Header(None, alias="Authorization")):
    body = await req.json()
    db_type = body.get("db_type") or body.get("dbType")
    version = body.get("version")
    if not db_type or not version:
        return JSONResponse({"status": "error", "message": "db_type and version required"}, status_code=400)
    if api_key and not _validate_api_key(api_key):
        return JSONResponse({"status": "error", "message": "Unauthorized: invalid API key"}, status_code=401)
    return await _proxy_request(req, "/api/dify/execute_sql", db_type, version)


@router.api_route("/api/databases/{db_type}/versions", methods=["GET"])
async def proxy_get_versions(req: Request, db_type: str):
    if _route_table is None:
        return JSONResponse({"status": "error"}, status_code=500)
    for node in _route_table.list_nodes():
        for entry in node["databases"]:
            if entry["db_type"].lower() == db_type.lower():
                return await _proxy_request(req, f"/api/databases/{db_type}/versions",
                                            db_type, entry["versions"][0])
    return JSONResponse({"status": "error", "message": f"No node for {db_type}"}, status_code=404)


@router.api_route("/mcp/call", methods=["POST"])
async def proxy_mcp_call(req: Request, api_key: Optional[str] = Header(None, alias="Authorization")):
    body = await req.json()
    db_type = body.get("db_type")
    version = body.get("version")
    if not db_type or not version:
        return JSONResponse({"status": "error", "message": "db_type and version required"}, status_code=400)
    if api_key and not _validate_api_key(api_key):
        return JSONResponse({"status": "error", "message": "Unauthorized: invalid API key"}, status_code=401)
    return await _proxy_request(req, "/mcp/call", db_type, version)


@router.api_route("/api/shutdown", methods=["POST"])
async def proxy_shutdown(req: Request):
    return JSONResponse(await _scatter_get("/api/shutdown", req))


# ---------------------------------------------------------------------------
# Scatter / aggregate
# ---------------------------------------------------------------------------

@router.get("/api/databases")
async def gather_databases(req: Request):
    results = await _scatter_get("/api/databases", req)
    merged, seen = [], set()
    for node_result in results.values():
        for db_info in node_result.get("data", {}).get("databases", []):
            t = db_info.get("type", "")
            if t and t not in seen:
                seen.add(t); merged.append(db_info)
    return {"databases": merged, "nodes": {n: r.get("status") for n, r in results.items()}}


@router.get("/api/health")
async def gather_health(req: Request):
    results = await _scatter_get("/api/health", req)
    aggregated = {"healthy": True, "nodes": {}}
    for node_name, node_result in results.items():
        aggregated["nodes"][node_name] = node_result
        if node_result.get("status") != "ok":
            aggregated["healthy"] = False
    return aggregated


# ---------------------------------------------------------------------------
# MCP JSON-RPC
# ---------------------------------------------------------------------------

@router.api_route("/mcp", methods=["POST"])
async def proxy_mcp_jsonrpc(req: Request, api_key: Optional[str] = Header(None, alias="Authorization")):
    if _route_table is None:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32603, "message": "Gateway not configured"}}, status_code=500)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, status_code=400)

    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    # Auth: require API key for everything except initialize & notifications
    _auth_required = method not in ("initialize", "notifications/initialized")
    if _auth_required and api_key and not _validate_api_key(api_key):
        return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                             "error": {"code": -32001, "message": "Unauthorized: invalid API key"}})

    if method == "initialize":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "serverInfo": GATEWAY_INFO, "capabilities": {"tools": {}}}})

    if method == "tools/list":
        results = await _scatter_jsonrpc(body, req)
        all_tools, seen_names = [], set()
        for node_result in results.values():
            for tool in node_result.get("data", {}).get("result", {}).get("tools", []):
                name = tool.get("name", "")
                if name and name not in seen_names:
                    seen_names.add(name); all_tools.append(tool)
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": all_tools}})

    if method == "tools/call":
        args = params.get("arguments", {})
        db_type = args.get("db_type")
        version = args.get("version")
        if not db_type or not version:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": -32602, "message": "db_type and version required"}})
        routes = _route_table.lookup(db_type, version)
        if not routes:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": -32602, "message": f"No node for {db_type}/{version}"}})
        body_bytes = json.dumps(body).encode()
        try:
            resp = await _proxy.forward(routes, "POST", "/mcp",
                                        headers=_strip_auth_headers(dict(req.headers)),
                                        body=body_bytes)
            return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        except NodeUnreachableError as e:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}})

    # default fallback
    nodes = _route_table.list_nodes()
    if not nodes:
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})
    first = nodes[0]["databases"][0]
    routes = _route_table.lookup(first["db_type"], first["versions"][0])
    body_bytes = json.dumps(body).encode()
    try:
        resp = await _proxy.forward(routes, "POST", "/mcp",
                                    headers=_strip_auth_headers(dict(req.headers)), body=body_bytes)
        return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
    except NodeUnreachableError:
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": "No nodes available"}})


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

@router.get("/sse")
async def proxy_sse(req: Request, api_key: Optional[str] = Header(None, alias="Authorization")):
    db_type = req.query_params.get("db_type")
    version = req.query_params.get("version")
    if not db_type or not version:
        return JSONResponse({"status": "error", "message": "db_type and version required"}, status_code=400)
    if api_key and not _validate_api_key(api_key):
        return JSONResponse({"status": "error", "message": "Unauthorized: invalid API key"}, status_code=401)
    if _route_table is None:
        return JSONResponse({"status": "error"}, status_code=500)
    routes = _route_table.lookup(db_type, version)
    if not routes:
        return JSONResponse({"status": "error", "message": f"No node for {db_type}/{version}"}, status_code=404)
    primary = routes[0]
    _cleanup_sessions()

    async def _stream():
        try:
            async for chunk in _proxy.forward_stream(primary, "GET", "/sse",
                                                     headers=_strip_auth_headers(dict(req.headers)),
                                                     params=dict(req.query_params)):
                yield chunk
        except NodeUnreachableError:
            yield f"event: error\ndata: {{\"error\": \"Node unreachable: {primary.node}\"}}\n\n".encode()

    return StreamingResponse(_stream(), media_type="text/event-stream")


@router.api_route("/messages", methods=["POST"])
async def proxy_messages(req: Request, api_key: Optional[str] = Header(None, alias="Authorization")):
    session_id = req.query_params.get("session_id", "")
    if api_key and not _validate_api_key(api_key):
        return JSONResponse({"status": "error", "message": "Unauthorized: invalid API key"}, status_code=401)
    if _route_table is None:
        return JSONResponse({"status": "error"}, status_code=500)
    body_bytes = await req.body()

    if session_id and session_id in _sse_sessions:
        _node_name, base_url = _sse_sessions[session_id]
        for routes in _route_table._routes.values():
            if routes and routes[0].base_url == base_url:
                try:
                    resp = await _proxy.forward(routes, "POST", f"/messages?session_id={session_id}",
                                                headers=_strip_auth_headers(dict(req.headers)), body=body_bytes)
                    return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
                except NodeUnreachableError:
                    pass

    for node in _route_table.list_nodes():
        entry = node["databases"][0]
        routes = _route_table.lookup(entry["db_type"], entry["versions"][0])
        if not routes:
            continue
        try:
            resp = await _proxy.forward(routes, "POST", f"/messages?session_id={session_id}",
                                        headers=_strip_auth_headers(dict(req.headers)), body=body_bytes)
            if resp.status_code != 404:
                return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
        except NodeUnreachableError:
            continue

    return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32603, "message": "Session not found"}}, status_code=404)


# ===========================================================================
# OAuth & Client Management — handled locally by Gateway
# ===========================================================================

def _require_gateway_auth() -> ClientRegistry:
    if _auth_registry is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Gateway auth not initialized")
    return _auth_registry


# --- /.well-known -----------------------------------------------------------

@router.get("/.well-known/oauth-authorization-server")
async def oauth_discovery(req: Request):
    base_url = str(req.base_url).rstrip("/")
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
    }


# --- OAuth DCR (RFC 7591) ---------------------------------------------------

from pydantic import BaseModel, Field
from typing import List, Optional as Opt


class OAuthRegisterRequest(BaseModel):
    client_name: str = "Dify MCP Client"
    redirect_uris: List[str] = Field(default_factory=list)
    grant_types: Opt[List[str]] = None
    response_types: Opt[List[str]] = None


@router.post("/register")
async def oauth_register(request: OAuthRegisterRequest):
    if not request.redirect_uris:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "redirect_uris is required"})
    result = _require_gateway_auth().register_oauth_client(
        client_name=request.client_name,
        redirect_uris=request.redirect_uris,
        grant_types=request.grant_types,
        response_types=request.response_types,
    )
    return JSONResponse(status_code=201, content=result)


# --- OAuth authorization ----------------------------------------------------

from urllib.parse import urlencode, quote

@router.get("/authorize")
async def oauth_authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: Opt[str] = None,
    scope: Opt[str] = None,
    code_challenge: Opt[str] = None,
    code_challenge_method: Opt[str] = None,
):
    if response_type != "code":
        return RedirectResponse(
            url=f"{redirect_uri}?error=unsupported_response_type"
            + (f"&state={quote(state)}" if state else ""))

    client = _require_gateway_auth().get_client(client_id)
    if not client or not client.is_active:
        return RedirectResponse(
            url=f"{redirect_uri}?error=invalid_client"
            + (f"&state={quote(state)}" if state else ""))

    allowed = client.metadata.get("redirect_uris", [])
    if allowed and redirect_uri not in allowed:
        return RedirectResponse(
            url=f"{redirect_uri}?error=invalid_redirect_uri"
            + (f"&state={quote(state)}" if state else ""))

    code = _require_gateway_auth().create_authorization_code(client_id, redirect_uri)
    if not code:
        return RedirectResponse(
            url=f"{redirect_uri}?error=server_error"
            + (f"&state={quote(state)}" if state else ""))

    params = {"code": code}
    if state:
        params["state"] = state
    return RedirectResponse(url=f"{redirect_uri}?{urlencode(params)}")


# --- OAuth token exchange ---------------------------------------------------

from fastapi import Form

@router.post("/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: Opt[str] = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    redirect_uri: Opt[str] = Form(None),
    code_verifier: Opt[str] = Form(None),
):
    if grant_type != "authorization_code":
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
    if not code:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "code required"})

    api_key = _require_gateway_auth().exchange_authorization_code(code, client_id, client_secret)
    if not api_key:
        return JSONResponse(status_code=400, content={"error": "invalid_grant",
                            "error_description": "Invalid or expired authorization code"})
    return {"access_token": api_key, "token_type": "Bearer", "expires_in": 3600}


# --- Client management ------------------------------------------------------

from ..client_registry import (
    ClientRegisterRequest, ClientRegisterResponse,
    ClientListResponse, ClientOperationResponse, ClientRotateKeyResponse,
)

@router.post("/api/clients/register")
async def gateway_client_register(request: ClientRegisterRequest):
    client_id, api_key = _require_gateway_auth().register(name=request.name, description=request.description)
    return ClientRegisterResponse(client_id=client_id, api_key=api_key, name=request.name,
                                  message="Save the api_key securely, it will not be shown again")


@router.get("/api/clients")
async def gateway_list_clients():
    return ClientListResponse(clients=_require_gateway_auth().list_clients())


@router.delete("/api/clients/{client_id}")
async def gateway_unregister_client(client_id: str):
    if _require_gateway_auth().unregister(client_id):
        return ClientOperationResponse(status="success", message=f"Client {client_id} unregistered", client_id=client_id)
    return JSONResponse(status_code=404, content={"detail": f"Client {client_id} not found"})


@router.patch("/api/clients/{client_id}")
async def gateway_update_client(client_id: str, name: Opt[str] = None, is_active: Opt[bool] = None):
    if _require_gateway_auth().update_client(client_id, name=name, is_active=is_active):
        return ClientOperationResponse(status="success", message=f"Client {client_id} updated", client_id=client_id)
    return JSONResponse(status_code=404, content={"detail": f"Client {client_id} not found"})


@router.post("/api/clients/{client_id}/rotate-key")
async def gateway_rotate_key(client_id: str):
    new_key = _require_gateway_auth().rotate_api_key(client_id)
    if new_key:
        return ClientRotateKeyResponse(client_id=client_id, new_api_key=new_key,
                                       message="Save the new api_key securely")
    return JSONResponse(status_code=404, content={"detail": f"Client {client_id} not found"})


# --- Dify MCP integration ---------------------------------------------------

@router.post("/console/api/mcp/oauth/callback")
async def gateway_dify_oauth_callback(code: Opt[str] = None, api_key: Opt[str] = Header(None, alias="Authorization")):
    reg = _require_gateway_auth()
    if api_key:
        token = _extract_bearer_token(api_key)
        client_info = reg.validate_api_key(token) if token else None
        if client_info:
            return {"status": "success", "data": {"api_key": token, "client_id": client_info.client_id,
                    "name": client_info.name, "description": client_info.description, "type": "api_key"}}
        return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
    if code:
        client_id, new_key = reg.register(name=f"dify-oauth-{code[:8]}", description="Dify OAuth registered client")
        return {"status": "success", "data": {"api_key": new_key, "client_id": client_id,
                "name": f"dify-oauth-{code[:8]}", "type": "oauth"}}
    return JSONResponse(status_code=400, content={"detail": "Missing authorization"})
