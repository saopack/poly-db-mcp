from typing import Dict, Any
from .base import DBAdapter, register_adapter
from ..exceptions import AdapterConnectionError, AdapterExecutionError

try:
    import pymssql
except ImportError:
    pymssql = None


@register_adapter('sqlserver')
class SqlServerAdapter(DBAdapter):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._supports_ddl_transaction = True

    def connect(self, host: str = 'localhost', port: int = None) -> None:
        if pymssql is None:
            raise AdapterConnectionError("pymssql library is not installed")

        db_port = port if port else self.config.get('port', 1433)
        try:
            self.connection = pymssql.connect(
                server=host,
                port=db_port,
                user=self.config.get('username', 'sa'),
                password=self.config.get('password', ''),
                database=self.config.get('database', 'master'),
                autocommit=True
            )
            self.cursor = self.connection.cursor()
        except AdapterConnectionError:
            raise
        except Exception as e:
            raise AdapterConnectionError(f"SQL Server connection failed: {str(e)}")

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
            raise AdapterExecutionError(f"SQL Server execute failed: {str(e)}")

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
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
