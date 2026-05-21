import logging
from typing import Dict, Any
from .base import DBAdapter, register_adapter
from ..exceptions import AdapterConnectionError, AdapterExecutionError

try:
    import oracledb
except ImportError:
    oracledb = None

logger = logging.getLogger(__name__)


@register_adapter('oracle')
class OracleAdapter(DBAdapter):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._supports_ddl_transaction = False

    def connect(self, host: str = 'localhost', port: int = None) -> None:
        if oracledb is None:
            raise AdapterConnectionError("oracledb library is not installed")

        db_port = port if port else self.config.get('port', 1521)
        database = self.config.get('database', 'XE')
        logger.info(f"Oracle connecting to {host}:{db_port}/{database}")
        try:
            dsn = oracledb.makedsn(host, db_port, service_name=database)
            connect_kwargs = {
                'user': self.config.get('username', 'SYSTEM'),
                'password': self.config.get('password', 'oracle'),
                'dsn': dsn,
                'tcp_connect_timeout': 10,
            }
            self.connection = oracledb.connect(**connect_kwargs)
            self.connection.autocommit = True
            self.cursor = self.connection.cursor()
            self._apply_statement_timeout()
        except AdapterConnectionError:
            raise
        except Exception as e:
            raise AdapterConnectionError(f"Oracle connection failed: {str(e)}")

    def _apply_statement_timeout(self) -> None:
        """Oracle has no session-level statement_timeout equivalent like PG.

        The HTTP-level MCP_QUERY_TIMEOUT provides the hard cap for Oracle queries.
        This avoids the need for DBA-level Resource Manager / profile setup which
        is impractical for throwaway Docker containers.
        """

    def execute(self, query: str) -> Dict[str, Any]:
        if not self.connection or not self.cursor:
            raise AdapterExecutionError("Not connected to database")

        try:
            self.cursor.execute(query)
            columns = [desc[0] for desc in self.cursor.description] if self.cursor.description else []
            if self.cursor.description:
                rows = self.cursor.fetchmany(self._max_rows + 1)
                truncated = len(rows) > self._max_rows
                if truncated:
                    rows = rows[:self._max_rows]
            else:
                rows = []
                truncated = False

            return {
                'columns': columns,
                'rows': self._format_result(columns, rows),
                'row_count': len(rows),
                'truncated': truncated,
            }
        except AdapterExecutionError:
            raise
        except Exception as e:
            raise AdapterExecutionError(f"Oracle execute failed: {str(e)}")

    def begin_transaction(self) -> None:
        if self.connection:
            self.connection.autocommit = False

    def rollback(self) -> None:
        if self.connection is None:
            return
        if self.cursor:
            try:
                self.cursor.close()
            except Exception:
                pass
            self.cursor = None
        try:
            self.connection.rollback()
        except Exception:
            pass
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()

    def commit(self) -> None:
        if self.connection is None:
            return
        if self.cursor:
            try:
                self.cursor.close()
            except Exception:
                pass
            self.cursor = None
        try:
            self.connection.commit()
        except Exception:
            pass
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()

    def disconnect(self) -> None:
        logger.info("Oracle disconnecting")
        self._safe_disconnect()
