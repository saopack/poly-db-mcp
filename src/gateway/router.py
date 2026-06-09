

"""RouteTable — maps (db_type, version) → ordered list of target Node Routes.

Multiple Nodes can declare the same (db_type, version) combination —
the Gateway tries them in configuration order (first = primary, rest = failover).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class Route:
    """A single routing entry pointing to a backend Node."""
    address: str           # "192.168.1.10:8000"
    node: str              # "node-pg"
    base_url: str          # "http://192.168.1.10:8000"


class RouteNotFoundError(Exception):
    """Raised when no Node is configured for a given (db_type, version)."""
    def __init__(self, db_type: str, version: str):
        super().__init__(
            f"No node found for db_type={db_type}, version={version}"
        )
        self.db_type = db_type
        self.version = version


class RouteTable:
    """Immutable routing table built from routing config.

    Each (db_type, version) maps to an *ordered* list of Routes.
    The first entry is the **primary**; subsequent entries are
    **failover** targets (tried in order when the primary is unreachable).

    Usage::

        table = RouteTable.from_config(routing_config)
        routes = table.lookup("postgresql", "14")
        # → [Route("192.168.1.10:8000", "node-pg"),
        #    Route("192.168.1.20:8000", "node-pg-backup")]
    """

    def __init__(self, nodes_info: Dict[str, dict]):
        """
        Args:
            nodes_info: parsed ``nodes`` section from routing.yaml, e.g.::

                {
                  "node-pg": {
                    "address": "192.168.1.10:8000",
                    "databases": [
                      {"db_type": "postgresql", "versions": [12, 13, 14]},
                      ...
                    ]
                  },
                  "node-pg-backup": {
                    "address": "192.168.1.20:8000",
                    "databases": [
                      {"db_type": "postgresql", "versions": [12, 13, 14]},
                    ]
                  },
                  ...
                }
        """
        # Flat map: (db_type_lower, version_lower) → [Route, ...]  (priority order)
        self._routes: Dict[Tuple[str, str], List[Route]] = {}
        self._nodes: Dict[str, dict] = {}  # node_name → {address, base_url, dbs[]}

        for node_name, node_cfg in nodes_info.items():
            address = node_cfg.get("address", "")
            if not address:
                raise ValueError(f"Node '{node_name}' is missing an address")
            base_url = f"http://{address}" if not address.startswith("http") else address
            node_entry = {
                "name": node_name,
                "address": address,
                "base_url": base_url,
                "databases": [],
            }
            for db_entry in node_cfg.get("databases", []):
                db_type = db_entry.get("db_type", "")
                if not db_type:
                    continue
                versions = db_entry.get("versions", [])
                node_entry["databases"].append({
                    "db_type": db_type,
                    "versions": list(versions),
                })
                for version in versions:
                    clean_ver = str(version).lower()
                    if clean_ver.startswith("v"):
                        clean_ver = clean_ver[1:]
                    key = (db_type.lower(), clean_ver)
                    route = Route(
                        address=address,
                        node=node_name,
                        base_url=base_url,
                    )
                    if key not in self._routes:
                        self._routes[key] = []
                    # Append in config order → priority: first declared = primary
                    self._routes[key].append(route)
            self._nodes[node_name] = node_entry

    # ---- lookup -----------------------------------------------------------

    def lookup(self, db_type: str, version: str) -> List[Route]:
        """Return all Routes for *db_type* + *version* in priority order.

        The first entry is the **primary** Node (first declared in config);
        subsequent entries are **failover** targets.

        Returns an empty list when no Node is configured for this combination.
        Callers should check ``if not routes`` to detect missing configurations.

        Matching is case-insensitive and strips leading ``v``/``V`` from the version.
        Falls back through three tiers:

        1. **exact match** — ``(postgresql, 14)``
        2. **prefix match** — ``(vastbase, 3.0.9.psu01)`` matches a node that declares
           ``3.0.9`` because ``3.0.9.psu01`` starts with ``3.0.9.``.
           Also ``(vastbase, 3.0.8.29475)`` matches ``3.0.8``.
        3. **db_type-only match** — any node serving *db_type* (ephemeral/unknown
           versions fall back to the configured node for that database type).
        """
        clean_version = str(version).lower()
        if clean_version.startswith("v"):
            clean_version = clean_version[1:]
        clean_type = str(db_type).lower()

        # 1. exact match
        key = (clean_type, clean_version)
        if key in self._routes:
            return list(self._routes[key])

        # 2. prefix match — query version starts with a declared version
        #    e.g. "3.0.9.psu01" → matches "3.0.9"; "3.0.8.29475" → matches "3.0.8"
        best_prefix: Tuple[int, List[Route]] = (0, [])
        for (dt, ver), routes in self._routes.items():
            if dt == clean_type and _is_version_prefix(clean_version, ver):
                # Prefer the longest prefix match (most specific declared version)
                if len(ver) > best_prefix[0]:
                    best_prefix = (len(ver), routes)
        if best_prefix[1]:
            return list(best_prefix[1])

        # 3. db_type-only fallback — any node serving this db_type
        #    (handles ephemeral Nexus-built versions, custom build numbers, etc.)
        first_match = None
        for (dt, _ver), routes in self._routes.items():
            if dt == clean_type:
                first_match = routes
                break
        if first_match:
            return list(first_match)

        return []

    def lookup_or_none(self, db_type: str, version: str) -> Optional[Route]:
        """Return the **primary** route, or None if no match.

        This is a convenience for code that only needs the first (primary)
        route and doesn't care about failover.
        """
        routes = self.lookup(db_type, version)
        return routes[0] if routes else None

    # ---- aggregation helpers ----------------------------------------------

    def list_nodes(self) -> List[dict]:
        """Return every node (deduplicated by name) with its databases."""
        return list(self._nodes.values())

    def list_node_urls(self) -> List[str]:
        """Return deduplicated base URLs for every Node."""
        seen = {}
        for routes in self._routes.values():
            for route in routes:
                seen[route.base_url] = True
        return sorted(seen.keys())

    def get_node_dbs(self, node_name: str) -> List[dict]:
        """Return the database entries for a given node name."""
        node = self._nodes.get(node_name)
        return node["databases"][:] if node else []

    # ---- introspection ----------------------------------------------------

    @property
    def route_count(self) -> int:
        """Total number of (db_type, version, node) mappings across all replicas."""
        return sum(len(routes) for routes in self._routes.values())

    @property
    def unique_route_count(self) -> int:
        """Number of unique (db_type, version) keys (ignoring replicas)."""
        return len(self._routes)

    @property
    def node_count(self) -> int:
        """Number of distinct nodes."""
        return len(self._nodes)

    def __repr__(self) -> str:
        return f"RouteTable({self.node_count} nodes, {self.route_count} routes)"

    # ---- factory ----------------------------------------------------------

    @classmethod
    def from_config(cls, routing_config: dict) -> "RouteTable":
        """Build a RouteTable from a loaded routing YAML config dict."""
        nodes_cfg = routing_config.get("nodes")
        if nodes_cfg is None:
            raise ValueError("routing config must contain a 'nodes' section")
        return cls(nodes_cfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_version_prefix(query_version: str, route_version: str) -> bool:
    """Return True if *query_version* starts with *route_version* + dot.

    Examples:
        _is_version_prefix("3.0.9.psu01", "3.0.9") → True
        _is_version_prefix("3.0.8.29475", "3.0.8") → True
        _is_version_prefix("12.4", "12") → True
        _is_version_prefix("3.0.9", "3.0.9") → False  (exact match, not prefix)
        _is_version_prefix("12", "12") → False  (exact, not prefix)
    """
    if query_version == route_version:
        return False  # exact match handled earlier
    return query_version.startswith(route_version + ".")
