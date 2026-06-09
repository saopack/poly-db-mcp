"""Tests for gateway ProxyClient — failover forwarding, scatter, stream."""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx

from src.gateway.router import RouteTable, Route
from src.gateway.proxy import ProxyClient, NodeUnreachableError, _filter_proxy_headers


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_route_table() -> RouteTable:
    return RouteTable.from_config({
        "nodes": {
            "node-pg": {
                "address": "192.168.1.10:8000",
                "databases": [
                    {"db_type": "postgresql", "versions": [12, 13, 14]},
                ],
            },
            "node-oracle": {
                "address": "192.168.1.11:8000",
                "databases": [
                    {"db_type": "oracle", "versions": ["11c", "21c"]},
                ],
            },
        },
    })


def _make_proxy(rt):
    return ProxyClient(rt, timeout=30)


@pytest.fixture
def pg_routes(sample_route_table):
    return sample_route_table.lookup("postgresql", "14")


# ---------------------------------------------------------------------------
# Header filtering
# ---------------------------------------------------------------------------

class TestFilterProxyHeaders:
    def test_removes_hop_by_hop(self):
        headers = {
            "content-type": "application/json",
            "connection": "keep-alive",
            "transfer-encoding": "chunked",
        }
        filtered = _filter_proxy_headers(headers)
        assert "content-type" in filtered
        assert "connection" not in filtered

    def test_empty_headers(self):
        assert _filter_proxy_headers({}) == {}


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

class TestClientManagement:
    def test_get_client_caches(self, sample_route_table):
        proxy = _make_proxy(sample_route_table)
        async def _run():
            c1 = await proxy._get_client("http://192.168.1.10:8000")
            c2 = await proxy._get_client("http://192.168.1.10:8000")
            assert c1 is c2
            await proxy.close()
        asyncio.run(_run())

    def test_close_cleans_up(self, sample_route_table):
        proxy = _make_proxy(sample_route_table)
        async def _run():
            await proxy._get_client("http://x:8000")
            await proxy.close()
            assert len(proxy._clients) == 0
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Forward (single route)
# ---------------------------------------------------------------------------

def _make_mock_resp(is_success, status_code, json_body):
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.is_success = is_success
    mock_resp.status_code = status_code
    mock_resp.content = json.dumps(json_body).encode()
    mock_resp.json.return_value = json_body
    mock_resp.headers = {}
    return mock_resp


class TestForward:
    def test_forward_success(self, sample_route_table, pg_routes):
        proxy = _make_proxy(sample_route_table)
        mock_resp = _make_mock_resp(True, 200, {"status": "ok"})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = mock_resp

        async def _run():
            with patch.object(proxy, "_get_client", return_value=mock_client):
                resp = await proxy.forward(pg_routes, "POST", "/api/execute_sql",
                                           body=json.dumps({"q": "1"}).encode())
                assert resp.status_code == 200
            await proxy.close()
        asyncio.run(_run())

    def test_forward_timeout(self, sample_route_table, pg_routes):
        proxy = _make_proxy(sample_route_table)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.side_effect = httpx.TimeoutException("timeout")

        async def _run():
            with patch.object(proxy, "_get_client", return_value=mock_client):
                with pytest.raises(NodeUnreachableError, match="timeout"):
                    await proxy.forward(pg_routes, "GET", "/api/health")
            await proxy.close()
        asyncio.run(_run())

    def test_forward_or_error_ok(self, sample_route_table, pg_routes):
        proxy = _make_proxy(sample_route_table)
        mock_resp = _make_mock_resp(True, 200, {"healthy": True})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.request.return_value = mock_resp

        async def _run():
            with patch.object(proxy, "_get_client", return_value=mock_client):
                result = await proxy.forward_or_error(pg_routes, "GET", "/api/health")
                assert result["status"] == "ok"
            await proxy.close()
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Failover
# ---------------------------------------------------------------------------

@pytest.fixture
def failover_table() -> RouteTable:
    return RouteTable.from_config({
        "nodes": {
            "node-primary": {
                "address": "10.0.0.1:8000",
                "databases": [{"db_type": "postgresql", "versions": [14]}],
            },
            "node-backup": {
                "address": "10.0.0.2:8000",
                "databases": [{"db_type": "postgresql", "versions": [14]}],
            },
        },
    })


@pytest.fixture
def failover_routes(failover_table):
    return failover_table.lookup("postgresql", "14")


class TestFailover:
    def test_primary_succeeds_backup_not_called(self, failover_table, failover_routes):
        """When primary responds, backup should never be contacted."""
        proxy = _make_proxy(failover_table)

        primary_resp = _make_mock_resp(True, 200, {"node": "primary"})
        primary_client = AsyncMock(spec=httpx.AsyncClient)
        primary_client.request.return_value = primary_resp

        async def _run():
            with patch.object(proxy, "_get_client") as mock_get:
                mock_get.side_effect = [primary_client]  # only primary needed
                resp = await proxy.forward(failover_routes, "GET", "/api/health")
                assert resp.json()["node"] == "primary"
                # Only one call to _get_client (for primary)
                assert mock_get.call_count == 1
            await proxy.close()
        asyncio.run(_run())

    def test_primary_fails_falls_back_to_backup(self, failover_table, failover_routes):
        """When primary is unreachable, failover to backup automatically."""
        proxy = _make_proxy(failover_table)

        primary_client = AsyncMock(spec=httpx.AsyncClient)
        primary_client.request.side_effect = httpx.ConnectError("refused")

        backup_resp = _make_mock_resp(True, 200, {"node": "backup"})
        backup_client = AsyncMock(spec=httpx.AsyncClient)
        backup_client.request.return_value = backup_resp

        async def _run():
            with patch.object(proxy, "_get_client") as mock_get:
                mock_get.side_effect = [primary_client, backup_client]
                resp = await proxy.forward(failover_routes, "GET", "/api/health")
                assert resp.json()["node"] == "backup"
                assert mock_get.call_count == 2  # both contacted
            await proxy.close()
        asyncio.run(_run())

    def test_all_fail_raises(self, failover_table, failover_routes):
        """When all replicas fail, the last error is raised."""
        proxy = _make_proxy(failover_table)

        primary_client = AsyncMock(spec=httpx.AsyncClient)
        primary_client.request.side_effect = httpx.TimeoutException("t/o")

        backup_client = AsyncMock(spec=httpx.AsyncClient)
        backup_client.request.side_effect = httpx.ConnectError("refused")

        async def _run():
            with patch.object(proxy, "_get_client") as mock_get:
                mock_get.side_effect = [primary_client, backup_client]
                with pytest.raises(NodeUnreachableError) as exc:
                    await proxy.forward(failover_routes, "GET", "/api/health")
                assert "tried 2 node" in str(exc.value)
                assert exc.value.tried == 2
            await proxy.close()
        asyncio.run(_run())

    def test_empty_routes_raises(self, sample_route_table):
        proxy = _make_proxy(sample_route_table)
        async def _run():
            with pytest.raises(ValueError, match="empty routes"):
                await proxy.forward([], "GET", "/test")
            await proxy.close()
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Scatter
# ---------------------------------------------------------------------------

class TestScatter:
    def test_scatter_aggregates(self, sample_route_table):
        proxy = _make_proxy(sample_route_table)

        async def _run():
            client1 = AsyncMock(spec=httpx.AsyncClient)
            client1.request.return_value = _make_mock_resp(True, 200, {"healthy": True, "containers": 5})
            client2 = AsyncMock(spec=httpx.AsyncClient)
            client2.request.return_value = _make_mock_resp(True, 200, {"healthy": True, "containers": 3})

            with patch.object(proxy, "_get_client") as mock_get:
                mock_get.side_effect = [client1, client2]
                result = await proxy.scatter("GET", "/api/health")
            assert len(result) == 2
            for name in ("node-pg", "node-oracle"):
                assert result[name]["status"] == "ok"
            await proxy.close()
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------

class TestForwardStream:
    def test_forward_stream_yields(self, sample_route_table, pg_routes):
        proxy = _make_proxy(sample_route_table)

        async def _aiter_bytes():
            for chunk in [b"data: e1\n\n", b"data: e2\n\n"]:
                yield chunk

        mock_resp_ctx = MagicMock()
        mock_resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp_ctx)
        mock_resp_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_resp_ctx.aiter_bytes.return_value = _aiter_bytes()

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.stream = MagicMock(return_value=mock_resp_ctx)

        primary = pg_routes[0]

        async def _run():
            with patch.object(proxy, "_get_client", return_value=mock_client):
                chunks = []
                async for chunk in proxy.forward_stream(primary, "GET", "/sse"):
                    chunks.append(chunk)
            assert len(chunks) == 2
            assert chunks[0] == b"data: e1\n\n"
            await proxy.close()
        asyncio.run(_run())
