# AGENTS.md — DB-MCP 数据库 SQL 验证服务

> 本文件面向 AI 编码助手。阅读者应对本项目一无所知。所有信息均基于项目实际代码。

---

## 项目概述

DB-MCP 是一个基于 FastAPI 的 SQL 验证服务，实现了 Model Context Protocol (MCP) JSON-RPC 协议，为智能答疑平台提供多数据库 SQL 执行与结果验证能力。

系统通过 Docker 容器化快速启动数据库实例，执行 SQL 后通过事务回滚（DML）或容器销毁（DDL）保证数据零残留。

### 核心功能

- 6 种数据库支持：Vastbase、金仓、PostgreSQL、Oracle、MySQL、SQL Server
- 多版本：同一数据库可配置多个版本
- 共享容器 + 连接池：多请求复用同一容器，通过 DBUtils 连接池 + Semaphore 限制并发
- 容器预热：启动时预热所有配置的 DB/版本，减少首次请求冷启动
- 健康监控：后台线程定期检查容器端口可达性，连续 3 次失败标记 UNHEALTHY 并触发重建
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
- 异步非阻塞路由：MCP tools/call 通过 `asyncio.to_thread` 卸载到线程池

### Gateway + Node 分布式架构

DB-MCP 支持两种角色部署以突破单机资源瓶颈：

```
                     ┌─────────────────────┐
                     │      Gateway         │  轻量路由层 :8000
                     │                      │  routing.yaml → RouteTable
                     │  按 (db_type,ver)    │  → ProxyClient httpx 转发
                     │  路由到对应 Node      │  不管理容器、不执行SQL
                     └──────────┬───────────┘
            ┌───────────────────┼───────────────────┐
            ▼                   ▼                   ▼
   ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
   │  Node "pg"     │  │ Node "oracle"  │  │  Node "mysql"  │
   │  PG/Vastbase/  │  │  Oracle 11c-   │  │  MySQL 5.6-    │
   │  Kingbase       │  │  21c           │  │  8.0 + MSSQL   │
   └────────────────┘  └────────────────┘  └────────────────┘
```

- **Node**：就是当前的单机部署，代码不动，只部署自己负责的数据库类型
- **Gateway**：新增无状态代理层，读取 `config/routing.yaml`，按 `(db_type, version)` 路由到对应 Node，支持透传、广播聚合、SSE 流式转发
- **路由是静态的**：不需要 K8s、共识算法 — 只是知道哪个版本在哪台机器上，把请求转过去
- **副本 + 自动 failover**：同一个 `(db_type, version)` 可配置在多个 Node，Gateway 按声明顺序尝试（primary → backup → ...），主节点不可达时自动切换到备份节点，首次成功即返回

**启动方式**：

```bash
# Node（默认角色，行为不变）
python -m src.main --role node --port 8000

# Gateway
python -m src.main --role gateway --port 8001

# 环境变量
MCP_ROLE=gateway python -m src.main
```

**Gateway 端点行为**：

| 端点 | Gateway 行为 |
|------|-------------|
| `POST /api/execute_sql` | 解析 body 中 `db_type`+`version` → RouteTable.lookup → 透传到目标 Node |
| `POST /api/dify/execute_sql` | 同上 |
| `GET /api/health` | scatter 广播到所有 Node → 聚合 `{"healthy": bool, "nodes": {...}}` |
| `GET /api/databases` | scatter 广播 → 合并去重返回 |
| `GET /api/databases/{db_type}/versions` | 查找拥有该 db_type 的 Node → 透传 |
| `POST /mcp` | `tools/list` → scatter 合并；`tools/call` → 解析 args 中 db_type+version → 透传；`initialize` → 返回 Gateway 自身信息 |
| `GET /sse` | 根据 query params 的 db_type+version 路由 → SSE 流透传 |
| `POST /messages` | 按 session 映射转发（fallback 到逐个 Node 尝试） |

**关键设计决策**：

- Node 端零改动：Gateway 是纯粹的透明代理层
- Gateway 无状态：可以横向扩展（多 Gateway 实例前加 LB）
- 错误隔离：一个 Node 不可达不影响其他 Node 的聚合响应
- 路由表是静态 YAML：简化部署，无需服务发现

---

## 技术栈

- **Python**: 3.8+
- **Web 框架**: FastAPI + uvicorn
- **Docker**: docker SDK
- **连接池**: DBUtils (PooledDB)
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
│   ├── databases.yaml              # 数据库版本配置（Pydantic 校验）
│   ├── routing.yaml                # Gateway 路由配置
│   └── dockerfile_templates/       # Dockerfile 模板目录
│       └── vastbase/
├── src/
│   ├── __init__.py                 # 公开接口导出
│   ├── main.py                     # 入口：uvicorn 启动，--role node|gateway
│   ├── api.py                      # Node FastAPI 应用创建 + lifespan
│   ├── config_manager.py           # 配置管理（databases.yaml + routing.yaml）
│   ├── container_pool.py           # 容器池（线程安全单例）
│   ├── executor.py                 # 执行引擎
│   ├── exceptions.py               # 异常体系
│   ├── dependencies.py             # 惰性单例工厂
│   ├── client_registry.py          # 客户端注册表
│   ├── nexus_client.py             # Nexus 仓库客户端
│   ├── package_manager.py          # 包管理器（Nexus 下载/解压/缓存）
│   ├── gateway/
│   │   ├── __init__.py             # Gateway 模块入口
│   │   ├── router.py               # RouteTable — (db_type,version) → Node 地址
│   │   ├── proxy.py                # ProxyClient — httpx 异步转发/聚合/流式
│   │   ├── routes.py               # Gateway FastAPI 路由（透传+聚合+MCP/SSE）
│   │   └── app.py                  # create_gateway_app() — Gateway 应用组装
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── vastbase.py
│   │   ├── kingbase.py
│   │   ├── postgresql.py
│   │   ├── mysql.py
│   │   ├── oracle.py
│   │   └── mssql.py
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── dify_mcp.py
│   └── routes/
│       ├── __init__.py
│       ├── mcp_routes.py
│       ├── oauth_routes.py
│       ├── client_routes.py
│       └── execute_routes.py
├── tests/
│   ├── test_adapters.py
│   ├── test_api.py
│   ├── test_config_manager.py
│   ├── test_executor.py
│   ├── test_container_pool.py
│   ├── test_gateway_router.py      # RouteTable 单测
│   └── test_gateway_proxy.py       # ProxyClient 单测
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
| `test_executor.py` | DML/DDL 路径、无效数据库、容器超时、适配器异常、SQL 拆分（dollar-quote、backtick、E-string）、ContainerPool 集成 |
| `test_container_pool.py` | ContainerPool 单例、线程安全、lease/release、信号量容量限制、并发创建防护、关闭拦截、ContainerEntry 默认值 |

### 测试策略

- 纯单元测试，不依赖真实数据库或 Docker
- Mock 外部依赖（ContainerPool、适配器）
- API 测试使用 FastAPI TestClient
- 每个 test class 在 `setup_method` 中重置状态
- `test_container_pool.py` 在 autouse fixture 中重置 ContainerPool 单例

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

### 3. ContainerPool 单例 + 租约模式

`ContainerPool`（[container_pool.py](src/container_pool.py)）是全局唯一的线程安全单例（双重检查锁定），管理所有数据库容器的生命周期、连接池和并发控制。

**数据模型**：

```
ContainerPool (单例)
├── _entries: Dict[str, ContainerEntry]     # key = "db_type-version[-compat_mode]"
├── _create_locks: Dict[str, Lock]          # 每个 key 一把细粒度锁，防并发创建
├── _shutting_down: Event                   # 关闭信号
└── _health_thread: daemon Thread           # 后台健康监控

ContainerEntry (每个容器一个)
├── state: ContainerState (STARTING/HEALTHY/UNHEALTHY/DESTROYING/STOPPED)
├── semaphore: BoundedSemaphore(max_concurrency)  # 并发上限
├── connection_pool: PooledDB                   # DBUtils 连接池
├── active_leases: int                          # 当前活跃租约数
├── condition: threading.Condition              # 状态变更通知
├── health_failures: int                        # 连续健康检查失败次数
└── exclusive: bool                             # DDL 独占标志
```

**核心流程**：

```
lease(db_type, version, config, compat_mode)
  ├─ 检查 _shutting_down，已关闭则抛 ContainerPoolCapacityError
  ├─ 获取 per-key create_lock → _ensure_healthy()
  │   ├─ Entry 存在且 HEALTHY → 直接返回
  │   ├─ Entry 为 DESTROYING → 抛异常让调用方重试
  │   ├─ Entry 为 UNHEALTHY → 销毁 → 重建
  │   └─ Entry 不存在 → 创建 ContainerEntry → _start_entry()
  │       ├─ docker pull if not exists
  │       ├─ _get_or_create_container() (幂等：重名容器复用)
  │       ├─ _wait_for_port() (最多 120s)
  │       └─ 创建 PooledDB 连接池
  ├─ semaphore.acquire(timeout=lease_timeout)  # 排队等待
  │   └─ 超时 → ContainerPoolCapacityError (HTTP 503)
  ├─ connection_pool.connection() → 借出连接
  ├─ active_leases += 1
  └─ 返回 ContainerLease(context manager)

lease.__exit__() → release(entry, conn)
  ├─ conn.close() → 归还到 DBUtils 连接池
  ├─ active_leases -= 1
  └─ semaphore.release()
```

**DDL 独占访问**（MySQL/Oracle/金仓 等 non-transactional DDL 数据库）：

`exclusive_lease()` 等待所有活跃租约释放后独占容器，阻止并发读写与 DDL 冲突。

**健康监控**：

后台 daemon 线程每 30s 检查端口可达性（`_is_port_open`），连续 3 次失败标记 UNHEALTHY → 下次 `lease()` 自动重建。每日午夜清理今日未使用的容器（last_used 不是今天 & active_leases=0）。

**优雅关闭**：

`shutdown()` 设置 `_shutting_down` Event → 拒绝新租约 → 等待 active_leases 归零（最多 30s）→ 关闭所有连接池 → stop 所有容器。

### 4. 连接池

**DBUtils PooledDB** 配置（[container_pool.py:98-197](src/container_pool.py#L98-L197)）：

- `maxconnections = max_concurrency`（默认 10）
- `mincached = 2`，`maxcached = 5`
- `maxusage = 100`（单连接执行 100 次后回收）
- `blocking = True`（池满时等待）

每个容器一个独立连接池，驱动对应关系：
- PostgreSQL / Vastbase / 金仓 → `psycopg2` + `PooledDB`
- MySQL → `pymysql` + `PooledDB`
- Oracle → `oracledb` + `PooledDB`（DSN 方式）
- SQL Server → `pymssql` + `PooledDB`

### 5. 适配器池化模式

`DBAdapter` 新增两个方法（[base.py](src/adapters/base.py)）：

- `use_connection(conn)` — 标记 `_is_pooled = True`，直接使用池连接（不创建新连接），创建 cursor，应用 `statement_timeout`
- `_safe_disconnect()` — 池化模式下只关闭 cursor 和清空引用（不关闭底层 socket）；非池化模式行为不变（真正 disconnect）

所有适配器的 `disconnect()` 改为调用 `self._safe_disconnect()`，对池化/非池化上下文透明。

### 6. 类级单例 + 线程安全

- `ConfigManager`：类变量 `_config` + `threading.Lock()`
- `ContainerPool`：模块级 `_instance` + `_instance_lock`（双重检查锁定）
- `ClientRegistry`：实例变量 + `threading.Lock()` 保护所有字典操作
- `dependencies.py`：惰性单例工厂函数，避免 Docker SDK 在模块导入时报错

### 7. 异常体系

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
├── ContainerPoolCapacityError (503)     # 新：并发超限 / 关闭中
└── ValidationError (422)
```

每个异常类携带 `http_status`，可在全局异常处理器中直接映射到 HTTP 响应。

### 8. SQL 拆分 + PL/pgSQL 块识别

`executor._split_sql_statements()` 按字符遍历 SQL 文本，正确处理以下上下文中的分号不应作为语句分隔符：

- 单行注释 `--`
- 块注释 `/* */`
- 单引号字符串 `'...'`（含 `''` 转义）
- 双引号标识符 `"..."`
- Dollar-quote `$$...$$` / `$tag$...$tag$`
- Backtick 标识符 `` `...` ``
- E 转义字符串 `E'...'`

PL/pgSQL 匿名块和存储过程通过 BEGIN/END 嵌套深度计数识别：`_is_plsql_block()` 检测 `DECLARE...BEGIN...END;` 和 `BEGIN...END;` 模式，排除裸 `BEGIN` 和 `BEGIN TRANSACTION`。遇到 `END;` 时深度减一，深度归零表示块结束，后续分号才作为语句分隔。支持嵌套 `CREATE PROCEDURE ... END;` / `CALL ...();` 的拆分（[test_executor.py:154-169](tests/test_executor.py#L154-L169)）。

### 9. 启动预热

`ContainerPool.prewarm()` 在 lifespan startup 中调用，遍历配置中所有标记 `prewarm: true` 的 DB/版本组合，启动容器 + 建连接池。

### 10. 异步非阻塞路由

MCP `tools/call` JSON-RPC 端点和 Dify 工具调用通过 `asyncio.to_thread()` 将 SQL 执行卸载到线程池，避免阻塞 FastAPI 事件循环。

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

**关键结论：数据能回滚 = 容器可复用。** 只有 DDL 在不支持事务的数据库（金仓、Oracle、MySQL）上执行时，数据才无法通过事务回退。此时先尝试生成反向 DDL（如 `DROP TABLE`）清理数据；反向 DDL 失败时，fallback 到销毁容器（`mark_for_destroy()`）。支持 DDL 事务的数据库（Vastbase、PostgreSQL、SQL Server）即使是 DDL 也通过 `execute_with_rollback` 回滚，容器保持干净。

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

```
请求 → lease() → [semaphore] → borrow 连接 → use_connection() → execute → release()
                                                                              ├─ conn.close() → 归还池
                                                                              ├─ semaphore.release()
                                                                              └─ mark_for_destroy? → destroy_container()
```

- **DML 执行**：事务包裹 + ROLLBACK，数据零残留 → 容器保留，连接归还池
- **DDL（事务型 DB）**：同 DML，通过 `execute_with_rollback` 回滚 → 容器保留
- **DDL（非事务型 DB）**：直接执行 → `lease.mark_for_destroy()` → release 后异步销毁容器 → 下次 lease 自动重建
- **DDL 反向清理失败**：reverse DDL 执行失败时 fallback 到 `mark_for_destroy()`，响应中带 "container will be destroyed" 提示
- **空闲 TTL**：`MCP_CONTAINER_IDLE_TTL`（默认 86400s），健康监控线程在每日午夜清理今日未使用且无活跃租约的容器
- **健康检查**：每 30s 探测端口，连续 3 次失败触发重建

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

### Gateway 路由配置 (`config/routing.yaml`)

Gateway 角色读取此文件（不加载 `databases.yaml`），静态映射 `(db_type, version)` 到 Node 地址：

```yaml
gateway:
  host: "0.0.0.0"
  port: 8000
  request_timeout: 3600
  retry_on_node_error: true

nodes:
  node-pg:
    address: "192.168.1.10:8000"
    databases:
      - db_type: postgresql
        versions: [12, 13, 14]
      - db_type: vastbase
        versions: ["3.0.8", "3.0.9"]
      - db_type: kingbase
        versions: [V8, V9]

  node-others:
    address: "192.168.1.11:8000"
    databases:
      - db_type: oracle
        versions: [11c, 12c, 18c, 21c]
      - db_type: mysql
        versions: ["5.6", "5.7", "8.0"]
      - db_type: sqlserver
        versions: ["2017", "2019"]
```

每个 Node 的 `databases.yaml` 只保留自己负责的数据库类型（裁剪部署）。Gateway 启动时 Load → RouteTable 构建扁平 `(db_type, version) → [Route, ...]` 映射，匹配区分大小写（自动剥离 v/V 前缀）。同一 `(db_type, version)` 允许多个 Node 声明，形成副本列表 — 第一个为 primary，后续为 failover 目标。

### 数据库版本配置 (`config/databases.yaml`)

当前配置：

| 数据库 | 版本 | 端口 | DDL 事务 |
|--------|------|------|---------|
| Vastbase | 2.2.15, 3.0.8, 3.0.9 | 5432 | 是 |
| PostgreSQL | 12, 13, 14 | 5432 | 是 |
| SQL Server | 2017, 2019 | 1433 | 是 |
| 金仓 | V8, V9 | 54321 | 否（PG 模式下为是） |
| Oracle | 11c, 12c, 18c, 19c, 21c | 1521 | 否 |
| MySQL | 5.6, 5.7, 8.0 | 3306 | 否 |

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MCP_PREWARM` | — | （未实现）无独立跳过预热的环境变量，预热始终执行 |
| `MCP_MAX_CONCURRENCY` | `10` | 单容器最大并发执行数（信号量上限） |
| `MCP_LEASE_TIMEOUT` | `30` | 信号量等待超时秒数，超时返回 503 |
| `MCP_CONTAINER_IDLE_TTL` | `86400` | 空闲容器保留时间（秒），默认 24 小时，超时后次日午夜清理 |
| `MCP_HEALTH_CHECK_INTERVAL` | `30` | 健康检查间隔（秒） |
| `MCP_RESOURCE_CPU_<TYPE>` | 见下表 | 覆盖数据库容器的 CPU 限制（核数） |
| `MCP_RESOURCE_MEM_<TYPE>` | 见下表 | 覆盖数据库容器的内存限制（如 `512m`、`2g`） |
| `MCP_ROLE` | `node` | 进程角色：`node`（数据库执行节点）或 `gateway`（路由代理） |
| `MCP_ROUTING_CONFIG` | `config/routing.yaml` | Gateway 路由配置文件路径 |
| `MCP_GATEWAY_HOST` | `0.0.0.0` | Gateway 监听地址 |
| `MCP_GATEWAY_PORT` | `8000` | Gateway 监听端口 |
| `MCP_GATEWAY_TIMEOUT` | `3600` | Gateway 转发请求超时（秒） |

### 容器资源限制（官方最低要求）

启动容器时通过 `mem_limit` + `nano_cpus` 施加资源上限，防止单个容器耗尽宿主机资源。按数据库类型的默认值如下，可在 `databases.yaml` 的 `resources` 字段或环境变量中覆盖：

| 数据库 | CPU | 内存 | 依据 |
|--------|-----|------|------|
| PostgreSQL | 1 | 512m | 官方镜像最低 |
| Vastbase | 1 | 1g | 基于 PostgreSQL，保守 1GB |
| Kingbase | 2 | 2g | 官方最低 4核/2GB（容器轻载下调） |
| MySQL | 1 | 512m | 官方最低 512MB |
| Oracle | 1 | 1g | XE 最低 1GB |
| SQL Server | 2 | 2g | Docker 硬性最低 2GB |

覆盖优先级：`MCP_RESOURCE_*` 环境变量 > `databases.yaml` `resources` 字段 > 默认值。

`databases.yaml` 示例：
```yaml
postgresql:
  versions:
    "14":
      image: "postgres:14"
      port: 5432
      ...
      resources:
        cpu: 2
        memory: "1g"
```

---

## 已知限制

- 无 `pyproject.toml` / `setup.py`：纯 requirements.txt 驱动
- 无 CI/CD：没有 `.github/workflows` 或 `.gitlab-ci.yml`
- 无类型检查工具配置（mypy、pylint、black 等）
- 客户端数据仅存内存，重启丢失
- API Key 固定 3600 秒过期（token 响应中声明），但实际未强制过期
- `list_db_versions` 仅返回 databases.yaml 中静态配置的版本，不包含 ephemeral（Nexus 自动构建）版本
- Vastbase 3.0.9 配置使用了 3.0.8 镜像（可用的相同基础镜像）
- 健康监控仅对端口可达性做检查，不验证数据库服务是否正常响应 SQL
- 无速率限制（QPS throttling）
