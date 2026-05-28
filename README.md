# DB-MCP 数据库SQL验证服务

## 系统概述

DB-MCP 是一个基于 Model Context Protocol (MCP) 的数据库 SQL 验证服务，为智能答疑平台提供高效、灵活的多数据库 SQL 执行与结果验证能力。

系统作为智能 Agent 与验证环境之间的桥梁，通过 Docker 容器化技术快速启动数据库实例，支持事务级回滚或容器销毁来保证数据零残留。

## 核心功能

- 支持多种数据库：Vastbase、金仓（Kingbase）、PostgreSQL、Oracle、MySQL、SQL Server
- 多版本支持：同一数据库的不同版本可同时配置和运行
- Docker 容器化：带容器命名和幂等启动（已运行则复用），端口自动映射
- 容器预热池：启动时预热配置容器，空闲容器可复用
- 容器健康监控：后台线程定期检查端口可达性，异常容器自动重建
- ephemeral 版本：不在 yaml 中的版本自动从 Nexus 下载二进制，Docker 挂载运行
- 事务级回滚：DML 操作通过显式事务实现毫秒级数据回退
- DDL 兜底策略：不支持 DDL 事务的数据库通过反向 DDL 清理或容器销毁
- 多 SQL 语句执行：支持分号分隔的多条 SQL，智能拆分（处理字符串、注释、各类引号、PL/SQL 块）
- EXPLAIN 模式：支持 REST API 和 MCP 接口查看执行计划
- 并发控制：BoundedSemaphore + PooledDB 连接池，单容器最多 10 个并发执行
- MCP JSON-RPC 协议：支持 `initialize`、`tools/list`、`tools/call`、SSE
- OAuth 2.0 授权：DCR 动态客户端注册 + Authorization Code 流程
- API Key 认证：客户端注册、管理、轮换
- 结构化审计日志：记录每次 SQL 执行的客户端、数据库、查询预览、状态
- RESTful API：基于 FastAPI 提供 HTTP 接口，附带 Swagger/ReDoc 文档

## 技术栈

- Python 3.8+
- FastAPI + uvicorn
- Docker SDK
- psycopg2（PostgreSQL / Vastbase / 金仓）
- pymysql（MySQL）
- oracledb（Oracle）
- pymssql（SQL Server）
- PyYAML

## 项目结构

```
db-mcp/
├── config/
│   ├── databases.yaml              # 数据库配置
│   └── dockerfile_templates/       # Dockerfile 模板
│       └── vastbase/
├── src/
│   ├── __init__.py
│   ├── main.py                     # 服务入口
│   ├── api.py                      # FastAPI 应用创建 + 路由挂载
│   ├── config_manager.py           # 配置加载（YAML → Pydantic 校验 + 线程锁）
│   ├── container_pool.py           # 容器池（单例 + 连接池 + 信号量 + 健康监控）
│   ├── executor.py                 # 执行引擎（SQL 拆分、DDL/DML 路由）
│   ├── exceptions.py               # 异常体系（精确 HTTP 状态码映射）
│   ├── dependencies.py             # 惰性单例依赖注入
│   ├── client_registry.py          # 客户端注册表（API Key + OAuth，线程安全）
│   ├── nexus_client.py             # Nexus 仓库客户端
│   ├── package_manager.py          # 包管理器（下载/解压/缓存）
│   ├── adapters/
│   │   ├── __init__.py             # 导出 ADAPTER_REGISTRY + 适配器类
│   │   ├── base.py                 # 抽象基类 DBAdapter + 自动注册装饰器
│   │   ├── vastbase.py             # Vastbase 适配器
│   │   ├── kingbase.py             # 金仓适配器
│   │   ├── postgresql.py           # PostgreSQL 适配器
│   │   ├── mysql.py                # MySQL 适配器
│   │   ├── oracle.py               # Oracle 适配器
│   │   └── mssql.py                # SQL Server 适配器
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── dify_mcp.py             # MCP 协议处理器（工具定义 + 调用）
│   └── routes/
│       ├── __init__.py             # 路由聚合导出
│       ├── execute_routes.py       # SQL 执行 + 数据库信息 + 健康检查
│       ├── mcp_routes.py           # MCP JSON-RPC + SSE 端点
│       ├── oauth_routes.py         # OAuth DCR / 授权 / Token 交换
│       └── client_routes.py        # 客户端管理 + Dify MCP 集成
├── tests/
│   ├── test_adapters.py
│   ├── test_api.py
│   ├── test_config_manager.py
│   ├── test_executor.py
│   └── test_container_pool.py
├── requirements.txt
└── README.md
```

## 安装与运行

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
python -m src.main
```

服务将在 http://localhost:8000 启动。

### API 文档

- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## API 接口

### 数据库信息

```
GET  /api/databases                        # 获取支持的数据库类型列表
GET  /api/databases/{db_type}/versions     # 获取指定数据库的版本列表
```

### SQL 执行

```
POST /api/execute_sql      # 执行 SQL（需认证）
POST /api/dify/execute_sql # Dify 专用 SQL 执行接口（需认证）
```

请求示例：

```json
{
    "db_type": "postgresql",
    "version": "14",
    "query": "SELECT 1"
}
```

可选字段：

- `db_compatibility`: 兼容性模式（oracle/pg/mysql/sqlserver 或 A/B/PG/MSSQL），自动转换为目标库格式
- `explain`: 设为 `true` 查看执行计划而非实际执行
- `params`: GUC 参数配置（仅 Vastbase 临时版本）
- `postgresql_conf`: postgresql.conf 内容（仅 Vastbase 临时版本）
- `pg_hba_conf`: pg_hba.conf 内容（仅 Vastbase 临时版本）
- `extra_files`: 额外挂载文件列表（仅 Vastbase 临时版本）

响应示例：

```json
{
    "status": "success",
    "data": {
        "columns": ["?column?"],
        "rows": [{"?column?": 1}],
        "row_count": 1
    }
}
```

多语句响应示例：

```json
{
    "status": "success",
    "data": [
        {"statement": "SELECT 1", "status": "success", "data": {...}},
        {"statement": "SELECT 2", "status": "success", "data": {...}}
    ]
}
```

### 健康检查

```
GET /api/health     # 返回 healthy / degraded + config + docker 状态
```

### MCP JSON-RPC

```
POST /               # JSON-RPC 入口 (initialize / tools/list / tools/call)
POST /mcp            # JSON-RPC 入口（/mcp 路径）
GET  /sse            # SSE 端点
POST /messages       # SSE 消息端点
GET  /mcp            # MCP 服务信息
GET  /mcp/tools      # MCP 工具列表
```

### OAuth

```
POST /register       # DCR 动态客户端注册
GET  /authorize      # OAuth 授权端点
POST /token          # Token 交换端点
```

### 客户端管理

```
POST   /api/clients/register             # 注册新客户端
GET    /api/clients                      # 列出所有客户端
DELETE /api/clients/{client_id}          # 注销客户端
POST   /api/clients/{client_id}/rotate-key   # 轮换 API Key
PATCH  /api/clients/{client_id}          # 更新客户端信息
POST   /mcp/call                         # MCP 工具调用
POST   /console/api/mcp/oauth/callback   # Dify OAuth 回调
```

## 配置说明

配置文件 `config/databases.yaml` 按数据库类型和版本组织：

```yaml
databases:
  <db_type>:
    versions:
      "<version>":
        image: "<docker_image>"
        port: <container_port>
        adapter: "<AdapterClassName>"
        username: "<user>"
        password: "<password>"
        database: "<database>"
        privileged: true/false     # 可选：特权模式
        env:                       # 可选：自定义环境变量
          KEY: "value"
```

当前已配置的数据库：

| 数据库 | 版本 |
|--------|------|
| Vastbase | 2.2.15, 3.0.8, 3.0.9 |
| 金仓 (Kingbase) | V8, V9 |
| PostgreSQL | 12, 13, 14 |
| Oracle | 11c, 12c, 18c, 19c, 21c |
| MySQL | 5.6, 5.7, 8.0 |
| SQL Server | 2017, 2019 |

## 数据回滚与容器生命周期

每次 SQL 执行的决策逻辑：

```
收到 SQL
  ├─ 是否为 DDL？
  │   ├─ 否（DML/SELECT）→ execute_with_rollback() → 事务回滚，数据干净 → 容器可复用
  │   └─ 是 → 适配器 supports_ddl_transaction？
  │          ├─ True  → execute_with_rollback() → 事务回滚，数据干净 → 容器可复用
  │          └─ False → execute() 直接执行 → 数据无法回滚 → 容器必须销毁
  └─ 执行结束后 → stop_container()
```

**核心原则：数据能否回滚决定了容器能不能复用。** 如果通过事务回滚了，容器数据是干净的，只需正常 stop，预热池可以继续复用。只有当 DDL 在不支持事务的数据库上直接执行后，数据无法回退，容器才需要销毁重建。

### DML 操作（INSERT / UPDATE / DELETE / SELECT）

1. 连接时设置 `autocommit = False`
2. 将 SQL 包裹在显式事务中执行
3. 无论执行成功与否，强制 `ROLLBACK`
4. 数据零残留，容器可正常 stop 并复用
5. 耗时通常 < 50ms

### DDL 操作（CREATE / ALTER / DROP / TRUNCATE / RENAME）

| 数据库 | DDL 事务支持 | DDL 策略 | 容器处理 |
|--------|------------|---------|---------|
| Vastbase | 支持 | 事务回滚 | 正常 stop，可复用 |
| PostgreSQL | 支持 | 事务回滚 | 正常 stop，可复用 |
| SQL Server | 支持 | 事务回滚 | 正常 stop，可复用 |
| 金仓 | 不支持 | 直接执行 | 需销毁重建 |
| Oracle | 不支持 | 直接执行 | 需销毁重建 |
| MySQL | 不支持 | 直接执行 | 需销毁重建 |

> DDL 在不支持事务的数据库上直接执行后，响应中会包含 `"note": "DDL executed, container will be destroyed"`，随后容器被销毁以确保后续请求拿到干净环境。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MCP_HOST` | `0.0.0.0` | 服务监听地址 |
| `MCP_PORT` | `8000` | 服务监听端口 |
| `MCP_LOG_DIR` | `./logs/` | 日志目录 |
| `MCP_BASE_URL` | `http://localhost:8000` | 服务对外 URL（OAuth 使用） |
| `MCP_MAX_CONCURRENCY` | `10` | 单容器最大并发执行数 |
| `MCP_LEASE_TIMEOUT` | `30` | 并发槽位等待超时（秒） |
| `MCP_DB_READY_TIMEOUT` | `300` | 数据库就绪等待超时（秒） |
| `MCP_CONTAINER_IDLE_TTL` | `86400` | 空闲容器保留时间（秒），默认 24 小时 |
| `MCP_HEALTH_CHECK_INTERVAL` | `30` | 健康检查间隔（秒） |
| `MCP_STATEMENT_TIMEOUT` | `30` | 单条 SQL 执行超时（秒） |
| `MCP_QUERY_TIMEOUT` | `3600` | HTTP 请求总超时（秒），含镜像构建 |
| `MCP_DOCKER_BUILD_TIMEOUT` | `1800` | Docker 镜像构建超时（秒） |
| `MCP_NEXUS_DOWNLOAD_TIMEOUT` | `600` | Nexus 下载超时（秒） |
| `MCP_PACKAGE_CACHE_DIR` | `./data/packages/` | 二进制包缓存目录 |
| `MCP_RESOURCE_CPU_<TYPE>` | 按数据库类型 | 覆盖 CPU 限制，如 `MCP_RESOURCE_CPU_VASTBASE=4` |
| `MCP_RESOURCE_MEM_<TYPE>` | 按数据库类型 | 覆盖内存限制，如 `MCP_RESOURCE_MEM_VASTBASE=4g` |

## 安全注意事项

1. 本工具仅供内网使用，请勿暴露到公网
2. 所有 SQL 执行需通过 API Key 认证（`Authorization: Bearer <key>`）
3. 容器使用 `--rm` 参数，停止后自动删除
4. DML 操作通过事务回滚保证数据零残留，容器可复用
5. 支持 DDL 事务的数据库（Vastbase、PostgreSQL、SQL Server）DDL 也会回滚，容器保持干净
6. 不支持 DDL 事务的数据库（金仓、Oracle、MySQL）DDL 执行后容器会被销毁重建
7. 建议配置适当的资源限制（CPU、内存）
