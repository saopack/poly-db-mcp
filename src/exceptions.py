"""自定义异常体系，用于精确的错误处理和API响应。"""


class MCPError(Exception):
    """所有MCP服务异常的基类。"""
    http_status: int = 500


class ConfigError(MCPError):
    """配置相关错误。"""
    http_status = 500


class DatabaseNotFoundError(ConfigError):
    """请求的数据库类型或版本未在配置中找到。"""
    http_status = 404


class AdapterError(MCPError):
    """数据库适配器错误基类。"""
    http_status = 502


class AdapterConnectionError(AdapterError):
    """数据库连接失败。"""
    http_status = 502


class AdapterExecutionError(AdapterError):
    """SQL执行失败。"""
    http_status = 400


class AdapterTimeoutError(AdapterExecutionError):
    """SQL执行超时。"""
    http_status = 504


class DockerError(MCPError):
    """Docker容器操作错误。"""
    http_status = 500


class DockerContainerStartError(DockerError):
    """容器启动失败。"""
    http_status = 500


class DockerContainerPortError(DockerError):
    """容器端口映射失败。"""
    http_status = 500


class ValidationError(MCPError):
    """输入验证错误。"""
    http_status = 422
