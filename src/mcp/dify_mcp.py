"""
Dify MCP (Model Context Protocol) 接口实现

Dify MCP允许外部工具与Dify平台集成，提供工具列表查询和工具调用功能。
"""

import json
from typing import Dict, Any, List, Optional, Union
from pydantic import BaseModel, Field
from ..config_manager import ConfigManager
from ..executor import MCPExecutor


class MCPToolParameter(BaseModel):
    """工具参数定义"""
    name: str = Field(..., description="参数名称")
    type: str = Field(..., description="参数类型")
    required: bool = Field(True, description="是否必填")
    description: str = Field("", description="参数描述")
    default: Optional[Any] = None


class MCPTool(BaseModel):
    """MCP工具定义"""
    name: str = Field(..., description="工具名称")
    description: str = Field(..., description="工具描述")
    parameters: List[MCPToolParameter] = Field([], description="参数列表")


class MCPError(BaseModel):
    """MCP错误响应"""
    type: str = Field(..., description="错误类型")
    message: str = Field(..., description="错误消息")


class MCPResult(BaseModel):
    """MCP执行结果"""
    status: str = Field(..., description="执行状态")
    content: Optional[Union[str, Dict[str, Any]]] = None
    error: Optional[MCPError] = None


class DifyMCPHandler:
    """Dify MCP处理器"""

    def __init__(self):
        from ..container_pool import ContainerPool
        self.executor = MCPExecutor(container_pool=ContainerPool())
    
    def get_tools(self) -> List[MCPTool]:
        """获取可用的MCP工具列表"""
        databases = ConfigManager.get_supported_databases()
        
        tools = []
        
        # SQL执行工具
        tools.append(MCPTool(
            name="execute_sql",
            description="执行SQL语句并返回结果",
            parameters=[
                MCPToolParameter(
                    name="db_type",
                    type="string",
                    required=True,
                    description=f"数据库类型，支持: {', '.join(databases)}"
                ),
                MCPToolParameter(
                    name="version",
                    type="string",
                    required=True,
                    description="数据库版本。Vastbase支持三种格式: 基础版本(3.0.8)、PSU补丁版本(3.0.8.psu0)、指定Build号(3.0.8.24875)；Kingbase: v8/v9；PostgreSQL: 12/13/14；Oracle: 11c/12c/18c/19c/21c；MySQL: 5.6/5.7/8.0；SQL Server: 2017/2019"
                ),
                MCPToolParameter(
                    name="query",
                    type="string",
                    required=True,
                    description="要执行的SQL语句"
                ),
                MCPToolParameter(
                    name="db_compatibility",
                    type="string",
                    required=False,
                    default="A",
                    description="数据库兼容性模式，支持通用名(oracle/pg/mysql/sqlserver)或Vastbase编码(A/B/PG/MSSQL)，自动转换为目标库格式，默认A"
                ),
                MCPToolParameter(
                    name="params",
                    type="string",
                    required=False,
                    default="",
                    description="GUC参数配置，每行一个，格式: work_mem=2MB\\nwal_buffers=16MB。仅Vastbase临时版本生效。"
                ),
                MCPToolParameter(
                    name="postgresql_conf",
                    type="string",
                    required=False,
                    default="",
                    description="postgresql.conf 文件内容(base64编码或纯文本)。仅Vastbase临时版本生效。"
                ),
                MCPToolParameter(
                    name="pg_hba_conf",
                    type="string",
                    required=False,
                    default="",
                    description="pg_hba.conf 文件内容(base64编码或纯文本)。仅Vastbase临时版本生效。"
                ),
                MCPToolParameter(
                    name="extra_files",
                    type="string",
                    required=False,
                    default="",
                    description="额外挂载文件列表，JSON数组格式: [{\"name\": \"init.sql\", \"content\": \"base64内容\"}]。文件统一放到 /docker-entrypoint-initdb.d/。仅Vastbase临时版本生效。"
                ),
                MCPToolParameter(
                    name="explain",
                    type="boolean",
                    required=False,
                    default=False,
                    description="是否使用EXPLAIN模式查看执行计划而不实际执行"
                ),
            ]
        ))
        
        # 获取数据库列表工具
        tools.append(MCPTool(
            name="list_databases",
            description="获取支持的数据库类型及版本列表",
            parameters=[]
        ))
        
        # 获取数据库版本工具
        tools.append(MCPTool(
            name="list_db_versions",
            description="获取指定数据库类型支持的版本列表",
            parameters=[
                MCPToolParameter(
                    name="db_type",
                    type="string",
                    required=True,
                    description=f"数据库类型，支持: {', '.join(databases)}"
                )
            ]
        ))
        
        return tools
    
    def call_tool(self, tool_name: str, parameters: Dict[str, Any]) -> MCPResult:
        """调用指定的MCP工具"""
        try:
            if tool_name == "execute_sql":
                return self._execute_sql(parameters)
            elif tool_name == "list_databases":
                return self._list_databases()
            elif tool_name == "list_db_versions":
                return self._list_db_versions(parameters)
            else:
                return MCPResult(
                    status="error",
                    error=MCPError(
                        type="tool_not_found",
                        message=f"Unknown tool: {tool_name}"
                    )
                )
        except Exception as e:
            return MCPResult(
                status="error",
                error=MCPError(
                    type="execution_error",
                    message=str(e)
                )
            )
    
    def _execute_sql(self, parameters: Dict[str, Any]) -> MCPResult:
        """执行SQL"""
        db_type = parameters.get("db_type")
        version = parameters.get("version")
        query = parameters.get("query")
        db_compatibility = parameters.get("db_compatibility")

        if not db_type or not version or not query:
            return MCPResult(
                status="error",
                error=MCPError(
                    type="invalid_parameters",
                    message="Missing required parameters: db_type, version, query"
                )
            )

        extra_files_raw = parameters.get("extra_files")
        extra_files = None
        if extra_files_raw:
            try:
                extra_files = json.loads(extra_files_raw) if isinstance(extra_files_raw, str) else extra_files_raw
            except (json.JSONDecodeError, TypeError):
                pass

        result = self.executor.execute(
            db_type, version, query,
            db_compatibility=db_compatibility,
            explain=parameters.get("explain", False),
            params=parameters.get("params"),
            postgresql_conf=parameters.get("postgresql_conf"),
            pg_hba_conf=parameters.get("pg_hba_conf"),
            extra_files=extra_files,
        )
        
        if result["status"] == "success":
            data = result["data"]
            if isinstance(data, list):
                return MCPResult(status="success", content={"results": data})
            return MCPResult(
                status="success",
                content={
                    "columns": data.get("columns", []),
                    "rows": data.get("rows", []),
                    "row_count": data.get("row_count", 0),
                    "start_time": result.get("start_time"),
                    "end_time": result.get("end_time"),
                    "elapsed_ms": result.get("elapsed_ms"),
                }
            )
        else:
            return MCPResult(
                status="error",
                error=MCPError(
                    type="sql_execution_error",
                    message=result.get("message", "Unknown error")
                )
            )
    
    def _list_databases(self) -> MCPResult:
        """获取数据库类型及版本列表"""
        databases = []
        for db_type in ConfigManager.get_supported_databases():
            versions = ConfigManager.get_db_versions(db_type)
            databases.append({"type": db_type, "versions": versions})
        return MCPResult(
            status="success",
            content={"databases": databases}
        )
    
    def _list_db_versions(self, parameters: Dict[str, Any]) -> MCPResult:
        """获取数据库版本列表"""
        db_type = parameters.get("db_type")
        
        if not db_type:
            return MCPResult(
                status="error",
                error=MCPError(
                    type="invalid_parameters",
                    message="Missing required parameter: db_type"
                )
            )
        
        versions = ConfigManager.get_db_versions(db_type)
        
        if not versions:
            return MCPResult(
                status="error",
                error=MCPError(
                    type="database_not_found",
                    message=f"Database type {db_type} not found"
                )
            )
        
        return MCPResult(
            status="success",
            content={"db_type": db_type, "versions": versions}
        )