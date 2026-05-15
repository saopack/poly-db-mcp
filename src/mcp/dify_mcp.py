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
        self.executor = MCPExecutor()
    
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
                    description="数据库版本"
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
                    description="Vastbase数据库兼容性模式: A=Oracle, B=MySQL, PG=PostgreSQL，默认A"
                )
            ]
        ))
        
        # 获取数据库列表工具
        tools.append(MCPTool(
            name="list_databases",
            description="获取支持的数据库类型列表",
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

        result = self.executor.run_validation(
            db_type, version, query,
            db_compatibility=db_compatibility
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
        """获取数据库类型列表"""
        databases = ConfigManager.get_supported_databases()
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