from typing import Dict, Any
from .base import DBAdapter, register_adapter
from ..exceptions import AdapterConnectionError, AdapterExecutionError

# Vastbase 官方 Python 驱动为 vastbase-psycopg2，安装后提供 psycopg2 模块。
# 该驱动是 psycopg2 的 fork，API 完全兼容。
try:
    import psycopg2
    from psycopg2 import OperationalError
except ImportError:
    psycopg2 = None
    OperationalError = Exception


@register_adapter('vastbase')
class VastbaseAdapter(DBAdapter):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._supports_ddl_transaction = True

    def connect(self, host: str = 'localhost', port: int = None) -> None:
        if psycopg2 is None:
            raise AdapterConnectionError(
                "psycopg2 is not installed. "
                "For Vastbase, install vendor/vastbase-psycopg2-1.1.2.tar.gz "
                "or fallback to psycopg2-binary."
            )

        db_port = port if port else self.config.get('port', 5432)
        try:
            self.connection = psycopg2.connect(
                host=host,
                port=db_port,
                user=self.config.get('username', 'dbadmin'),
                password=self.config.get('password', 'password'),
                database=self.config.get('database', 'postgres')
            )
            self.cursor = self.connection.cursor()
            self.connection.autocommit = True
            self._apply_statement_timeout()
        except OperationalError as e:
            raise AdapterConnectionError(f"Vastbase connection failed: {str(e)}")

    def _apply_statement_timeout(self) -> None:
        if self.cursor and self._statement_timeout > 0:
            try:
                self.cursor.execute(f"SET statement_timeout = '{int(self._statement_timeout)}s'")
            except Exception:
                pass

    def execute(self, query: str) -> Dict[str, Any]:
        if not self.connection or not self.cursor:
            raise AdapterExecutionError("Not connected to database")

        try:
            self.cursor.execute(query)
            columns = [desc[0] for desc in self.cursor.description] if self.cursor.description else []
            rows = self.cursor.fetchmany(self._max_rows + 1) if self.cursor.description else []
            truncated = len(rows) > self._max_rows
            if truncated:
                rows = rows[:self._max_rows]

            return {
                'columns': columns,
                'rows': self._format_result(columns, rows),
                'row_count': len(rows),
                'truncated': truncated,
            }
        except AdapterExecutionError:
            raise
        except Exception as e:
            raise AdapterExecutionError(f"Vastbase execute failed: {str(e)}")

    # TODO: 暂时禁用事务相关逻辑
    def begin_transaction(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def disconnect(self) -> None:
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
