import psycopg2
from psycopg2 import OperationalError
from typing import Dict, Any
from .base import DBAdapter, register_adapter
from ..exceptions import AdapterConnectionError, AdapterExecutionError


@register_adapter('kingbase')
class KingbaseAdapter(DBAdapter):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        db_mode = (config.get('env') or {}).get('DB_MODE', '').lower()
        self._supports_ddl_transaction = (db_mode == 'pg')

    def connect(self, host: str = 'localhost', port: int = None) -> None:
        db_port = port if port else self.config.get('port', 54321)
        try:
            self.connection = psycopg2.connect(
                host=host,
                port=db_port,
                user=self.config.get('username', 'SYSTEM'),
                password=self.config.get('password', 'password'),
                database=self.config.get('database', 'TEST')
            )
            self.cursor = self.connection.cursor()
            self.connection.autocommit = True
            self._apply_statement_timeout()
        except OperationalError as e:
            raise AdapterConnectionError(f"Kingbase connection failed: {str(e)}")

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
            raise AdapterExecutionError(f"Kingbase execute failed: {str(e)}")

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
        self._apply_statement_timeout()

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
        self._apply_statement_timeout()

    def disconnect(self) -> None:
        if self.cursor:
            self.cursor.close()
        if self.connection:
            self.connection.close()
