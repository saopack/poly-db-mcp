import json
from abc import ABC, abstractmethod
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional, Type
from ..exceptions import AdapterExecutionError

ADAPTER_REGISTRY: Dict[str, Type['DBAdapter']] = {}


def register_adapter(name: str):
    """适配器注册装饰器，将适配器类注册到全局注册表中。"""
    def decorator(cls: Type['DBAdapter']) -> Type['DBAdapter']:
        ADAPTER_REGISTRY[name] = cls
        return cls
    return decorator


class DBAdapter(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.connection = None
        self.cursor = None
        self._supports_ddl_transaction = False
        self._statement_timeout = config.get('statement_timeout', 30)
        self._max_rows = config.get('max_rows', 1000)
        self._is_pooled = False

    @property
    def supports_ddl_transaction(self) -> bool:
        return self._supports_ddl_transaction

    def _apply_statement_timeout(self) -> None:
        """Apply statement timeout for the current session. Override per driver."""
        pass

    def use_connection(self, conn) -> None:
        """Use an existing DB-API connection from the connection pool.

        Sets up the cursor and applies statement timeout, just like connect()
        would, but without creating a new connection.
        """
        self._is_pooled = True
        self.connection = conn
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()
        self._apply_statement_timeout()

    def _safe_disconnect(self) -> None:
        """Close cursor and optionally close connection.

        When using a pooled connection, only closes the cursor and detaches
        the connection (the pool manages its lifecycle).
        When using a direct connection, closes both.
        """
        if self.cursor:
            try:
                self.cursor.close()
            except Exception:
                pass
            self.cursor = None
        if not self._is_pooled and self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None

    @abstractmethod
    def connect(self, host: str = 'localhost', port: int = None) -> None:
        pass

    @abstractmethod
    def execute(self, query: str) -> Dict[str, Any]:
        pass

    @abstractmethod
    def begin_transaction(self) -> None:
        pass

    @abstractmethod
    def rollback(self) -> None:
        pass

    @abstractmethod
    def commit(self) -> None:
        pass

    @abstractmethod
    def disconnect(self) -> None:
        pass

    def _format_result(self, columns: list, rows: list) -> Dict[str, Any]:
        result = []
        for row in rows:
            row_dict = {}
            for i, col in enumerate(columns):
                val = row[i]
                if isinstance(val, datetime):
                    val = val.isoformat()
                elif isinstance(val, (date, time, timedelta)):
                    val = str(val)
                elif isinstance(val, Decimal):
                    val = float(val)
                elif isinstance(val, bytes):
                    val = val.decode('utf-8', errors='replace')
                row_dict[col] = val
            result.append(row_dict)
        return result

    def execute_with_rollback(self, query: str) -> Dict[str, Any]:
        """Execute query within a transaction and rollback.

        Returns the raw result dict from execute() on success.
        Exceptions propagate to the caller — rollback is guaranteed via finally.
        """
        self.begin_transaction()
        try:
            return self.execute(query)
        finally:
            self.rollback()
