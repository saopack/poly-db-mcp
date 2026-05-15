import pytest
from unittest.mock import Mock, patch, MagicMock
from src.executor import MCPExecutor, _split_sql_statements
from src.config_manager import ConfigManager


class TestSplitSqlStatements:
    def test_single_statement(self):
        assert _split_sql_statements("SELECT 1") == ["SELECT 1"]

    def test_multiple_statements(self):
        result = _split_sql_statements("SELECT 1; SELECT 2; SELECT 3")
        assert result == ["SELECT 1", "SELECT 2", "SELECT 3"]

    def test_trailing_semicolon(self):
        assert _split_sql_statements("SELECT 1;") == ["SELECT 1"]

    def test_empty_statements_skipped(self):
        result = _split_sql_statements("SELECT 1; ; SELECT 2")
        assert result == ["SELECT 1", "SELECT 2"]

    def test_semicolon_in_single_quoted_string(self):
        result = _split_sql_statements("SELECT 'hello; world' AS greeting; SELECT 2")
        assert result == ["SELECT 'hello; world' AS greeting", "SELECT 2"]

    def test_semicolon_in_double_quoted_identifier(self):
        result = _split_sql_statements('SELECT "col;umn" FROM t; SELECT 2')
        assert result == ['SELECT "col;umn" FROM t', "SELECT 2"]

    def test_escaped_single_quote(self):
        result = _split_sql_statements("SELECT 'it''s fine' AS word; SELECT 2")
        assert result == ["SELECT 'it''s fine' AS word", "SELECT 2"]

    def test_single_line_comment_with_semicolon(self):
        result = _split_sql_statements("SELECT 1; -- comment with ; semicolon\nSELECT 2")
        assert result == ["SELECT 1", "-- comment with ; semicolon\nSELECT 2"]

    def test_block_comment_with_semicolon(self):
        result = _split_sql_statements("SELECT 1; /* inline; semicolon */ SELECT 2")
        assert result == ["SELECT 1", "/* inline; semicolon */ SELECT 2"]

    def test_multiline_sql(self):
        result = _split_sql_statements(
            "CREATE TABLE t (\n  id INT,\n  name VARCHAR(100)\n);\nINSERT INTO t VALUES (1, 'test');"
        )
        assert len(result) == 2
        assert result[0].startswith("CREATE TABLE t")
        assert result[1].startswith("INSERT INTO t")

    def test_ddl_and_dml_mixed(self):
        result = _split_sql_statements(
            "CREATE TABLE users (id INT, name VARCHAR(100));"
            "INSERT INTO users VALUES (1, 'Alice');"
            "SELECT * FROM users;"
        )
        assert len(result) == 3
        assert result[0] == "CREATE TABLE users (id INT, name VARCHAR(100))"
        assert result[1] == "INSERT INTO users VALUES (1, 'Alice')"
        assert result[2] == "SELECT * FROM users"

    def test_empty_input(self):
        assert _split_sql_statements("") == []
        assert _split_sql_statements("   ") == []
        assert _split_sql_statements(";") == []

    def test_dollar_quoted_string(self):
        result = _split_sql_statements(
            "SELECT $$hello; world$$ AS greeting; SELECT 2"
        )
        assert result == ["SELECT $$hello; world$$ AS greeting", "SELECT 2"]

    def test_named_dollar_quoted_string(self):
        result = _split_sql_statements(
            "SELECT $body$text; more$$ text$body$ AS body; SELECT 2"
        )
        assert len(result) == 2
        assert "$body$" in result[0]

    def test_backtick_identifier(self):
        result = _split_sql_statements(
            "SELECT `col;name` FROM t; SELECT 2"
        )
        assert result == ["SELECT `col;name` FROM t", "SELECT 2"]

    def test_escape_string(self):
        result = _split_sql_statements(
            "SELECT E'hello\\'world;' AS esc; SELECT 2"
        )
        assert result == ["SELECT E'hello\\'world;' AS esc", "SELECT 2"]


class TestMCPExecutor:
    def setup_method(self):
        ConfigManager.load_config()

    @patch('src.executor.DockerManager')
    def test_is_ddl_statement(self, MockDocker):
        executor = MCPExecutor()

        assert executor._is_ddl_statement('CREATE TABLE test (id INT)') is True
        assert executor._is_ddl_statement('ALTER TABLE test ADD COLUMN name VARCHAR(100)') is True
        assert executor._is_ddl_statement('DROP TABLE test') is True
        assert executor._is_ddl_statement('TRUNCATE TABLE test') is True
        assert executor._is_ddl_statement('RENAME TABLE test TO new_test') is True

        assert executor._is_ddl_statement('SELECT * FROM test') is False
        assert executor._is_ddl_statement('INSERT INTO test VALUES (1)') is False
        assert executor._is_ddl_statement('UPDATE test SET name = "test"') is False
        assert executor._is_ddl_statement('DELETE FROM test WHERE id = 1') is False

    @patch('src.executor.DockerManager')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_run_validation_success_dml(self, MockRegistry, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 5432)
        mock_docker.wait_for_port.return_value = True

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = True
        mock_adapter.execute_with_rollback.return_value = {
            'status': 'success',
            'data': {'columns': ['id'], 'rows': [{'id': 1}], 'row_count': 1}
        }

        executor = MCPExecutor()
        result = executor.run_validation('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'success'
        assert 'data' in result
        mock_adapter.execute_with_rollback.assert_called_once_with('SELECT 1')
        mock_docker.stop_container.assert_not_called()

    @patch('src.executor.DockerManager')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_run_validation_ddl_unsupported(self, MockRegistry, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 54321)
        mock_docker.wait_for_port.return_value = True

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = False
        mock_adapter.execute.return_value = {
            'columns': [], 'rows': [], 'row_count': 0
        }

        executor = MCPExecutor()
        result = executor.run_validation('kingbase', 'V8', 'CREATE TABLE test (id INT)')

        assert result['status'] == 'success'
        assert 'note' in result
        assert 'container will be destroyed' in result['note']
        mock_adapter.execute.assert_called_once()
        mock_docker.stop_container.assert_called_once_with('container_id')

    @patch('src.executor.DockerManager')
    def test_run_validation_invalid_db(self, MockDocker):
        executor = MCPExecutor()
        result = executor.run_validation('invalid_db', '1.0', 'SELECT 1')

        assert result['status'] == 'error'
        assert 'Unsupported database type' in result['message']

    @patch('src.executor.DockerManager')
    def test_run_validation_timeout(self, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 5432)
        mock_docker.wait_for_port.return_value = False

        executor = MCPExecutor()
        result = executor.run_validation('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'error'
        assert 'failed to start' in result['message'].lower()
        mock_docker.stop_container.assert_not_called()

    @patch('src.executor.DockerManager')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_run_validation_adapter_exception(self, MockRegistry, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 5432)
        mock_docker.wait_for_port.return_value = True

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.connect.side_effect = ConnectionError("Connection refused")

        executor = MCPExecutor()
        result = executor.run_validation('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'error'
        assert 'Connection refused' in result['message']
        mock_docker.stop_container.assert_not_called()

    @patch('src.executor.DockerManager')
    def test_run_validation_no_adapter(self, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 5432)
        mock_docker.wait_for_port.return_value = True

        with patch('src.executor.ADAPTER_REGISTRY', {}):
            executor = MCPExecutor()
            result = executor.run_validation('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'error'
        assert 'No adapter found' in result['message']

    @patch('src.executor.DockerManager')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_run_validation_multi_statement(self, MockRegistry, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 5432)
        mock_docker.wait_for_port.return_value = True

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = True
        mock_adapter.execute_with_rollback.return_value = {
            'status': 'success',
            'data': {'columns': ['id'], 'rows': [{'id': 1}], 'row_count': 1}
        }

        executor = MCPExecutor()
        result = executor.run_validation(
            'postgresql', '14',
            'SELECT 1; SELECT 2; SELECT 3'
        )

        assert result['status'] == 'success'
        assert isinstance(result['data'], list)
        assert len(result['data']) == 3
        for entry in result['data']:
            assert entry['status'] == 'success'
            assert 'statement' in entry
        assert result['data'][0]['statement'] == 'SELECT 1'
        assert result['data'][1]['statement'] == 'SELECT 2'
        assert result['data'][2]['statement'] == 'SELECT 3'
        assert mock_adapter.execute_with_rollback.call_count == 3

    @patch('src.executor.DockerManager')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_run_validation_multi_statement_error_stops(self, MockRegistry, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 5432)
        mock_docker.wait_for_port.return_value = True

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = True
        mock_adapter.execute_with_rollback.side_effect = [
            {'status': 'success', 'data': {'columns': ['id'], 'rows': [], 'row_count': 0}},
            {'status': 'error', 'message': 'syntax error'},
            {'status': 'success', 'data': {'columns': ['id'], 'rows': [{'id': 3}], 'row_count': 1}},
        ]

        executor = MCPExecutor()
        result = executor.run_validation(
            'postgresql', '14',
            'SELECT 1; BAD SQL; SELECT 3'
        )

        assert result['status'] == 'success'
        assert len(result['data']) == 2
        assert result['data'][0]['status'] == 'success'
        assert result['data'][1]['status'] == 'error'
        assert 'syntax error' in result['data'][1]['message']

    @patch('src.executor.DockerManager')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_run_validation_mixed_ddl_dml(self, MockRegistry, MockDocker):
        mock_docker = MockDocker.return_value
        mock_docker.start_container.return_value = ('container_id', 5432)
        mock_docker.wait_for_port.return_value = True

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = False
        mock_adapter.execute.return_value = {
            'columns': [], 'rows': [], 'row_count': 0
        }
        mock_adapter.execute_with_rollback.return_value = {
            'status': 'success',
            'data': {'columns': ['id'], 'rows': [{'id': 1}], 'row_count': 1}
        }

        executor = MCPExecutor()
        result = executor.run_validation(
            'mysql', '8.0',
            'CREATE TABLE t (id INT); INSERT INTO t VALUES (1); SELECT * FROM t'
        )

        assert result['status'] == 'success'
        assert len(result['data']) == 3
        assert 'DDL executed' in result['data'][0]['note']
        mock_adapter.execute.assert_called_once()
        assert mock_adapter.execute_with_rollback.call_count == 2
