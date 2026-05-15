"""共享的模块级单例依赖，供 api.py 和各个路由模块使用。"""
from .mcp import DifyMCPHandler
from .client_registry import get_registry

_mcp_handler = None
_client_registry = None


def get_mcp_handler() -> DifyMCPHandler:
    global _mcp_handler
    if _mcp_handler is None:
        _mcp_handler = DifyMCPHandler()
    return _mcp_handler


def get_client_registry():
    global _client_registry
    if _client_registry is None:
        _client_registry = get_registry()
    return _client_registry
