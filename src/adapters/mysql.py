import logging
from typing import Dict, Any
from .base import DBAdapter, register_adapter
from ..exceptions import AdapterConnectionError, AdapterExecutionError

try:
    import pymysql
except ImportError:
    pymysql = None

logger = logging.getLogger(__name__)


@register_adapter('mysql')
class MySQLAdapter(DBAdapter):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._supports_ddl_transaction = False

    def connect(self, host: str = 'localhost', port: int = None) -> None:
        if pymysql is None:
            raise AdapterConnectionError("pymysql library is not installed")

        db_port = port if port else self.config.get('port', 3306)
        timeout = self._statement_timeout if self._statement_timeout > 0 else 30
        logger.info(f"MySQL connecting to {host}:{db_port}, db={self.config.get('database', 'test')}, timeout={timeout}s")
        try:
            self.connection = pymysql.connect(
                host=host,
                port=db_port,
                user=self.config.get('username', 'root'),
                password=self.config.get('password', ''),
                database=self.config.get('database', 'test'),
                autocommit=True,
                charset='utf8mb4',
                read_timeout=timeout,
                write_timeout=timeout,
                connect_timeout=10,
            )
            self.cursor = self.connection.cursor()
            self._apply_statement_timeout()
        except AdapterConnectionError:
            raise
        except Exception as e:
            raise AdapterConnectionError(f"MySQL connection failed: {str(e)}")

    def _apply_statement_timeout(self) -> None:
        """Set server-side max_execution_time for MySQL 5.7.8+."""
        if self._statement_timeout > 0 and self.connection:
            try:
                ms = int(self._statement_timeout * 1000)
                with self.connection.cursor() as cur:
                    cur.execute(f"SET SESSION max_execution_time = {ms}")
            except Exception:
                pass

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
            raise AdapterExecutionError(f"MySQL execute failed: {str(e)}")

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
        logger.info("MySQL disconnecting")
        self._safe_disconnect()
