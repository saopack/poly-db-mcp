"""客户端管理和 Dify MCP 集成路由"""
import asyncio
import logging
from typing import Dict, Any, Optional, Union
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..dependencies import get_mcp_handler, get_client_registry
from ..client_registry import (
    ClientRegisterRequest, ClientRegisterResponse,
    ClientListResponse, ClientOperationResponse, ClientRotateKeyResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class DifyToolParameter(BaseModel):
    name: str
    type: str = "string"
    required: bool = True
    description: Optional[str] = None
    default: Optional[Any] = None


class DifyTool(BaseModel):
    name: str
    description: str
    parameters: list[DifyToolParameter] = []


class DifyToolCallRequest(BaseModel):
    tool_name: str
    parameters: Dict[str, Any] = {}


class DifyToolCallResponse(BaseModel):
    status: str
    content: Optional[Union[str, Dict[str, Any]]] = None
    error: Optional[Dict[str, str]] = None


def _extract_bearer_token(api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    return api_key[7:] if api_key.startswith("Bearer ") else api_key


def _require_api_key(api_key: Optional[str]) -> None:
    token = _extract_bearer_token(api_key)
    if not token or not get_client_registry().validate_api_key(token):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API key")


# --- 客户端管理 ---

@router.post("/api/clients/register", summary="注册新客户端", response_model=ClientRegisterResponse)
async def register_client(request: ClientRegisterRequest):
    client_id, api_key = get_client_registry().register(name=request.name, description=request.description)
    return ClientRegisterResponse(client_id=client_id, api_key=api_key, name=request.name,
                                  message="Please save the api_key securely, it will not be shown again")


@router.get("/api/clients", summary="获取客户端列表", response_model=ClientListResponse)
async def list_clients():
    return ClientListResponse(clients=get_client_registry().list_clients())


@router.delete("/api/clients/{client_id}", summary="注销客户端", response_model=ClientOperationResponse)
async def unregister_client(client_id: str):
    if get_client_registry().unregister(client_id):
        return ClientOperationResponse(status="success", message=f"Client {client_id} has been unregistered", client_id=client_id)
    raise HTTPException(status_code=404, detail=f"Client {client_id} not found")


@router.post("/api/clients/{client_id}/rotate-key", summary="轮换API Key", response_model=ClientRotateKeyResponse)
async def rotate_client_key(client_id: str):
    new_api_key = get_client_registry().rotate_api_key(client_id)
    if new_api_key:
        return ClientRotateKeyResponse(client_id=client_id, new_api_key=new_api_key,
                                       message="Please save the new api_key securely, it will not be shown again")
    raise HTTPException(status_code=404, detail=f"Client {client_id} not found")


@router.patch("/api/clients/{client_id}", summary="更新客户端信息", response_model=ClientOperationResponse)
async def update_client(client_id: str, name: Optional[str] = None, is_active: Optional[bool] = None):
    if get_client_registry().update_client(client_id, name=name, is_active=is_active):
        return ClientOperationResponse(status="success", message=f"Client {client_id} has been updated", client_id=client_id)
    raise HTTPException(status_code=404, detail=f"Client {client_id} not found")


# --- Dify MCP 集成 ---

@router.post("/mcp/call", summary="MCP工具调用接口")
async def dify_mcp_call(request: DifyToolCallRequest, api_key: Optional[str] = Header(None, alias="Authorization")):
    if api_key:
        _require_api_key(api_key)
    result = await asyncio.to_thread(
        get_mcp_handler().call_tool, request.tool_name, request.parameters
    )
    if result.status == "success":
        return DifyToolCallResponse(status="success", content=result.content)
    return DifyToolCallResponse(
        status="error",
        error={"type": result.error.type if result.error else "unknown",
               "message": result.error.message if result.error else "Unknown error"}
    )


@router.post("/console/api/mcp/oauth/callback", summary="Dify MCP授权回调")
async def dify_mcp_oauth_callback(code: Optional[str] = None, api_key: Optional[str] = Header(None, alias="Authorization")):
    if api_key:
        token = _extract_bearer_token(api_key)
        client_info = get_client_registry().validate_api_key(token) if token else None
        if client_info:
            return {"status": "success", "data": {"api_key": token, "client_id": client_info.client_id,
                    "name": client_info.name, "description": client_info.description, "type": "api_key"}}
        raise HTTPException(status_code=401, detail="Invalid API key")
    if code:
        client_id, new_api_key = get_client_registry().register(name=f"dify-oauth-{code[:8]}", description="Dify OAuth registered client")
        return {"status": "success", "data": {"api_key": new_api_key, "client_id": client_id,
                "name": f"dify-oauth-{code[:8]}", "type": "oauth"}}
    raise HTTPException(status_code=400, detail="Missing authorization")
