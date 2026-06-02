import pytest
from src.config_manager import ConfigManager

class TestConfigManager:
    def setup_method(self):
        ConfigManager._config = {}

    def test_load_config(self):
        ConfigManager.load_config()
        assert 'databases' in ConfigManager._config
        assert 'vastbase' in ConfigManager._config['databases']

    def test_get_db_config(self):
        ConfigManager.load_config()
        config = ConfigManager.get_db_config('vastbase', '3.0.8.29407')
        assert config is not None
        assert 'vastbase' in config['image']
        assert config['port'] == 5432

    def test_get_db_config_invalid(self):
        ConfigManager.load_config()
        config = ConfigManager.get_db_config('invalid', '1.0')
        assert config is None

    def test_get_supported_databases(self):
        ConfigManager.load_config()
        dbs = ConfigManager.get_supported_databases()
        assert 'vastbase' in dbs
        assert 'kingbase' in dbs

    def test_get_db_versions(self):
        ConfigManager.load_config()
        versions = ConfigManager.get_db_versions('vastbase')
        assert '3.0.9' in versions
        assert '3.0.8' in versions
        assert '2.2.15' in versions