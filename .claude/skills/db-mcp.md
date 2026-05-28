---
name: db-mcp
description: 基于 Docker 容器池 + MCP JSON-RPC 协议的多数据库 SQL 执行与验证服务，支持 PostgreSQL/Kingbase/Oracle/MySQL/SQL Server 等 6 种数据库，事务级回滚保证数据零残留。
---

# DB-MCP — 多数据库 SQL 验证服务

## 项目概述

基于 FastAPI 的 SQL 验证服务，实现了 Model Context Protocol (MCP) JSON-RPC 协议。通过 Docker 容器化按需启动数据库实例，执行 SQL 后通过事务回滚（DML）或容器销毁（DDL）保证数据零残留。

**核心能力：**
- 6 种数据库、20+ 个版本的 SQL 执行
- Docker 容器自动启动 + DBUtils 连接池
- 智能 SQL 拆分（处理 PL/SQL 块、dollar-quote、字符串文本、各类注释）
- EXPLAIN 模式查看执行计划
- OAuth 2.0 + API Key 双重认证
- 临时容器：支持自定义 postgresql.conf / pg_hba.conf / GUC 参数（仅 Vastbase）

## 启动服务

```bash
cd <项目根目录>
pip install -r requirements.txt
python -m src.main
```

服务监听 `http://0.0.0.0:8000`，Swagger 文档在 `http://localhost:8000/docs`。

**前置条件：** Docker 守护进程必须运行。容器镜像首次使用时自动拉取。

## 停止服务

```bash
curl -X POST http://localhost:8000/api/shutdown
```

## 快速验证

```bash
# 健康检查（无需认证）
curl http://localhost:8000/api/health

# 查看支持的数据库
curl http://localhost:8000/api/databases

# 执行 SQL（需要先注册客户端获取 API Key）
curl -X POST http://localhost:8000/api/execute_sql \
  -H "Authorization: Bearer <api_key>" \
  -H "Content-Type: application/json" \
  -d '{"db_type": "postgresql", "version": "14", "query": "SELECT 1"}'
```

## API 端点

### 公开接口（无需认证）

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| GET | `/api/health` | 健康检查（配置 + Docker 状态 + 容器池状态） |
| GET | `/api/databases` | 获取支持的数据库类型及版本列表 |
| GET | `/api/databases/{db_type}/versions` | 获取指定数据库的版本列表 |
| POST | `/api/clients/register` | 注册新客户端（返回 API Key） |
| GET | `/api/clients` | 列出所有已注册客户端 |
| DELETE | `/api/clients/{client_id}` | 注销客户端 |

### 认证接口（需要 `Authorization: Bearer <key>`）

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| POST | `/api/execute_sql` | 执行 SQL（核心接口） |
| POST | `/api/dify/execute_sql` | Dify 专用 SQL 执行接口 |

### MCP JSON-RPC

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| POST | `/` 或 `/mcp` | JSON-RPC 入口（initialize / tools/list / tools/call） |
| GET | `/sse` | SSE 事件流 |
| POST | `/messages` | SSE 消息端点 |

## 支持的数据库及版本

| 数据库 | 版本 | 端口 | DDL 事务支持 | 驱动 |
|----------|----------|------|-----------------|--------|
| Vastbase | 2.2.15, 3.0.8, 3.0.9 | 5432 | 支持 | psycopg2 |
| PostgreSQL | 12, 13, 14 | 5432 | 支持 | psycopg2 |
| 金仓 (Kingbase) | V8, V9 | 54321 | 不支持（PG 模式支持） | psycopg2 |
| Oracle | 11c, 12c, 18c, 19c, 21c | 1521 | 不支持 | oracledb |
| MySQL | 5.6, 5.7, 8.0 | 3306 | 不支持 | pymysql |
| SQL Server | 2017, 2019 | 1433 | 支持 | pymssql |

## `/api/execute_sql` 请求格式

```json
{
  "db_type": "vastbase",
  "version": "3.0.8",
  "query": "SELECT * FROM pg_tables LIMIT 5",
  "db_compatibility": "pg",
  "explain": false,
  "params": "work_mem=2MB\nwal_buffers=16MB",
  "postgresql_conf": "<base64 或纯文本>",
  "pg_hba_conf": "<base64 或纯文本>",
  "extra_files": [{"name": "init.sql", "content": "base64..."}]
}
```

**字段说明：**
- `db_type`（必填）：数据库类型，大小写不敏感
- `version`（必填）：版本号，不同数据库格式不同：
  - **Vastbase** 支持三种格式：
    - 基础版本：`3.0.8`、`3.0.9`、`2.2.15`
    - PSU 补丁版本：`3.0.8.psu0`（从 Nexus 自动下载构建）
    - 指定 Build 号：`3.0.8.24875`（从 Nexus 自动下载构建）
  - **Kingbase**：`v8`、`v9`
  - **PostgreSQL**：`12`、`13`、`14`
  - **Oracle**：`11c`、`12c`、`18c`、`19c`、`21c`
  - **MySQL**：`5.6`、`5.7`、`8.0`
  - **SQL Server**：`2017`、`2019`
- `query`（必填）：SQL 语句，最大 5000 字符
- `db_compatibility`（可选）：兼容模式 — `oracle`、`pg`、`mysql`、`sqlserver` 或 Vastbase 编码 `A`、`B`、`PG`、`MSSQL`。自动转换为目标库格式
- `explain`（可选）：设为 `true` 查看执行计划而非实际执行
- `params`、`postgresql_conf`、`pg_hba_conf`、`extra_files`（可选）：临时容器配置，仅 Vastbase 生效

## `/api/execute_sql` 响应格式

**单语句成功：**
```json
{
  "status": "success",
  "data": {
    "columns": ["table_name", "table_schema"],
    "rows": [{"table_name": "pg_statistic", "table_schema": "pg_catalog"}],
    "row_count": 1
  },
  "start_time": "2026-05-28T10:00:00+00:00",
  "end_time": "2026-05-28T10:00:00.050+00:00",
  "elapsed_ms": 50.0
}
```

**多语句成功：**
```json
{
  "status": "success",
  "data": [
    {"statement": "SELECT 1", "status": "success", "data": {...}},
    {"statement": "SELECT 2", "status": "success", "data": {...}}
  ]
}
```

**错误：**
```json
{
  "status": "error",
  "message": "relation \"nonexistent\" does not exist"
}
```

**DDL 提示（非事务型数据库）：**
```json
{
  "status": "success",
  "data": {...},
  "note": "DDL executed on database that does not support transactional DDL, container will be destroyed"
}
```

## MCP 工具及参数

MCP JSON-RPC 接口对外暴露 3 个工具，通过 `tools/list` 获取，`tools/call` 调用。

### `execute_sql` — 执行 SQL

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `db_type` | string | 是 | — | 数据库类型，支持: vastbase, kingbase, postgresql, oracle, mysql, sqlserver |
| `version` | string | 是 | — | 数据库版本。**Vastbase** 支持三种格式：基础版本(`3.0.8`)、PSU补丁(`3.0.8.psu0`)、指定Build号(`3.0.8.24875`)；**Kingbase**：`v8`/`v9`；**PostgreSQL**：`12`/`13`/`14`；**Oracle**：`11c`/`12c`/`18c`/`19c`/`21c`；**MySQL**：`5.6`/`5.7`/`8.0`；**SQL Server**：`2017`/`2019` |
| `query` | string | 是 | — | 要执行的 SQL 语句 |
| `db_compatibility` | string | 否 | `A` | 兼容性模式，支持通用名(`oracle`/`pg`/`mysql`/`sqlserver`)或 Vastbase 编码(`A`/`B`/`PG`/`MSSQL`)，自动转换为目标库格式 |
| `params` | string | 否 | — | GUC 参数配置，每行一个，格式: `work_mem=2MB\nwal_buffers=16MB`。仅 Vastbase 临时版本生效 |
| `postgresql_conf` | string | 否 | — | postgresql.conf 文件内容(base64编码或纯文本)。仅 Vastbase 临时版本生效 |
| `pg_hba_conf` | string | 否 | — | pg_hba.conf 文件内容(base64编码或纯文本)。仅 Vastbase 临时版本生效 |
| `extra_files` | string | 否 | — | 额外挂载文件列表，JSON 数组: `[{"name":"init.sql","content":"base64内容"}]`。仅 Vastbase 临时版本生效 |
| `explain` | boolean | 否 | `false` | 是否使用 EXPLAIN 模式查看执行计划 |

### `list_databases` — 获取支持的数据库及版本列表

无参数。返回所有已配置的数据库类型及其版本列表。

### `list_db_versions` — 获取指定数据库的版本列表

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `db_type` | string | 是 | 数据库类型，支持: vastbase, kingbase, postgresql, oracle, mysql, sqlserver |

## 架构

### 核心文件

| 文件 | 职责 |
|------|------|
| [src/main.py](src/main.py) | 入口，uvicorn 启动 |
| [src/api.py](src/api.py) | FastAPI 应用创建 + lifespan（预热 + 优雅关闭） |
| [src/config_manager.py](src/config_manager.py) | YAML 配置加载 + Pydantic 校验（类级单例） |
| [src/container_pool.py](src/container_pool.py) | Docker 容器生命周期、连接池、并发控制 |
| [src/executor.py](src/executor.py) | SQL 执行引擎：拆分、DDL/DML 路由、事务管理 |
| [src/exceptions.py](src/exceptions.py) | 异常体系（精确 HTTP 状态码映射） |
| [src/dependencies.py](src/dependencies.py) | 惰性单例工厂，依赖注入 |
| [src/client_registry.py](src/client_registry.py) | API Key + OAuth 客户端管理（内存存储） |
| [src/package_manager.py](src/package_manager.py) | Nexus 二进制包下载/解压/缓存（临时版本） |
| [src/nexus_client.py](src/nexus_client.py) | Nexus 仓库客户端（搜索 + 下载） |
| [src/adapters/base.py](src/adapters/base.py) | DBAdapter 抽象基类 + `@register_adapter` 装饰器 |
| [src/adapters/*.py](src/adapters/) | 6 个数据库适配器实现 |
| [src/mcp/dify_mcp.py](src/mcp/dify_mcp.py) | MCP 处理器：工具定义 + 调用分发 |
| [src/routes/execute_routes.py](src/routes/execute_routes.py) | REST API 路由 |
| [src/routes/mcp_routes.py](src/routes/mcp_routes.py) | MCP JSON-RPC + SSE 路由 |
| [src/routes/oauth_routes.py](src/routes/oauth_routes.py) | OAuth DCR / 授权 / Token 路由 |
| [config/databases.yaml](config/databases.yaml) | 数据库配置（Pydantic 校验的 YAML） |

### ContainerPool — 核心引擎

`ContainerPool`（[container_pool.py](src/container_pool.py)）是线程安全的全局单例，管理所有 Docker 容器的生命周期、连接池和并发控制。

**单容器生命周期：**
1. 首次 `lease()` → 检查容器是否存在且健康
2. 不存在/不健康 → `docker pull` 镜像 → `docker run` → 等待端口 → 等待数据库就绪（TCP 连接 + SELECT 1）→ 创建 DBUtils PooledDB 连接池
3. 获取信号量（默认最大并发 10）→ 从连接池借出连接 → 返回 `ContainerLease`
4. `lease.__exit__()` 时 → 归还连接到池 → 释放信号量
5. 若调用了 `mark_for_destroy()` → 等所有租约释放后销毁容器

**健康监控：** 后台 daemon 线程每 30s 检查端口可达性。连续 3 次失败 → 标记 UNHEALTHY → 下次 lease 自动重建。每日午夜清理当天未使用的容器。

**并发控制：** 每个容器一个 `BoundedSemaphore(max_concurrency)` + DBUtils PooledDB。超限 → HTTP 503。

### SQL 执行流程

1. 请求到达 `/api/execute_sql` → API Key 认证
2. 通过 `asyncio.to_thread()` 调用 `MCPExecutor.execute()`（非阻塞）
3. 归一化 Unicode 空白字符 → 拆分 SQL 为多条语句（处理 PL/SQL 块、字符串文本、注释、dollar-quote）
4. 逐条判断 DDL / DML
5. 路由到对应执行路径：

```
DDL + 非事务型数据库（金仓/Oracle/MySQL）？
  → 直接执行 → 尝试反向 DDL 清理 → 清理失败则销毁容器

DDL + 事务型数据库（PostgreSQL/Vastbase/SQL Server）？
  → 事务包裹 → 执行 → 回滚（数据干净，容器复用）

DML（INSERT/UPDATE/DELETE/SELECT）？
  → 事务包裹 → 执行 → 回滚（数据干净，容器复用）

非事务语句（CREATE DATABASE、VACUUM、CREATE INDEX CONCURRENTLY 等）？
  → 直接执行
```

**多语句处理：**
- 事务型数据库：所有语句在同一事务中执行 → 末尾统一回滚
- 非事务型数据库：DDL 直接执行 + 反向 DDL 清理；DML 批量事务回滚

### 添加新数据库适配器

1. 创建 `src/adapters/<name>.py`，类继承 `DBAdapter`
2. 用 `@register_adapter('name')` 装饰
3. 在 `src/adapters/__init__.py` 中导出
4. 在 `config/databases.yaml` 中添加配置
5. 在 `tests/test_adapters.py` 中添加测试

## 测试

```bash
# 运行全部测试
pytest

# Windows：
$env:PYTHONPATH="."; python -m pytest
```

**测试文件：**
- `test_adapters.py` — 适配器初始化、注册表、结果格式化
- `test_api.py` — API 路由集成测试
- `test_config_manager.py` — 配置加载、查询、单例
- `test_executor.py` — DML/DDL 路径、SQL 拆分（dollar-quote、backtick、E-string 等）、ContainerPool 集成
- `test_container_pool.py` — 单例、线程安全、lease/release、信号量容量、并发创建防护

测试均为纯单元测试，不依赖真实数据库或 Docker，所有外部依赖均被 mock。

## 环境变量

| 变量 | 默认值 | 说明 |
|----------|---------|---------|
| `MCP_HOST` | `0.0.0.0` | 服务监听地址 |
| `MCP_PORT` | `8000` | 服务监听端口 |
| `MCP_MAX_CONCURRENCY` | `10` | 单容器最大并发执行数 |
| `MCP_LEASE_TIMEOUT` | `30` | 信号量获取超时（秒） |
| `MCP_DB_READY_TIMEOUT` | `300` | 数据库就绪等待超时（秒） |
| `MCP_CONTAINER_IDLE_TTL` | `86400` | 空闲容器保留时间（秒，默认 24 小时） |
| `MCP_HEALTH_CHECK_INTERVAL` | `30` | 健康检查间隔（秒） |
| `MCP_STATEMENT_TIMEOUT` | `30` | 单条 SQL 执行超时（秒） |
| `MCP_QUERY_TIMEOUT` | `3600` | HTTP 请求总超时（秒） |
| `MCP_DOCKER_BUILD_TIMEOUT` | `1800` | Docker 镜像构建超时（秒） |
| `MCP_RESOURCE_CPU_<TYPE>` | 按数据库类型 | 覆盖 CPU 限制，如 `MCP_RESOURCE_CPU_VASTBASE=4` |
| `MCP_RESOURCE_MEM_<TYPE>` | 按数据库类型 | 覆盖内存限制，如 `MCP_RESOURCE_MEM_VASTBASE=4g` |

## 已知限制

- 客户端数据仅存内存，重启丢失
- API Key 过期时间已声明但未实际强制执行
- `list_db_versions` 仅返回 YAML 中配置的版本，不含临时（Nexus 构建）版本
- 健康监控仅检查端口可达性，不验证 SQL 响应能力
- 无速率限制
- 无 CI/CD 流水线配置
- Docker 资源限制（CPU/内存）已在代码中定义但被注释掉，实际未生效
