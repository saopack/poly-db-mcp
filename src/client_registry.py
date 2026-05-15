"""
客户端注册表模块

提供动态客户端注册、注销、认证等功能，支持多租户场景。
"""

import uuid
import secrets
import threading
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from pydantic import BaseModel, Field
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ClientInfo:
    """客户端信息"""
    client_id: str
    api_key: str
    name: str
    description: Optional[str] = None
    client_secret: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_used: Optional[datetime] = None
    is_active: bool = True
    metadata: Dict = field(default_factory=dict)


@dataclass
class OAuthAuthorizationCode:
    """OAuth 授权码"""
    code: str
    client_id: str
    redirect_uri: str
    api_key: str
    created_at: datetime = field(default_factory=datetime.now)
    expires_at: datetime = field(default_factory=lambda: datetime.now() + timedelta(minutes=10))
    used: bool = False


class ClientRegistry:
    """客户端注册表，支持 API Key 和 OAuth 两种注册方式（线程安全）"""

    def __init__(self):
        self._lock = threading.Lock()
        self._clients: Dict[str, ClientInfo] = {}
        self._api_key_to_client: Dict[str, str] = {}
        self._client_secret_to_client: Dict[str, str] = {}
        self._auth_codes: Dict[str, OAuthAuthorizationCode] = {}

    def _cleanup_expired_auth_codes(self) -> int:
        """Remove expired authorization codes. Returns count removed."""
        now = datetime.now()
        expired = [
            code for code, ac in self._auth_codes.items()
            if now > ac.expires_at
        ]
        for code in expired:
            del self._auth_codes[code]
        if expired:
            logger.debug(f"Cleaned up {len(expired)} expired OAuth auth codes")
        return len(expired)

    def register(self, name: str, description: Optional[str] = None) -> tuple[str, str]:
        """注册新客户端，返回(client_id, api_key)"""
        client_id = str(uuid.uuid4())
        api_key = f"mcp-{secrets.token_urlsafe(32)}"

        client_info = ClientInfo(
            client_id=client_id,
            api_key=api_key,
            name=name,
            description=description
        )

        with self._lock:
            self._cleanup_expired_auth_codes()
            self._clients[client_id] = client_info
            self._api_key_to_client[api_key] = client_id

        return client_id, api_key

    def register_oauth_client(
        self,
        client_name: str,
        redirect_uris: List[str],
        grant_types: Optional[List[str]] = None,
        response_types: Optional[List[str]] = None,
    ) -> Dict:
        """OAuth 动态客户端注册 (DCR, RFC 7591)"""
        client_id = str(uuid.uuid4())
        client_secret = secrets.token_urlsafe(32)
        api_key = f"mcp-{secrets.token_urlsafe(32)}"

        client_info = ClientInfo(
            client_id=client_id,
            api_key=api_key,
            name=client_name,
            description=f"OAuth client (redirects: {', '.join(redirect_uris)})",
            client_secret=client_secret,
            metadata={
                "redirect_uris": redirect_uris,
                "grant_types": grant_types or ["authorization_code"],
                "response_types": response_types or ["code"],
            }
        )

        with self._lock:
            self._cleanup_expired_auth_codes()
            self._clients[client_id] = client_info
            self._api_key_to_client[api_key] = client_id
            self._client_secret_to_client[client_secret] = client_id

        logger.info(f"OAuth client registered: {client_id} ({client_name})")

        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "grant_types": grant_types or ["authorization_code"],
            "response_types": response_types or ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }

    def create_authorization_code(self, client_id: str, redirect_uri: str) -> Optional[str]:
        """创建 OAuth 授权码"""
        with self._lock:
            self._cleanup_expired_auth_codes()
            client_info = self._clients.get(client_id)
            if not client_info or not client_info.is_active:
                return None

            code = f"mcp-auth-{secrets.token_urlsafe(32)}"
            auth_code = OAuthAuthorizationCode(
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                api_key=client_info.api_key,
            )
            self._auth_codes[code] = auth_code
            return code

    def exchange_authorization_code(
        self, code: str, client_id: str, client_secret: str
    ) -> Optional[str]:
        """用授权码换取 API Key (access token)"""
        with self._lock:
            self._cleanup_expired_auth_codes()

            stored_client_id = self._client_secret_to_client.get(client_secret)
            if not stored_client_id or stored_client_id != client_id:
                logger.warning(f"OAuth token exchange: invalid client credentials for {client_id}")
                return None

            auth_code = self._auth_codes.get(code)
            if not auth_code:
                logger.warning(f"OAuth token exchange: unknown code {code[:12]}...")
                return None

            if auth_code.used:
                logger.warning(f"OAuth token exchange: code {code[:12]}... already used")
                return None

            if auth_code.client_id != client_id:
                logger.warning(f"OAuth token exchange: client_id mismatch for code {code[:12]}...")
                return None

            if datetime.now() > auth_code.expires_at:
                logger.warning(f"OAuth token exchange: code {code[:12]}... expired")
                del self._auth_codes[code]
                return None

            auth_code.used = True
            return auth_code.api_key

    def unregister(self, client_id: str) -> bool:
        """注销客户端"""
        with self._lock:
            if client_id not in self._clients:
                return False

            client_info = self._clients[client_id]
            del self._api_key_to_client[client_info.api_key]
            if client_info.client_secret:
                self._client_secret_to_client.pop(client_info.client_secret, None)
            del self._clients[client_id]
            return True

    def validate_api_key(self, api_key: str) -> Optional[ClientInfo]:
        """验证API Key并返回客户端信息"""
        with self._lock:
            client_id = self._api_key_to_client.get(api_key)
            if not client_id:
                return None

            client_info = self._clients.get(client_id)
            if not client_info or not client_info.is_active:
                return None

            client_info.last_used = datetime.now()
            return client_info

    def get_client(self, client_id: str) -> Optional[ClientInfo]:
        """根据client_id获取客户端信息"""
        with self._lock:
            return self._clients.get(client_id)

    def list_clients(self) -> List[Dict]:
        """列出所有客户端（不包含API Key和client_secret）"""
        with self._lock:
            return [
                {
                    "client_id": client.client_id,
                    "name": client.name,
                    "description": client.description,
                    "created_at": client.created_at.isoformat(),
                    "last_used": client.last_used.isoformat() if client.last_used else None,
                    "is_active": client.is_active
                }
                for client in self._clients.values()
            ]

    def update_client(self, client_id: str, **kwargs) -> bool:
        """更新客户端信息"""
        with self._lock:
            if client_id not in self._clients:
                return False

            client_info = self._clients[client_id]
            if "name" in kwargs:
                client_info.name = kwargs["name"]
            if "description" in kwargs:
                client_info.description = kwargs["description"]
            if "is_active" in kwargs:
                client_info.is_active = kwargs["is_active"]
            return True

    def rotate_api_key(self, client_id: str) -> Optional[str]:
        """轮换API Key"""
        with self._lock:
            if client_id not in self._clients:
                return None

            client_info = self._clients[client_id]
            old_api_key = client_info.api_key
            del self._api_key_to_client[old_api_key]

            new_api_key = f"mcp-{secrets.token_urlsafe(32)}"
            client_info.api_key = new_api_key
            self._api_key_to_client[new_api_key] = client_id
            return new_api_key


# 全局注册表实例
_global_registry = ClientRegistry()


def get_registry() -> ClientRegistry:
    """获取全局注册表实例"""
    return _global_registry


class ClientRegisterRequest(BaseModel):
    """客户端注册请求"""
    name: str = Field(..., min_length=1, max_length=100, description="客户端名称")
    description: Optional[str] = Field(None, max_length=500, description="客户端描述")


class ClientRegisterResponse(BaseModel):
    """客户端注册响应"""
    client_id: str
    api_key: str
    name: str
    message: str = "Please save the api_key securely, it will not be shown again"


class ClientListResponse(BaseModel):
    """客户端列表响应"""
    clients: List[Dict]


class ClientOperationResponse(BaseModel):
    """客户端操作响应"""
    status: str
    message: str
    client_id: Optional[str] = None


class ClientRotateKeyResponse(BaseModel):
    """API Key轮换响应"""
    client_id: str
    new_api_key: str
    message: str = "Please save the new api_key securely, it will not be shown again"