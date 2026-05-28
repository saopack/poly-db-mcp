import yaml
import os
import threading
import logging
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class VersionConfig(BaseModel):
    """单个数据库版本的配置校验模型"""
    image: str = Field(..., min_length=1, description="Docker镜像名")
    port: int = Field(..., gt=0, le=65535, description="数据库端口")
    adapter: str = Field(..., min_length=1, description="适配器类名")
    username: str = Field(default="", description="数据库用户名")
    password: str = Field(default="", description="数据库密码")
    database: str = Field(default="", description="数据库名")
    privileged: bool = Field(default=False, description="是否特权容器")
    prewarm: bool = Field(default=False, description="是否在启动时预热容器")
    env: Optional[Dict[str, str]] = Field(default=None, description="环境变量")
    command: Optional[str] = Field(default=None, description="容器启动命令参数")


class DBTypeConfig(BaseModel):
    """数据库类型配置"""
    versions: Dict[str, VersionConfig]


class DatabaseConfig(BaseModel):
    """顶层配置模型"""
    databases: Dict[str, DBTypeConfig]


class ConfigManager:
    _config: Dict[str, Any] = {}
    _validated: bool = False
    _lock = threading.Lock()

    @classmethod
    def load_config(cls, config_path: str = "config/databases.yaml") -> None:
        abs_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), config_path)
        with cls._lock:
            with open(abs_path, 'r', encoding='utf-8') as f:
                raw = yaml.safe_load(f)
            try:
                DatabaseConfig.model_validate(raw)
                cls._validated = True
                logger.info("Configuration validated successfully")
            except ValidationError as e:
                logger.error(f"Configuration validation failed: {e}")
                cls._validated = False
            cls._config = raw

    @classmethod
    def is_config_valid(cls) -> bool:
        """返回配置是否通过校验"""
        return cls._validated

    @classmethod
    def _find_db_type(cls, db_type: str) -> Optional[str]:
        """Case-insensitive db_type lookup, returns the canonical key or None."""
        if 'databases' not in cls._config:
            return None
        lower = db_type.lower()
        for key in cls._config['databases']:
            if key.lower() == lower:
                return key
        return None

    @classmethod
    def _find_version(cls, versions: dict, version: str) -> Optional[str]:
        """Case-insensitive version lookup, returns the canonical key or None."""
        lower = version.lower()
        for key in versions:
            if key.lower() == lower:
                return key
        return None

    @classmethod
    def get_db_config(cls, db_type: str, version: str) -> Optional[Dict[str, Any]]:
        if 'databases' not in cls._config:
            return None

        canonical_type = cls._find_db_type(db_type)
        if not canonical_type:
            return None
        db_config = cls._config['databases'][canonical_type]

        canonical_version = cls._find_version(db_config['versions'], version)
        if not canonical_version:
            return cls._get_ephemeral_config(canonical_type, db_config, version)

        version_config = db_config['versions'][canonical_version]

        return {
            'image': version_config['image'],
            'port': version_config['port'],
            'adapter': version_config['adapter'],
            'username': version_config.get('username', ''),
            'password': version_config.get('password', ''),
            'database': version_config.get('database', ''),
            'env': version_config.get('env'),
            'privileged': version_config.get('privileged', False),
            'prewarm': version_config.get('prewarm', False),
            'command': version_config.get('command'),
            'ephemeral': False,
        }

    @classmethod
    def _get_ephemeral_config(cls, canonical_type: str, db_config: dict,
                               version: str) -> Optional[Dict[str, Any]]:
        """Return a synthetic config for versions not in databases.yaml.

        Only works for db_types that have nexus credentials configured.
        Falls back to the ``defaults`` section of the db_type config.
        """
        nexus = db_config.get('nexus', {})
        if not nexus:
            return None

        defaults = db_config.get('defaults', {})
        if not defaults:
            return None

        logger.info(f"Version {version} not in config for {canonical_type}, "
                     "using ephemeral (auto-build via Nexus)")

        return {
            'image': defaults.get('base_image'),
            'port': defaults.get('port', 5432),
            'adapter': defaults.get('adapter', ''),
            'username': defaults.get('username', ''),
            'password': defaults.get('password', ''),
            'database': defaults.get('database', 'postgres'),
            'env': defaults.get('env'),
            'privileged': defaults.get('privileged', True),
            'prewarm': False,
            'command': defaults.get('command'),
            'ephemeral': True,
            'needs_binary_prep': True,
        }

    @classmethod
    def get_supported_databases(cls) -> list:
        if 'databases' not in cls._config:
            return []
        return list(cls._config['databases'].keys())

    @classmethod
    def get_db_versions(cls, db_type: str) -> list:
        if 'databases' not in cls._config:
            return []

        canonical_type = cls._find_db_type(db_type)
        if not canonical_type:
            return []
        db_config = cls._config['databases'][canonical_type]
        if 'versions' not in db_config:
            return []

        return list(db_config['versions'].keys())

    @classmethod
    def get_nexus_config(cls, db_type: str) -> Optional[Dict[str, str]]:
        """Return nexus credentials for a db_type, or None."""
        if 'databases' not in cls._config:
            return None
        canonical_type = cls._find_db_type(db_type)
        if not canonical_type:
            return None
        db_cfg = cls._config['databases'][canonical_type]
        nexus = db_cfg.get('nexus', {})
        if not nexus:
            return None
        return {
            'domain': nexus.get('domain', ''),
            'username': nexus.get('username', ''),
            'password': nexus.get('password', ''),
            'repository': nexus.get('repository', ''),
            'package': nexus.get('package', {}),
        }
