import pytest
from unittest.mock import Mock, patch, MagicMock
from src.executor import MCPExecutor, _split_sql_statements, _is_plsql_block
from src.config_manager import ConfigManager
from src.exceptions import AdapterExecutionError
from src.container_pool import ContainerPoolCapacityError


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


class TestPlsqlBlock:
    def test_declare_block_single_statement(self):
        result = _split_sql_statements(
            "DECLARE v INT; BEGIN SELECT 1 INTO v FROM dual; END;"
        )
        assert len(result) == 1
        assert result[0].startswith("DECLARE")

    def test_anonymous_begin_end_block(self):
        result = _split_sql_statements(
            "BEGIN\n  INSERT INTO t VALUES (1);\n  INSERT INTO t VALUES (2);\nEND;"
        )
        assert len(result) == 1
        assert result[0].startswith("BEGIN")

    def test_create_function(self):
        result = _split_sql_statements(
            "CREATE OR REPLACE FUNCTION foo RETURN INT AS BEGIN RETURN 1; END;"
        )
        assert len(result) == 1
        assert result[0].startswith("CREATE")

    def test_create_procedure(self):
        result = _split_sql_statements(
            "CREATE PROCEDURE bar AS BEGIN NULL; END;"
        )
        assert len(result) == 1

    def test_bare_begin_is_not_plsql(self):
        result = _split_sql_statements("BEGIN;")
        assert result == ["BEGIN"]

    def test_begin_transaction_is_not_plsql(self):
        result = _split_sql_statements("BEGIN TRANSACTION;")
        assert result == ["BEGIN TRANSACTION"]

    def test_is_plsql_block_declare(self):
        assert _is_plsql_block("DECLARE v INT; BEGIN NULL; END;") is True

    def test_is_plsql_block_begin_end(self):
        assert _is_plsql_block("BEGIN NULL; END;") is True

    def test_is_plsql_block_bare_begin(self):
        assert _is_plsql_block("BEGIN") is False

    def test_is_plsql_block_begin_transaction(self):
        assert _is_plsql_block("BEGIN TRANSACTION") is False

    def test_is_plsql_block_select(self):
        assert _is_plsql_block("SELECT 1") is False

    def test_plsql_with_trailing_slash(self):
        result = _split_sql_statements(
            "DECLARE v INT; BEGIN SELECT 1 INTO v FROM dual; END;\n/\nSELECT 1;"
        )
        assert len(result) == 2
        assert result[0].startswith("DECLARE")
        assert "END;" in result[0]
        assert result[1] == "SELECT 1"

    def test_create_procedure_followed_by_call(self):
        """CREATE PROCEDURE ... END; followed by CALL should be two statements."""
        result = _split_sql_statements(
            "CREATE OR REPLACE PROCEDURE demo_sys_context_3\n"
            "IS\n"
            "    v_user VARCHAR2(100);\n"
            "BEGIN\n"
            "    v_user := SESSION_USER;\n"
            "    INSERT INTO log_table_3(msg) VALUES ('current user: ' || v_user);\n"
            "END;\n"
            "CALL demo_sys_context_3();"
        )
        assert len(result) == 2, f"Expected 2 statements, got {len(result)}: {result}"
        assert result[0].startswith("CREATE OR REPLACE PROCEDURE")
        assert "END;" in result[0]
        assert result[1] == "CALL demo_sys_context_3()"

    def test_create_procedure_followed_by_select(self):
        """CREATE PROCEDURE ... END; followed by SELECT should be two statements."""
        result = _split_sql_statements(
            "CREATE PROCEDURE bar AS BEGIN NULL; END;\nSELECT 1;"
        )
        assert len(result) == 2
        assert result[0].startswith("CREATE PROCEDURE")
        assert result[1] == "SELECT 1"

    def test_nested_begin_end_in_procedure(self):
        """Nested BEGIN...END inside a procedure should be handled correctly."""
        result = _split_sql_statements(
            "CREATE OR REPLACE PROCEDURE nested_proc\n"
            "IS\n"
            "BEGIN\n"
            "  BEGIN\n"
            "    NULL;\n"
            "  END;\n"
            "  NULL;\n"
            "END;\n"
            "SELECT 1;"
        )
        assert len(result) == 2, f"Expected 2 statements, got {len(result)}: {result}"
        assert result[0].startswith("CREATE OR REPLACE PROCEDURE")
        assert result[1] == "SELECT 1"

    def test_anonymous_block_followed_by_statement(self):
        """Anonymous BEGIN...END followed by another statement."""
        result = _split_sql_statements(
            "BEGIN NULL; END;\nSELECT 1;"
        )
        assert len(result) == 2
        assert result[0] == "BEGIN NULL; END;"
        assert result[1] == "SELECT 1"

    def test_declare_block_followed_by_statement(self):
        """DECLARE...BEGIN...END followed by another statement."""
        result = _split_sql_statements(
            "DECLARE v INT; BEGIN v := 1; END;\nSELECT 1;"
        )
        assert len(result) == 2
        assert result[0].startswith("DECLARE")
        assert result[1] == "SELECT 1"


def _make_mock_pool():
    """Create a mock ContainerPool that returns a working lease with
    a mock DB-API connection usable by any adapter's use_connection()."""
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.description = [['id']]
    mock_cursor.fetchmany.return_value = [(1,)]
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.autocommit = True

    mock_lease = MagicMock()
    mock_lease.connection = mock_conn
    mock_lease.container_id = 'container_id'
    mock_lease.__enter__ = MagicMock(return_value=mock_lease)
    mock_lease.__exit__ = MagicMock(return_value=False)

    mock_pool.lease.return_value = mock_lease
    return mock_pool, mock_lease, mock_conn, mock_cursor


class TestMCPExecutor:
    def setup_method(self):
        ConfigManager.load_config()

    @patch('src.executor.ContainerPool')
    def test_is_ddl_statement(self, MockPool):
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

    @patch('src.executor.ContainerPool')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_execute_success_dml(self, MockRegistry, MockPool):
        mock_pool, mock_lease, mock_conn, _ = _make_mock_pool()
        MockPool.return_value = mock_pool

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = True
        mock_adapter.execute_with_rollback.return_value = {
            'columns': ['id'], 'rows': [{'id': 1}], 'row_count': 1
        }

        executor = MCPExecutor()
        result = executor.execute('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'success'
        assert 'data' in result
        mock_adapter.use_connection.assert_called_once_with(mock_conn)
        mock_adapter.execute_with_rollback.assert_called_once_with('SELECT 1')
        mock_lease.mark_for_destroy.assert_not_called()

    @patch('src.executor.ContainerPool')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_execute_ddl_unsupported(self, MockRegistry, MockPool):
        mock_pool, mock_lease, mock_conn, _ = _make_mock_pool()
        MockPool.return_value = mock_pool

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = False
        mock_adapter.execute.return_value = {
            'columns': [], 'rows': [], 'row_count': 0
        }

        executor = MCPExecutor()
        result = executor.execute('kingbase', 'V8', 'CREATE TABLE test (id INT)')

        assert result['status'] == 'success'
        assert 'note' not in result
        mock_adapter.use_connection.assert_called_once_with(mock_conn)
        assert mock_adapter.execute.call_count == 2  # CREATE TABLE + DROP TABLE (reverse)
        mock_lease.mark_for_destroy.assert_not_called()

    @patch('src.executor.ContainerPool')
    def test_execute_invalid_db(self, MockPool):
        executor = MCPExecutor()
        result = executor.execute('invalid_db', '1.0', 'SELECT 1')

        assert result['status'] == 'error'
        assert 'Unsupported database type' in result['message']

    @patch('src.executor.ContainerPool')
    def test_execute_timeout(self, MockPool):
        mock_pool = MockPool.return_value
        mock_pool.lease.side_effect = ContainerPoolCapacityError("Lease timeout")

        executor = MCPExecutor()
        result = executor.execute('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'error'

    @patch('src.executor.ContainerPool')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_execute_adapter_exception(self, MockRegistry, MockPool):
        mock_pool, mock_lease, mock_conn, _ = _make_mock_pool()
        MockPool.return_value = mock_pool

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = True
        mock_adapter.execute_with_rollback.side_effect = AdapterExecutionError("SQL error")

        executor = MCPExecutor()
        result = executor.execute('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'error'
        assert 'SQL error' in result['message']
        mock_adapter.use_connection.assert_called_once_with(mock_conn)

    @patch('src.executor.ContainerPool')
    def test_execute_no_adapter(self, MockPool):
        with patch('src.executor.ADAPTER_REGISTRY', {}):
            executor = MCPExecutor()
            result = executor.execute('postgresql', '14', 'SELECT 1')

        assert result['status'] == 'error'
        assert 'No adapter found' in result['message']

    @patch('src.executor.ContainerPool')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_execute_multi_statement(self, MockRegistry, MockPool):
        mock_pool, mock_lease, mock_conn, _ = _make_mock_pool()
        MockPool.return_value = mock_pool

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = True
        mock_adapter.execute.return_value = {
            'columns': ['id'], 'rows': [{'id': 1}], 'row_count': 1
        }

        executor = MCPExecutor()
        result = executor.execute(
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
        assert mock_adapter.execute.call_count == 3
        mock_adapter.begin_transaction.assert_called_once()
        mock_adapter.rollback.assert_called_once()

    @patch('src.executor.ContainerPool')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_execute_multi_statement_error_stops(self, MockRegistry, MockPool):
        mock_pool, mock_lease, mock_conn, _ = _make_mock_pool()
        MockPool.return_value = mock_pool

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = True
        mock_adapter.execute.side_effect = [
            {'columns': ['id'], 'rows': [], 'row_count': 0},
            AdapterExecutionError('syntax error'),
            {'columns': ['id'], 'rows': [{'id': 3}], 'row_count': 1},
        ]

        executor = MCPExecutor()
        result = executor.execute(
            'postgresql', '14',
            'SELECT 1; BAD SQL; SELECT 3'
        )

        assert result['status'] == 'success'
        assert len(result['data']) == 2
        assert result['data'][0]['status'] == 'success'
        assert result['data'][1]['status'] == 'error'
        assert 'syntax error' in result['data'][1]['message']
        mock_adapter.rollback.assert_called_once()

    @patch('src.executor.ContainerPool')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_execute_mixed_ddl_dml(self, MockRegistry, MockPool):
        mock_pool, mock_lease, mock_conn, _ = _make_mock_pool()
        MockPool.return_value = mock_pool

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = False
        mock_adapter.execute.return_value = {
            'columns': [], 'rows': [], 'row_count': 0
        }

        executor = MCPExecutor()
        result = executor.execute(
            'mysql', '8.0',
            'CREATE TABLE t (id INT); INSERT INTO t VALUES (1); SELECT * FROM t'
        )

        assert result['status'] == 'success'
        assert len(result['data']) == 3
        assert 'note' not in result['data'][0]
        assert result['data'][1]['status'] == 'success'
        assert result['data'][2]['status'] == 'success'
        assert mock_adapter.execute.call_count == 4  # DDL + 2*DML + reverse DDL
        mock_adapter.begin_transaction.assert_called_once()
        mock_adapter.rollback.assert_called_once()

    @patch('src.executor.ContainerPool')
    @patch('src.executor.ADAPTER_REGISTRY')
    def test_execute_ddl_reverse_fails_fallback(self, MockRegistry, MockPool):
        mock_pool, mock_lease, mock_conn, _ = _make_mock_pool()
        MockPool.return_value = mock_pool

        mock_adapter_cls = MockRegistry.get.return_value
        mock_adapter = mock_adapter_cls.return_value
        mock_adapter.supports_ddl_transaction = False
        # First call (CREATE TABLE) succeeds, second call (DROP TABLE) fails
        mock_adapter.execute.side_effect = [
            {'columns': [], 'rows': [], 'row_count': 0},
            AdapterExecutionError('reverse DDL failed'),
        ]

        executor = MCPExecutor()
        result = executor.execute('kingbase', 'V8', 'CREATE TABLE test (id INT)')

        assert result['status'] == 'success'
        assert 'note' in result
        assert 'container will be destroyed' in result['note']
        mock_lease.mark_for_destroy.assert_called_once()

    def test_generate_reverse_ddl_create_table(self):
        assert MCPExecutor._generate_reverse_ddl('CREATE TABLE foo (id INT)') == 'DROP TABLE foo'
        assert MCPExecutor._generate_reverse_ddl('CREATE TABLE IF NOT EXISTS foo (id INT)') == 'DROP TABLE foo'
        assert MCPExecutor._generate_reverse_ddl('create table foo (id INT)') == 'DROP TABLE foo'

    def test_generate_reverse_ddl_create_index(self):
        assert MCPExecutor._generate_reverse_ddl('CREATE INDEX idx_foo ON foo (id)') == 'DROP INDEX idx_foo'
        assert MCPExecutor._generate_reverse_ddl('CREATE UNIQUE INDEX idx_foo ON foo (id)') == 'DROP INDEX idx_foo'

    def test_generate_reverse_ddl_create_view(self):
        assert MCPExecutor._generate_reverse_ddl('CREATE VIEW v AS SELECT 1') == 'DROP VIEW v'
        assert MCPExecutor._generate_reverse_ddl('CREATE OR REPLACE VIEW v AS SELECT 1') == 'DROP VIEW v'

    def test_generate_reverse_ddl_alter_add_column(self):
        assert MCPExecutor._generate_reverse_ddl(
            'ALTER TABLE t ADD COLUMN name VARCHAR(100)'
        ) == 'ALTER TABLE t DROP COLUMN name'

    def test_generate_reverse_ddl_rename_table(self):
        assert MCPExecutor._generate_reverse_ddl('RENAME TABLE t1 TO t2') == 'RENAME TABLE t2 TO t1'

    def test_generate_reverse_ddl_irreversible(self):
        assert MCPExecutor._generate_reverse_ddl('DROP TABLE foo') is None
        assert MCPExecutor._generate_reverse_ddl('TRUNCATE TABLE foo') is None
