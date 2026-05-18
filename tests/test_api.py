import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from src.api import app
from src.config_manager import ConfigManager

client = TestClient(app)


class TestAPI:
    def setup_method(self):
        ConfigManager._config = {}
        ConfigManager.load_config()

    def test_health_check(self):
        response = client.get("/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ("healthy", "degraded")
        assert "checks" in data
        assert "config" in data["checks"]
        assert "docker" in data["checks"]
        assert "databases" in data["checks"]

    def test_get_databases(self):
        response = client.get("/api/databases")
        assert response.status_code == 200
        data = response.json()
        assert 'databases' in data
        db_types = [d['type'] for d in data['databases']]
        assert 'postgresql' in db_types
        # each entry has type and versions
        for entry in data['databases']:
            assert 'type' in entry
            assert 'versions' in entry
            assert isinstance(entry['versions'], list)

    def test_get_db_versions(self):
        response = client.get("/api/databases/postgresql/versions")
        assert response.status_code == 200
        data = response.json()
        assert data['db_type'] == 'postgresql'
        assert '14' in data['versions']

    def test_get_db_versions_not_found(self):
        response = client.get("/api/databases/unknown/versions")
        assert response.status_code == 404

    def test_validate_empty_query(self):
        response = client.post("/api/execute_sql", json={
            "db_type": "postgresql",
            "version": "14",
            "query": ""
        })
        assert response.status_code == 422

    def test_validate_whitespace_query(self):
        response = client.post("/api/execute_sql", json={
            "db_type": "postgresql",
            "version": "14",
            "query": "   "
        })
        assert response.status_code == 422

    def test_validate_query_too_long(self):
        response = client.post("/api/execute_sql", json={
            "db_type": "postgresql",
            "version": "14",
            "query": "SELECT 1" * 1000
        })
        assert response.status_code == 422

    @patch('src.routes.validation_routes.MCPExecutor')
    def test_validate_sql_success(self, MockExecutor):
        mock_executor = MockExecutor.return_value
        mock_executor.run_validation.return_value = {
            "status": "success",
            "data": {"columns": ["id"], "rows": [{"id": 1}], "row_count": 1}
        }

        response = client.post("/api/execute_sql", json={
            "db_type": "postgresql",
            "version": "14",
            "query": "SELECT 1"
        })
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'success'
