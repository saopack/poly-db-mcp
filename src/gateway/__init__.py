"""Gateway module — lightweight routing proxy for the poly-db-mcp distributed architecture.

The Gateway is a stateless HTTP proxy that:
- Reads routing.yaml to map (db_type, version) → target Node
- Forwards single-Node requests transparently
- Scatters aggregation requests to all Nodes
- Handles MCP JSON-RPC routing and SSE session passthrough

It does NOT manage Docker containers, connection pools, or SQL execution.
"""
