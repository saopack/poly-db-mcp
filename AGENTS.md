# AGENTS.md — DB-MCP 数据库 SQL 验证服务

> 本文件面向 AI 编码助手。阅读者应对本项目一无所知。所有信息均基于项目实际代码。

---

## 项目概述

DB-MCP 是一个基于 FastAPI 的 SQL 验证服务，实现了 Model Context Protocol (MCP) JSON-RPC 协议，为智能答疑平台提供多数据库 SQL 执行与结果验证能力。

系统通过 Docker 容器化快速启动数据库实例，执行 SQL 后通过事务回滚（DML）或容器销毁（DDL）保证数据零残留。

### 核心功能

- 6 种数据库支持：Vastbase、金仓、PostgreSQL、Oracle、MySQL、SQL Server
- 多版本：同一数据库可配置多个版本
- 容器命名 + 幂等启动：同名容器已运行则复用
- 容器预热池：空闲容器 TTL 5 分钟，减少重复请求冷启动
- 事务级回滚：DML 通过显式事务实现毫秒级回退
- DDL 兜底：不支持 DDL 事务的数据库自动销毁容器
- 多 SQL 拆分：智能状态机解析分号分隔的多条语句（处理字符串文本、标识符引用、各类注释和 dollar-quote）
- EXPLAIN 模式：前置 `EXPLAIN` 查看执行计划
- MCP JSON-RPC：`initialize`、`tools/list`、`tools/call`、SSE 流
- OAuth 2.0：DCR (RFC 7591) + Authorization Code 流程
- API Key 认证 + 客户端 CRUD + 密钥轮换
- 结构化审计日志
- Pydantic 配置校验
- 分层异常体系（精确 HTTP 状态码）

---

## 技术栈

- **Python**: 3.8+
- **Web 框架**: FastAPI + uvicorn
- **Docker**: docker SDK
- **数据库驱动**:
  - `psycopg2`（PostgreSQL / Vastbase / 金仓）
  - `pymysql`（MySQL）
  - `oracledb`（Oracle）
  - `pymssql`（SQL Server）
- **配置**: PyYAML + Pydantic
- **测试**: pytest + httpx

---

## 项目结构

```
db-mcp/
├── config/
│   └── databases.yaml              # YAML 配置（Pydantic 校验）
├── src/
│   ├── __init__.py                 # 公开接口导出（不含 DockerManager）
│   ├── main.py                     # 入口：uvicorn 启动
│   ├── api.py                      # FastAPI 应用创建 + lifespan + 4 路由器挂载
│   ├── config_manager.py           # 配置管理（类级单例 + 线程锁 + Pydantic 模型校验）
│   ├── docker_manager.py           # 容器生命周期（命名、幂等启动、预热池、TTL 清理）
│   ├── executor.py                 # 执行引擎（SQL 拆分、DDL/DML 路由、异常分类处理）
│   ├── exceptions.py               # 异常体系（含 http_status）
│   ├── dependencies.py             # 惰性单例工厂（get_mcp_handler / get_client_registry）
│   ├── client_registry.py          # 客户端注册表（API Key + OAuth，threading.Lock）
│   ├── adapters/
│   │   ├── __init__.py             # ADAPTER_REGISTRY 导出
│   │   ├── base.py                 # DBAdapter 抽象基类 + register_adapter 装饰器
│   │   ├── vastbase.py             # Vastbase (psycopg2)
│   │   ├── kingbase.py             # 金仓 (psycopg2)
│   │   ├── postgresql.py           # PostgreSQL (psycopg2)
│   │   ├── mysql.py                # MySQL (pymysql)
│   │   ├── oracle.py               # Oracle (oracledb)
│   │   └── mssql.py                # SQL Server (pymssql)
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── dify_mcp.py             # MCP Handler：工具定义 + 调用分发
│   └── routes/
│       ├── __init__.py             # mcp_router, oauth_router, client_router, validation_router
│       ├── mcp_routes.py           # MCP JSON-RPC (/、/mcp、/sse、/messages、/mcp/tools 等)
│       ├── oauth_routes.py         # OAuth DCR / authorize / token
│       ├── client_routes.py        # 客户端 CRUD + /mcp/call + OAuth 回调
│       └── validation_routes.py    # /api/execute_sql、/api/databases、/api/health
├── tests/
│   ├── test_adapters.py
│   ├── test_api.py
│   ├── test_config_manager.py
│   └── test_executor.py
├── requirements.txt
└── AGENTS.md
```

---

## 构建与运行

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动服务

```bash
python -m src.main
```

服务监听 `http://0.0.0.0:8000`。

### API 文档

- Swagger: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## 完整端点表

### SQL 执行与信息

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/api/databases` | 无 | 支持的数据库类型列表 |
| GET | `/api/databases/{db_type}/versions` | 无 | 指定数据库的版本列表 |
| POST | `/api/execute_sql` | API Key | 执行 SQL（支持 explain、db_compatibility） |
| POST | `/api/dify/execute_sql` | API Key | Dify 专用 SQL 执行 |
| GET | `/api/health` | 无 | 健康检查（config + docker 状态） |

### MCP JSON-RPC

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | MCP 根路径（服务信息 + OAuth 元数据） |
| POST | `/` | JSON-RPC 入口 |
| POST | `/mcp` | JSON-RPC 入口（/mcp） |
| GET | `/sse` | SSE 事件流 |
| POST | `/messages` | SSE 消息端点 |
| GET | `/mcp` | MCP 服务信息 |
| GET | `/mcp/tools` | MCP 工具列表 |

### OAuth

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/register` | DCR 动态客户端注册 |
| GET | `/authorize` | OAuth 授权端点 |
| POST | `/token` | Token 交换（authorization_code → access_token） |
| GET | `/.well-known/oauth-authorization-server` | OAuth 服务发现 |

### 客户端管理

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/clients/register` | 无 | 注册新客户端 |
| GET | `/api/clients` | 无 | 列出所有客户端 |
| DELETE | `/api/clients/{client_id}` | 无 | 注销客户端 |
| PATCH | `/api/clients/{client_id}` | 无 | 更新客户端 |
| POST | `/api/clients/{client_id}/rotate-key` | 无 | 轮换 API Key |
| POST | `/mcp/call` | 可选 | MCP 工具调用 |
| POST | `/console/api/mcp/oauth/callback` | 可选 | Dify OAuth 回调 |

---

## 测试

```bash
pytest
# Windows:
$env:PYTHONPATH="."; python -m pytest
```

### 测试文件

| 文件 | 覆盖内容 |
|------|---------|
| `test_adapters.py` | 适配器初始化、注册表、`_format_result` 边界 |
| `test_api.py` | API 路由集成测试（含输入校验 422） |
| `test_config_manager.py` | 配置加载、查询、单例隔离 |
| `test_executor.py` | DML/DDL 路径、无效数据库、容器超时、适配器异常、SQL 拆分（dollar-quote、backtick、E-string） |

### 测试策略

- 纯单元测试，不依赖真实数据库或 Docker
- Mock 外部依赖（DockerManager、适配器）
- API 测试使用 FastAPI TestClient
- 每个 test class 在 `setup_method` 中重置状态

---

## 关键设计模式

### 1. 适配器模式 + 自动注册

所有适配器继承 `DBAdapter`，通过 `@register_adapter('name')` 自动注册到 `ADAPTER_REGISTRY`。`MCPExecutor` 通过 `ADAPTER_REGISTRY.get(db_type)` 动态获取，无需硬编码。

新增适配器步骤：
1. `src/adapters/<name>.py` → 继承 `DBAdapter` + `@register_adapter`
2. `src/adapters/__init__.py` → 导出（可选）
3. `config/databases.yaml` → 添加配置
4. `tests/test_adapters.py` → 添加测试

### 2. 模板方法模式

`DBAdapter.execute_with_rollback()` 定义标准事务包裹逻辑（begin → execute → rollback），子类只需实现原子操作 `execute()`。

### 3. 类级单例 + 线程安全

- `ConfigManager`：类变量 `_config` + `threading.Lock()`
- `ClientRegistry`：实例变量 + `threading.Lock()` 保护所有字典操作
- `dependencies.py`：惰性单例工厂函数，避免 Docker SDK 在模块导入时报错

### 4. 异常体系

```
MCPError (500)
├── ConfigError (500)
│   └── DatabaseNotFoundError (404)
├── AdapterError (502)
│   ├── AdapterConnectionError (502)
│   └── AdapterExecutionError (400)
│       └── AdapterTimeoutError (504)
├── DockerError (500)
│   ├── DockerContainerStartError (500)
│   └── DockerContainerPortError (500)
└── ValidationError (422)
```

每个异常类携带 `http_status`，可在全局异常处理器中直接映射到 HTTP 响应。

### 5. SQL 拆分状态机

`executor._split_sql_statements()` 是模块级函数，按字符遍历 SQL 文本，正确处理：
- 单行注释 `--`
- 块注释 `/* */`
- 单引号字符串 `'...'`（含 `''` 转义）
- 双引号标识符 `"..."`
- Dollar-quote `$$...$$` / `$tag$...$tag$`
- Backtick 标识符 `` `...` ``
- E 转义字符串 `E'...'`

仅当分号出现在这些上下文之外时才作为语句分隔符。

### 6. 容器预热池

`DockerManager._warm_pool: Dict[str, float]` 记录容器最后使用时间。`_try_reuse_warm_container()` 在启动前先检查是否存在已运行的容器并更新 TTL。`_cleanup_stale_warm_containers()` 清理 5 分钟未使用的闲置容器。

---

## 数据回滚与容器生命周期

`executor._execute_single()` 的决策逻辑（[executor.py:192-202](src/executor.py#L192-L202)）：

```
is_ddl AND NOT adapter.supports_ddl_transaction?
  ├─ True  → adapter.execute(stmt)        # 直接执行，数据无法回滚
  │          返回 "note": "DDL executed, container will be destroyed"
  └─ False → adapter.execute_with_rollback(stmt)  # 事务包裹 → ROLLBACK
             数据干净，容器正常 stop 即可复用
```

**关键结论：数据能回滚 = 容器可复用。** 只有 DDL 在不支持事务的数据库（金仓、Oracle、MySQL）上执行时，数据才无法回退，才需要销毁容器。支持 DDL 事务的数据库（Vastbase、PostgreSQL、SQL Server）即使是 DDL 也通过 `execute_with_rollback` 回滚，容器保持干净。

### DML（INSERT / UPDATE / DELETE / SELECT）

1. `autocommit = False`
2. BEGIN → execute SQL → ROLLBACK（强制，无论成功与否）
3. 数据零残留 → 容器正常 stop，可被预热池复用
4. 耗时 < 50ms

### DDL（CREATE / ALTER / DROP / TRUNCATE / RENAME）

| 数据库 | `supports_ddl_transaction` | DDL 策略 | 容器 |
|--------|---------------------------|---------|------|
| Vastbase | True | execute_with_rollback | 复用 |
| PostgreSQL | True | execute_with_rollback | 复用 |
| SQL Server | True | execute_with_rollback | 复用 |
| 金仓 | False | execute 直接执行 | 销毁 |
| Oracle | False | execute 直接执行 | 销毁 |
| MySQL | False | execute 直接执行 | 销毁 |

### 容器生命周期

- 所有容器 `run_validation()` 的 `finally` 块中调用 `stop_container()`
- 数据干净的容器：stop 后可由预热池重新启动复用
- 数据脏的容器（DDL 无事务）：`stop_container()` 停止后，下次请求会创建新容器
- 预热池 TTL 5 分钟，超时自动清理闲置容器

---

## 配置说明

`config/databases.yaml` 由 Pydantic 模型在加载时校验，结构：

```yaml
databases:
  <db_type>:
    versions:
      "<version>":
        image: "<image>"
        port: <port>
        adapter: "<AdapterClass>"
        username: "<user>"
        password: "<password>"
        database: "<db>"
        privileged: true/false   # 可选
        env:                     # 可选（容器环境变量）
          KEY: "value"
```

`adapter` 字段值必须与 `ADAPTER_REGISTRY` 注册名一致。

当前配置：

| 数据库 | 版本 | 端口 | DDL 事务 |
|--------|------|------|---------|
| Vastbase | 3.0.8.29407, 3.0.9.31338 | 5432 | 是 |
| PostgreSQL | 12, 13, 14 | 5432 | 是 |
| SQL Server | 2017, 2019 | 1433 | 是 |
| 金仓 | V8 | 54321 | 否 |
| Oracle | 11c, 12c, 18c, 19c | 1521 | 否 |
| MySQL | 5.6, 5.7, 8.0 | 3306 | 否 |

---

## 已知限制

- 无 `pyproject.toml` / `setup.py`：纯 requirements.txt 驱动
- 无 CI/CD：没有 `.github/workflows` 或 `.gitlab-ci.yml`
- 无类型检查工具配置（mypy、pylint、black 等）
- 日志仅通过 `logging.basicConfig` 简单配置，无文件轮转
- 客户端数据仅存内存，重启丢失
- API Key 固定 3600 秒过期（token 响应中声明），但实际未强制过期
