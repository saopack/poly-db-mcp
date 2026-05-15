from abc import ABC, abstractmethod
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

    @property
    def supports_ddl_transaction(self) -> bool:
        return self._supports_ddl_transaction

    def _apply_statement_timeout(self) -> None:
        """Apply statement timeout for the current session. Override per driver."""
        pass

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
                row_dict[col] = row[i]
            result.append(row_dict)
        return result

    def execute_with_rollback(self, query: str) -> Dict[str, Any]:
        try:
            self.begin_transaction()
            result = self.execute(query)
            self.rollback()
            return {"status": "success", "data": result}
        except AdapterExecutionError as e:
            try:
                self.rollback()
            except Exception:
                pass
            return {"status": "error", "message": str(e)}
        except Exception as e:
            try:
                self.rollback()
            except Exception:
                pass
            return {"status": "error", "message": str(e)}
