import pytest
from src.adapters import VastbaseAdapter, PostgreSQLAdapter, KingbaseAdapter, OracleAdapter
from src.adapters.base import ADAPTER_REGISTRY


class TestAdapters:
    def test_vastbase_adapter_initialization(self):
        config = {
            'username': 'dbadmin',
            'password': 'password',
            'database': 'postgres',
            'port': 5432
        }
        adapter = VastbaseAdapter(config)
        assert adapter.config == config
        assert adapter.connection is None
        assert adapter.cursor is None
        assert adapter.supports_ddl_transaction is True

    def test_postgresql_adapter_initialization(self):
        config = {
            'username': 'postgres',
            'password': 'postgres',
            'database': 'postgres',
            'port': 5432
        }
        adapter = PostgreSQLAdapter(config)
        assert adapter.config == config
        assert adapter.connection is None
        assert adapter.cursor is None
        assert adapter.supports_ddl_transaction is True

    def test_kingbase_adapter_initialization(self):
        config = {
            'username': 'SYSTEM',
            'password': 'password',
            'database': 'TEST',
            'port': 54321
        }
        adapter = KingbaseAdapter(config)
        assert adapter.config == config
        assert adapter.connection is None
        assert adapter.cursor is None
        assert adapter.supports_ddl_transaction is False

    def test_format_result(self):
        config = {'username': 'test', 'password': 'test'}
        adapter = PostgreSQLAdapter(config)
        columns = ['id', 'name']
        rows = [(1, 'test1'), (2, 'test2')]
        result = adapter._format_result(columns, rows)
        
        assert len(result) == 2
        assert result[0]['id'] == 1
        assert result[0]['name'] == 'test1'
        assert result[1]['id'] == 2
        assert result[1]['name'] == 'test2'

    def test_format_result_empty(self):
        config = {'username': 'test', 'password': 'test'}
        adapter = PostgreSQLAdapter(config)
        result = adapter._format_result([], [])
        assert result == []

    def test_oracle_adapter_initialization(self):
        config = {
            'username': 'SYSTEM',
            'password': 'oracle',
            'database': 'XE',
            'port': 1521
        }
        adapter = OracleAdapter(config)
        assert adapter.config == config
        assert adapter.connection is None
        assert adapter.cursor is None
        assert adapter.supports_ddl_transaction is False

    def test_adapter_registry(self):
        assert 'vastbase' in ADAPTER_REGISTRY
        assert 'postgresql' in ADAPTER_REGISTRY
        assert 'kingbase' in ADAPTER_REGISTRY
        assert 'oracle' in ADAPTER_REGISTRY
        assert ADAPTER_REGISTRY['vastbase'] is VastbaseAdapter
        assert ADAPTER_REGISTRY['postgresql'] is PostgreSQLAdapter
        assert ADAPTER_REGISTRY['oracle'] is OracleAdapter
