"""ProxyClient — async HTTP proxy with automatic failover across Node replicas."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from .router import Route, RouteTable

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 3600       # total timeout for long-running requests (SQL, cold start)
DEFAULT_CONNECT_TIMEOUT = 10  # TCP connect timeout — only used to detect dead Nodes


class NodeUnreachableError(Exception):
    """Raised when ALL backend Nodes for a route are unreachable."""

    def __init__(self, node: str = "", address: str = "", detail: str = "",
                 tried: int = 0):
        if node and address:
            msg = f"Node '{node}' ({address}) unreachable"
        else:
            msg = "All target nodes unreachable"
        if detail:
            msg = f"{msg}: {detail}"
        if tried > 1:
            msg = f"{msg} (tried {tried} node(s))"
        super().__init__(msg)
        self.node = node
        self.address = address
        self.tried = tried


class ProxyClient:
    """Manages a pool of ``httpx.AsyncClient`` instances, one per backend
    Node base URL, and provides forward / scatter / forward_stream helpers
    with automatic failover across replicas.

    Usage::

        proxy = ProxyClient(route_table)
        routes = route_table.lookup("postgresql", "14")
        resp = await proxy.forward(routes, "POST", "/api/execute_sql",
                                   headers, body_bytes)
    """

    def __init__(
        self,
        route_table: RouteTable,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        retry_on_error: bool = False,
    ):
        self._route_table = route_table
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._retry_on_error = retry_on_error
        self._clients: Dict[str, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    # ---- client management -------------------------------------------------

    async def _get_client(self, base_url: str) -> httpx.AsyncClient:
        if base_url not in self._clients:
            async with self._lock:
                if base_url not in self._clients:
                    self._clients[base_url] = httpx.AsyncClient(
                        base_url=base_url,
                        timeout=httpx.Timeout(
                            timeout=self._timeout,
                            connect=self._connect_timeout,
                        ),
                    )
        return self._clients[base_url]

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ---- forward (with failover) ------------------------------------------

    async def _forward_one(
        self,
        route: Route,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Send a request to a *single* Node.  Raises NodeUnreachableError on failure."""
        client = await self._get_client(route.base_url)
        clean_headers = _filter_proxy_headers(headers or {})

        try:
            return await client.request(
                method=method,
                url=path,
                headers=clean_headers,
                content=body,
                params=params,
            )
        except httpx.TimeoutException:
            logger.warning("ProxyClient: timeout talking to %s %s", route.node, path)
            raise NodeUnreachableError(route.node, route.address, "timeout")
        except httpx.ConnectError:
            logger.warning("ProxyClient: connect refused for %s", route.node)
            raise NodeUnreachableError(route.node, route.address, "connection refused")
        except Exception:
            logger.exception("ProxyClient: error proxying %s %s", method, path)
            raise NodeUnreachableError(route.node, route.address)

    async def forward(
        self,
        routes: List[Route],
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Proxy a request, trying *routes* in priority order until one succeeds.

        If all routes fail the last ``NodeUnreachableError`` is raised.

        Args:
            routes: ordered list of candidate Nodes (primary first, then failover).
            method: HTTP method.
            path: URL path on the target Node.
            headers: request headers to forward.
            body: raw request body bytes.
            params: query string parameters.

        Returns:
            ``httpx.Response`` from the first reachable Node.
        """
        if not routes:
            raise ValueError("forward() called with empty routes list")

        last_error = None
        tried = 0
        for i, route in enumerate(routes):
            try:
                if i > 0:
                    logger.info(
                        "ProxyClient: failover → trying %s (%s) for %s %s",
                        route.node, route.address, method, path,
                    )
                resp = await self._forward_one(route, method, path, headers, body, params)
                return resp
            except NodeUnreachableError as e:
                last_error = e
                tried = i + 1
                if i < len(routes) - 1:
                    logger.warning(
                        "ProxyClient: %s unreachable, trying next replica ...",
                        route.node,
                    )

        # All routes failed
        raise NodeUnreachableError(
            detail=str(last_error) if last_error else "",
            tried=tried,
        )

    async def forward_or_error(
        self,
        routes: List[Route],
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        params: Optional[Dict[str, str]] = None,
    ) -> dict:
        """Like :meth:`forward` but returns a status dict for Gateway responses."""
        if not routes:
            return {"status": "error", "message": "No routes available"}
        try:
            resp = await self.forward(routes, method, path, headers, body, params)
            try:
                data = resp.json() if resp.content else {}
            except Exception:
                data = {"raw": resp.text[:2000]}
            return {
                "status": "ok" if resp.is_success else "error",
                "status_code": resp.status_code,
                "data": data,
            }
        except NodeUnreachableError as e:
            return {
                "status": "error",
                "message": str(e),
                "tried": e.tried,
            }

    # ---- scatter (broadcast to all nodes) ----------------------------------

    async def scatter(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
    ) -> Dict[str, dict]:
        """Broadcast the same request to **all** Nodes, returning per-node results.

        Partial failures are captured individually.
        """
        nodes = self._route_table.list_nodes()
        if not nodes:
            return {}

        clean_headers = _filter_proxy_headers(headers or {})

        async def _fetch(node: dict) -> tuple:
            name = node["name"]
            base_url = node["base_url"]
            try:
                client = await self._get_client(base_url)
                resp = await client.request(
                    method=method,
                    url=path,
                    headers=clean_headers,
                    content=body,
                )
                try:
                    data = resp.json() if resp.content else {}
                except Exception:
                    data = {"raw": resp.text[:2000]}
                return name, {
                    "status": "ok" if resp.is_success else "error",
                    "status_code": resp.status_code,
                    "data": data,
                }
            except Exception as e:
                logger.warning("ProxyClient: scatter failed for %s: %s", name, e)
                return name, {
                    "status": "error",
                    "message": str(e),
                }

        tasks = [asyncio.create_task(_fetch(n)) for n in nodes]
        results = {}
        for task in asyncio.as_completed(tasks):
            name, result = await task
            results[name] = result
        return results

    # ---- stream (SSE passthrough) — first route only, no failover --------

    async def forward_stream(
        self,
        route: Route,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
        params: Optional[Dict[str, str]] = None,
    ):
        """Proxy a streaming request and yield raw bytes as they arrive.

        Uses the *primary* Route only — failover mid-stream is not supported.
        """
        client = await self._get_client(route.base_url)
        clean_headers = _filter_proxy_headers(headers or {})

        try:
            async with client.stream(
                method=method,
                url=path,
                headers=clean_headers,
                content=body,
                params=params,
            ) as response:
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.TimeoutException:
            logger.warning("ProxyClient: stream timeout for %s %s", route.node, path)
            raise NodeUnreachableError(route.node, route.address, "stream timeout")
        except httpx.ConnectError:
            raise NodeUnreachableError(route.node, route.address, "connection refused")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Headers that must be *removed* before forwarding.
# - hop-by-hop headers (RFC 2616 §13.5.1)
# - content-length / content-encoding: the proxy may re-serialise the body
#   or change the HTTP method (e.g. POST→GET for scatter), so let httpx
#   compute the correct value from the actual body bytes.
# - host: refers to the Gateway, not the backend Node.
_STRIP_HEADERS = {
    "connection", "content-encoding", "content-length", "host",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _filter_proxy_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _STRIP_HEADERS
    }
