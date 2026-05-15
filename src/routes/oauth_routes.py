"""OAuth 2.0 路由：DCR、授权、Token 交换"""
import logging
from typing import Optional, List
from urllib.parse import urlencode, quote
from fastapi import APIRouter, Query, Form
from fastapi.responses import RedirectResponse, JSONResponse
from pydantic import BaseModel, Field

from ..dependencies import get_client_registry

logger = logging.getLogger(__name__)
router = APIRouter()


class OAuthRegisterRequest(BaseModel):
    client_name: str = "Dify MCP Client"
    redirect_uris: List[str] = Field(default_factory=list)
    grant_types: Optional[List[str]] = None
    response_types: Optional[List[str]] = None


@router.post("/register", summary="OAuth动态客户端注册 (DCR)")
async def oauth_register(request: OAuthRegisterRequest):
    if not request.redirect_uris:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "redirect_uris is required"})
    result = get_client_registry().register_oauth_client(
        client_name=request.client_name,
        redirect_uris=request.redirect_uris,
        grant_types=request.grant_types,
        response_types=request.response_types,
    )
    return JSONResponse(status_code=201, content=result)


@router.get("/authorize", summary="OAuth授权端点")
async def oauth_authorize(
    response_type: str = Query(..., description="必须为 code"),
    client_id: str = Query(..., description="客户端ID"),
    redirect_uri: str = Query(..., description="回调地址"),
    state: Optional[str] = Query(None),
    scope: Optional[str] = Query(None),
    code_challenge: Optional[str] = Query(None),
    code_challenge_method: Optional[str] = Query(None),
):
    if response_type != "code":
        return RedirectResponse(
            url=f"{redirect_uri}?error=unsupported_response_type&error_description=Only+code+is+supported"
            + (f"&state={quote(state)}" if state else "")
        )
    client_info = get_client_registry().get_client(client_id)
    if not client_info or not client_info.is_active:
        return RedirectResponse(
            url=f"{redirect_uri}?error=invalid_client&error_description=Client+not+found+or+inactive"
            + (f"&state={quote(state)}" if state else "")
        )
    allowed_uris = client_info.metadata.get("redirect_uris", [])
    if allowed_uris and redirect_uri not in allowed_uris:
        return RedirectResponse(
            url=f"{redirect_uri}?error=invalid_redirect_uri&error_description=Redirect+URI+not+registered"
            + (f"&state={quote(state)}" if state else "")
        )
    code = get_client_registry().create_authorization_code(client_id, redirect_uri)
    if not code:
        return RedirectResponse(
            url=f"{redirect_uri}?error=server_error&error_description=Failed+to+create+authorization+code"
            + (f"&state={quote(state)}" if state else "")
        )
    params = {"code": code}
    if state:
        params["state"] = state
    redirect_url = f"{redirect_uri}?{urlencode(params)}"
    return RedirectResponse(url=redirect_url)


@router.post("/token", summary="OAuth Token交换端点")
async def oauth_token(
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
):
    if grant_type != "authorization_code":
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type", "error_description": "Only authorization_code is supported"})
    if not code:
        return JSONResponse(status_code=400, content={"error": "invalid_request", "error_description": "code is required"})
    api_key = get_client_registry().exchange_authorization_code(code, client_id, client_secret)
    if not api_key:
        return JSONResponse(status_code=400, content={"error": "invalid_grant", "error_description": "Invalid or expired authorization code"})
    return {"access_token": api_key, "token_type": "Bearer", "expires_in": 3600}
