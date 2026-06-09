"""Tests for gateway RouteTable."""

import pytest
from src.gateway.router import RouteTable, Route, RouteNotFoundError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_config() -> dict:
    return {
        "gateway": {"host": "0.0.0.0", "port": 8000},
        "nodes": {
            "node-pg": {
                "address": "192.168.1.10:8000",
                "databases": [
                    {"db_type": "postgresql", "versions": [12, 13, 14]},
                    {"db_type": "vastbase", "versions": ["3.0.8", "3.0.9", "2.2.15"]},
                    {"db_type": "kingbase", "versions": ["V8", "V9"]},
                ],
            },
            "node-oracle": {
                "address": "192.168.1.11:8000",
                "databases": [
                    {"db_type": "oracle", "versions": ["11c", "12c", "18c", "21c"]},
                ],
            },
            "node-mysql": {
                "address": "192.168.1.12:8000",
                "databases": [
                    {"db_type": "mysql", "versions": ["5.6", "5.7", "8.0"]},
                    {"db_type": "sqlserver", "versions": ["2017", "2019"]},
                ],
            },
        },
    }


@pytest.fixture
def table(sample_config) -> RouteTable:
    return RouteTable.from_config(sample_config)


# ---------------------------------------------------------------------------
# RouteTable construction
# ---------------------------------------------------------------------------

class TestRouteTableBuild:
    def test_from_config_creates_routes(self, table):
        assert table.unique_route_count == 17  # 3+3+2 + 4 + 3+2 unique keys
        assert table.route_count == 17  # no replicas → same as unique
        assert table.node_count == 3

    def test_flat_mapping(self, table):
        routes = table.lookup("postgresql", "12")
        assert len(routes) == 1
        assert routes[0].address == "192.168.1.10:8000"
        assert routes[0].node == "node-pg"

    def test_rep(self, table):
        rep = repr(table)
        assert "RouteTable" in rep
        assert "3 nodes" in rep
        assert "17 routes" in rep


# ---------------------------------------------------------------------------
# lookup — returns list
# ---------------------------------------------------------------------------

class TestLookup:
    def test_exact_match_lowercase(self, table):
        routes = table.lookup("postgresql", "14")
        assert len(routes) == 1
        assert routes[0].node == "node-pg"

    def test_exact_match_mixed_case(self, table):
        routes = table.lookup("PostgreSQL", "14")
        assert routes[0].node == "node-pg"

    def test_v_prefix_stripped(self, table):
        routes = table.lookup("kingbase", "V8")
        assert routes[0].node == "node-pg"

    def test_oracle_21c(self, table):
        routes = table.lookup("oracle", "21c")
        assert routes[0].node == "node-oracle"

    def test_mysql_8(self, table):
        routes = table.lookup("mysql", "8.0")
        assert routes[0].node == "node-mysql"

    def test_sqlserver(self, table):
        routes = table.lookup("sqlserver", "2019")
        assert routes[0].node == "node-mysql"

    def test_vastbase_version(self, table):
        routes = table.lookup("vastbase", "3.0.9")
        assert routes[0].node == "node-pg"


class TestLookupFailure:
    def test_unknown_db_type(self, table):
        routes = table.lookup("clickhouse", "21.8")
        assert routes == []

    def test_unknown_version_falls_back_to_db_type(self, table):
        """Unknown version of known db_type → db_type-level fallback."""
        routes = table.lookup("postgresql", "99")
        # Falls back to any node serving postgresql
        assert len(routes) == 1
        assert routes[0].node == "node-pg"

    def test_lookup_or_none_returns_none(self, table):
        assert table.lookup_or_none("clickhouse", "1.0") is None

    def test_lookup_or_none_returns_route(self, table):
        r = table.lookup_or_none("oracle", "18c")
        assert r is not None
        assert r.node == "node-oracle"


# ---------------------------------------------------------------------------
# Replica / failover
# ---------------------------------------------------------------------------

@pytest.fixture
def replica_config() -> dict:
    """Two nodes declare the same (postgresql, 14)."""
    return {
        "nodes": {
            "node-primary": {
                "address": "10.0.0.1:8000",
                "databases": [
                    {"db_type": "postgresql", "versions": [14]},
                ],
            },
            "node-backup": {
                "address": "10.0.0.2:8000",
                "databases": [
                    {"db_type": "postgresql", "versions": [14]},
                ],
            },
        },
    }


@pytest.fixture
def replica_table(replica_config) -> RouteTable:
    return RouteTable.from_config(replica_config)


class TestReplicas:
    def test_multiple_routes_for_same_key(self, replica_table):
        routes = replica_table.lookup("postgresql", "14")
        assert len(routes) == 2
        # Primary first (declared first in config)
        assert routes[0].node == "node-primary"
        assert routes[0].address == "10.0.0.1:8000"
        # Backup second
        assert routes[1].node == "node-backup"
        assert routes[1].address == "10.0.0.2:8000"

    def test_route_counts_with_replicas(self, replica_table):
        """unique_route_count counts keys; route_count counts all mappings."""
        assert replica_table.unique_route_count == 1
        assert replica_table.route_count == 2  # 2 nodes × 1 version

    def test_lookup_or_none_gives_primary(self, replica_table):
        r = replica_table.lookup_or_none("postgresql", "14")
        assert r is not None
        assert r.node == "node-primary"


class TestMultiReplicaWithGaps:
    """node-A has pg:14, node-B has pg:14+15 — pg:14 has 2 replicas, pg:15 has 1."""

    @pytest.fixture
    def table(self):
        return RouteTable.from_config({
            "nodes": {
                "node-a": {
                    "address": "10.0.0.1:8000",
                    "databases": [
                        {"db_type": "postgresql", "versions": [14]},
                    ],
                },
                "node-b": {
                    "address": "10.0.0.2:8000",
                    "databases": [
                        {"db_type": "postgresql", "versions": [14, 15]},
                    ],
                },
            },
        })

    def test_pg14_has_two_replicas(self, table):
        routes = table.lookup("postgresql", "14")
        assert len(routes) == 2
        assert routes[0].node == "node-a"  # declared first
        assert routes[1].node == "node-b"

    def test_pg15_has_one_replica(self, table):
        routes = table.lookup("postgresql", "15")
        assert len(routes) == 1
        assert routes[0].node == "node-b"


# ---------------------------------------------------------------------------
# Node listing
# ---------------------------------------------------------------------------

class TestNodeListing:
    def test_list_nodes(self, table):
        nodes = table.list_nodes()
        assert len(nodes) == 3
        node_names = {n["name"] for n in nodes}
        assert node_names == {"node-pg", "node-oracle", "node-mysql"}

    def test_list_node_urls(self, table):
        urls = table.list_node_urls()
        assert len(urls) == 3

    def test_get_node_dbs(self, table):
        dbs = table.get_node_dbs("node-oracle")
        assert len(dbs) == 1
        assert dbs[0]["db_type"] == "oracle"


# ---------------------------------------------------------------------------
# Lookup tiers: exact → prefix → db_type fallback
# ---------------------------------------------------------------------------

@pytest.fixture
def vastbase_config() -> dict:
    """Simulates routing.yaml with vastbase standard versions declared."""
    return {
        "nodes": {
            "node-pg": {
                "address": "192.168.1.10:8000",
                "databases": [
                    {"db_type": "vastbase", "versions": ["3.0.8", "3.0.9", "2.2.15"]},
                ],
            },
        },
    }


@pytest.fixture
def vastbase_table(vastbase_config) -> RouteTable:
    return RouteTable.from_config(vastbase_config)


class TestVersionPrefixMatch:
    """Tier 2: ephemeral PSU/build versions should prefix-match the declared version."""

    def test_psu_version_matches_base(self, vastbase_table):
        """3.0.9.psu01 → prefixes 3.0.9"""
        routes = vastbase_table.lookup("vastbase", "3.0.9.psu01")
        assert len(routes) == 1
        assert routes[0].node == "node-pg"

    def test_build_number_matches_base(self, vastbase_table):
        """3.0.8.29475 → prefixes 3.0.8"""
        routes = vastbase_table.lookup("vastbase", "3.0.8.29475")
        assert len(routes) == 1
        assert routes[0].node == "node-pg"

    def test_psu0_matches_base(self, vastbase_table):
        """2.2.15.psu11 → prefixes 2.2.15"""
        routes = vastbase_table.lookup("vastbase", "2.2.15.psu11")
        assert len(routes) == 1

    def test_v_prefix_psu(self, vastbase_table):
        """v3.0.9.psu01 → strips v, then prefixes 3.0.9"""
        routes = vastbase_table.lookup("vastbase", "v3.0.9.psu01")
        assert len(routes) == 1
        assert routes[0].node == "node-pg"

    def test_longest_prefix_wins(self):
        """When both 3.0 and 3.0.9 are declared, prefer 3.0.9 for 3.0.9.psu01."""
        table = RouteTable.from_config({
            "nodes": {
                "node-a": {
                    "address": "10.0.0.1:8000",
                    "databases": [
                        {"db_type": "vastbase", "versions": ["3.0"]},
                    ],
                },
                "node-b": {
                    "address": "10.0.0.2:8000",
                    "databases": [
                        {"db_type": "vastbase", "versions": ["3.0.9"]},
                    ],
                },
            },
        })
        routes = table.lookup("vastbase", "3.0.9.psu01")
        assert len(routes) == 1
        # 3.0.9 is longer than 3.0 → should match node-b
        assert routes[0].node == "node-b"
        assert routes[0].address == "10.0.0.2:8000"


class TestDbTypeFallback:
    """Tier 3: unknown version → route to any node serving this db_type."""

    def test_unknown_version_falls_back(self, vastbase_table):
        """5.0.0 not in config → any vastbase node."""
        routes = vastbase_table.lookup("vastbase", "5.0.0")
        assert len(routes) == 1
        assert routes[0].node == "node-pg"

    def test_unknown_db_type_still_empty(self, vastbase_table):
        """No node serves clickhouse at all."""
        routes = vastbase_table.lookup("clickhouse", "1.0")
        assert routes == []

    def test_custom_build_version(self, vastbase_table):
        """Completely custom version like 'dev-build' → db_type fallback."""
        routes = vastbase_table.lookup("vastbase", "dev-build")
        assert len(routes) == 1
        assert routes[0].node == "node-pg"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_nodes(self):
        table = RouteTable.from_config({"nodes": {}})
        assert table.route_count == 0
        assert table.node_count == 0
        assert table.lookup("anything", "1") == []

    def test_missing_address_raises(self):
        with pytest.raises(ValueError, match="missing an address"):
            RouteTable.from_config({"nodes": {"orphan": {"databases": []}}})

    def test_http_prefix_in_address(self):
        cfg = {"nodes": {"n": {"address": "http://10.0.0.1:9000", "databases": [
            {"db_type": "pg", "versions": ["14"]},
        ]}}}
        table = RouteTable.from_config(cfg)
        routes = table.lookup("pg", "14")
        assert routes[0].base_url == "http://10.0.0.1:9000"


class TestRouteNotFoundError:
    def test_error_message(self):
        e = RouteNotFoundError("mysql", "99")
        assert "mysql" in str(e)
        assert "99" in str(e)
        assert e.db_type == "mysql"
        assert e.version == "99"
