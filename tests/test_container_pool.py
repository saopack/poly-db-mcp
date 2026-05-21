"""Unit tests for ContainerPool."""
import os
import pytest
import threading
import time
from unittest.mock import Mock, patch, MagicMock

# Reset singleton before each test
import src.container_pool as cp_mod


@pytest.fixture(autouse=True)
def reset_singleton():
    cp_mod.ContainerPool._instance = None
    yield
    cp_mod.ContainerPool._instance = None


class TestContainerKey:
    def test_key_without_compat(self):
        assert cp_mod._make_container_key("postgresql", "14") == "postgresql-14"

    def test_key_with_compat(self):
        assert cp_mod._make_container_key("vastbase", "3.0.8", "A") == "vastbase-3.0.8-a"

    def test_key_case_insensitive(self):
        assert cp_mod._make_container_key("PostgreSQL", "14", "PG") == "postgresql-14-pg"


class TestContainerPoolSingleton:
    def test_same_instance(self):
        a = cp_mod.ContainerPool()
        b = cp_mod.ContainerPool()
        assert a is b

    def test_thread_safe_singleton(self):
        results = []

        def get_instance():
            results.append(cp_mod.ContainerPool())

        threads = [threading.Thread(target=get_instance) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        first = results[0]
        assert all(r is first for r in results)


class TestContainerLease:
    def test_mark_for_destroy(self):
        mock_pool = MagicMock()
        mock_entry = MagicMock()
        mock_conn = MagicMock()

        lease = cp_mod.ContainerLease(mock_pool, mock_entry, mock_conn)
        lease.mark_for_destroy()

        with lease:
            pass

        mock_pool.release.assert_called_once_with(mock_entry, mock_conn)
        mock_pool.destroy_container.assert_called_once_with(
            mock_entry.db_type, mock_entry.version, mock_entry.compat_mode
        )

    def test_no_destroy_without_mark(self):
        mock_pool = MagicMock()
        mock_entry = MagicMock()
        mock_conn = MagicMock()

        lease = cp_mod.ContainerLease(mock_pool, mock_entry, mock_conn)

        with lease:
            pass

        mock_pool.release.assert_called_once()
        mock_pool.destroy_container.assert_not_called()


class TestContainerPoolLease:
    @patch.object(cp_mod.ContainerPool, '_start_entry')
    @patch.object(cp_mod.ContainerPool, '_wait_for_port', return_value=True)
    @patch.object(cp_mod.ContainerPool, '_is_port_open', return_value=True)
    @patch('src.container_pool.docker')
    def test_lease_acquire_and_release(self, mock_docker, mock_port, mock_wait, mock_start):
        mock_docker.from_env.return_value = MagicMock()

        # Create mock pool
        mock_conn_pool = MagicMock()
        mock_conn_pool.connection.return_value = MagicMock()

        pool = cp_mod.ContainerPool()

        # Pre-register a healthy entry with a mock pool
        key = "postgresql-14"
        entry = cp_mod.ContainerEntry(
            container_name=f"db-mcp-{key}",
            container_id="abc123",
            host_port=15432,
            db_type="postgresql",
            version="14",
            state=cp_mod.ContainerState.HEALTHY,
            connection_pool=mock_conn_pool,
            max_concurrency=3,
        )
        with pool._pool_lock:
            pool._entries[key] = entry

        lease = pool.lease("postgresql", "14",
                           {"port": 5432, "image": "postgres:14",
                            "username": "u", "password": "p", "database": "d"})
        assert lease.connection is mock_conn_pool.connection.return_value
        with pool._pool_lock:
            assert entry.active_leases == 1

        pool.release(entry, lease.connection)
        with pool._pool_lock:
            assert entry.active_leases == 0

    @patch('src.container_pool.docker')
    def test_lease_capacity_error(self, mock_docker):
        mock_docker.from_env.return_value = MagicMock()

        mock_conn_pool = MagicMock()
        pool = cp_mod.ContainerPool()

        key = "postgresql-14"
        entry = cp_mod.ContainerEntry(
            container_name=f"db-mcp-{key}",
            container_id="abc123",
            host_port=15432,
            db_type="postgresql",
            version="14",
            state=cp_mod.ContainerState.HEALTHY,
            connection_pool=mock_conn_pool,
            max_concurrency=1,
        )
        # Acquire the only slot
        entry.semaphore.acquire()
        with pool._pool_lock:
            pool._entries[key] = entry

        pool._lease_timeout = 0.1
        with pytest.raises(cp_mod.ContainerPoolCapacityError, match="Too many concurrent"):
            pool.lease("postgresql", "14",
                       {"port": 5432, "image": "postgres:14",
                        "username": "u", "password": "p", "database": "d"})

        # Clean up
        entry.semaphore.release()


class TestContainerPoolContainerCreation:
    @patch.object(cp_mod.ContainerPool, '_start_entry')
    def test_concurrent_lease_same_key(self, mock_start_entry):
        """Two threads leasing the same key should result in only one _start_entry call."""
        mock_conn_pool = MagicMock()
        mock_conn_pool.connection.return_value = MagicMock()

        def _setup_entry(entry, config, db_type):
            entry.state = cp_mod.ContainerState.HEALTHY
            entry.connection_pool = mock_conn_pool
            entry.container_id = "abc123"
            entry.host_port = 15432

        mock_start_entry.side_effect = _setup_entry

        pool = cp_mod.ContainerPool()

        errors = []
        results = []

        def lease_and_check():
            try:
                lease = pool.lease(
                    "postgresql", "14",
                    {"port": 5432, "image": "postgres:14",
                     "username": "u", "password": "p", "database": "d"}
                )
                results.append(lease)
                time.sleep(0.05)
                pool.release(
                    pool._entries["postgresql-14"], lease.connection
                )
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=lease_and_check)
        t2 = threading.Thread(target=lease_and_check)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 2
        # Only one container should have been created
        assert mock_start_entry.call_count == 1


class TestContainerPoolShutdown:
    @patch('src.container_pool.docker')
    def test_shutdown_rejects_new_leases(self, mock_docker):
        mock_docker.from_env.return_value = MagicMock()

        pool = cp_mod.ContainerPool()
        pool.shutdown()

        with pytest.raises(cp_mod.ContainerPoolCapacityError, match="shutting down"):
            pool.lease("postgresql", "14",
                       {"port": 5432, "image": "postgres:14",
                        "username": "u", "password": "p", "database": "d"})


class TestContainerEntry:
    def test_defaults(self):
        entry = cp_mod.ContainerEntry(container_name="test")
        assert entry.state == cp_mod.ContainerState.STOPPED
        assert entry.active_leases == 0
        assert entry.max_concurrency == 10
        assert entry.semaphore is not None
        assert isinstance(entry.condition, threading.Condition)

    def test_semaphore_initial_value(self):
        entry = cp_mod.ContainerEntry(container_name="test", max_concurrency=3)
        assert entry.semaphore.acquire()
        assert entry.semaphore.acquire()
        assert entry.semaphore.acquire()
        # 4th acquire should block, test with timeout
        assert not entry.semaphore.acquire(blocking=False)

        entry.semaphore.release()
        entry.semaphore.release()
        entry.semaphore.release()


class TestResolveResourceLimits:
    def test_default_postgresql(self):
        limits = cp_mod._resolve_resource_limits("postgresql", {})
        assert limits == {"cpu": 1, "memory": "512m"}

    def test_default_sqlserver(self):
        limits = cp_mod._resolve_resource_limits("sqlserver", {})
        assert limits == {"cpu": 2, "memory": "2g"}

    def test_mssql_alias(self):
        limits = cp_mod._resolve_resource_limits("mssql", {})
        assert limits == {"cpu": 2, "memory": "2g"}

    def test_default_kingbase(self):
        limits = cp_mod._resolve_resource_limits("kingbase", {})
        assert limits == {"cpu": 2, "memory": "2g"}

    def test_unknown_db_uses_fallback(self):
        limits = cp_mod._resolve_resource_limits("unknown_db", {})
        assert limits == {"cpu": 1, "memory": "512m"}

    def test_config_override(self):
        limits = cp_mod._resolve_resource_limits("postgresql", {
            "resources": {"cpu": 4, "memory": "2g"}
        })
        assert limits == {"cpu": 4, "memory": "2g"}

    def test_config_partial_override_cpu(self):
        limits = cp_mod._resolve_resource_limits("mysql", {
            "resources": {"cpu": 2}
        })
        assert limits == {"cpu": 2, "memory": "512m"}

    def test_config_partial_override_memory(self):
        limits = cp_mod._resolve_resource_limits("oracle", {
            "resources": {"memory": "3g"}
        })
        assert limits == {"cpu": 1, "memory": "3g"}

    @patch.dict(os.environ, {"MCP_RESOURCE_CPU_POSTGRESQL": "3"}, clear=True)
    def test_env_override_cpu(self):
        limits = cp_mod._resolve_resource_limits("postgresql", {})
        assert limits["cpu"] == 3

    @patch.dict(os.environ, {"MCP_RESOURCE_MEM_MYSQL": "4g"}, clear=True)
    def test_env_override_memory(self):
        limits = cp_mod._resolve_resource_limits("mysql", {})
        assert limits["memory"] == "4g"

    @patch.dict(os.environ, {"MCP_RESOURCE_CPU_POSTGRESQL": "8",
                               "MCP_RESOURCE_MEM_POSTGRESQL": "8g"}, clear=True)
    def test_env_overrides_config(self):
        limits = cp_mod._resolve_resource_limits("postgresql", {
            "resources": {"cpu": 2, "memory": "1g"}
        })
        assert limits == {"cpu": 8, "memory": "8g"}
