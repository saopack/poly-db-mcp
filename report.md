# DB-MCP 功能报告

## 一、用户需求

DB-MCP 定位为智能答疑平台的数据库 SQL 验证中间层：

| 场景 | 说明 |
|------|------|
| SQL 教学与答疑 | 用户在智能答疑平台提交 SQL，系统在真实数据库中执行并返回结果 |
| 多数据库兼容性验证 | 同一 SQL 在 Vastbase/金仓/PostgreSQL/Oracle/MySQL/SQL Server 上的差异验证 |
| 多版本迁移验证 | 同数据库不同版本间（如 MySQL 5.6 → 8.0）的 SQL 兼容性测试 |
| AI Agent 工具调用 | 作为 MCP Server 被 Dify 等 AI 平台发现和调用，为 LLM 提供真实数据库执行能力 |
| 安全隔离执行 | Docker 容器隔离 + 事务回滚/容器销毁，数据零残留 |

---

## 二、现有功能清单与工作量分配

> 现有功能总工作量：10 人天

### 2.1 数据库支持

| 数据库 | 版本 | 适配器 | DDL 事务 | 状态 |
|--------|------|--------|----------|------|
| Vastbase | 2.2.15, 3.0.8, 3.0.9 | VastbaseAdapter | 支持 | 已实现 |
| 金仓 Kingbase | V8 | KingbaseAdapter | 不支持 | 已实现 |
| PostgreSQL | 12, 13, 14 | PostgreSQLAdapter | 支持 | 已实现 |
| Oracle | 11c, 12c, 18c, 19c, 21c | OracleAdapter | 不支持 | 已实现 |
| MySQL | 5.6, 5.7, 8.0 | MySQLAdapter | 不支持 | 已实现 |
| SQL Server | 2017, 2019 | SqlServerAdapter | 支持 | 已实现 |

### 2.2 SQL 执行引擎（executor.py）— 3d

| 功能 | 说明 |
|------|------|
| 多语句拆分 | 分号分隔，正确处理字符串引号、转义、Dollar-quote、块注释、行注释、PL/SQL 块 |
| DDL 检测 | 正则识别 CREATE/ALTER/DROP/TRUNCATE/RENAME |
| 反向 DDL | CREATE TABLE → DROP TABLE，ALTER ADD COLUMN → DROP COLUMN 等 7 种模式，用于非事务 DDL 的清理 |
| 事务回滚 | DML 和事务型 DDL 在显式事务中执行后 ROLLBACK，数据零残留 |
| DDL 兜底 | 非事务型 DDL 先尝试反向 DDL 清理，失败则销毁容器重建 |
| EXPLAIN 模式 | 自动在查询前加 EXPLAIN，仅返回执行计划，不实际执行 |
| 多语句执行编排 | 事务型 DB 中所有语句在同一事务中执行（DDL 后 DML 可见）；非事务型 DB 中 DDL 立即提交、DML 在独立事务中回滚 |
| 连接重试 | 最多 24 次（120s），应对容器启动后的数据库就绪延迟 |
| Unicode 空白字符清理 | 自动替换全角空格等 Unicode 空白字符，防止从网页/中文输入法粘贴的 SQL 报错 |
| PL/SQL 块保护 | 不拆分 DECLARE/BEGIN...END、CREATE FUNCTION/PROCEDURE/PACKAGE 等块内的分号 |

### 2.3 Docker 容器管理（docker_manager.py）— 2d

| 功能 | 说明 |
|------|------|
| 镜像管理 | 自动 pull 缺失镜像 |
| 幂等启动 | 已运行的容器直接复用 |
| 端口自动映射 | 获取 Docker 分配的 HostPort（10s 重试） |
| 预热池 | 空闲容器 5 分钟内复用，过期自动清理 |
| 容器销毁 | DDL 污染容器 stop 后不预热，后续请求重建 |
| 关机清理 | `stop_all_warm_containers()` 在服务退出时执行 |
| 端口检测 | TCP socket 探测等待容器就绪（最长 120s） |
| 特权容器 | 支持 `privileged: true` 配置（Vastbase 需要） |
| 自定义命令 | 支持 `command` 配置参数（如 MySQL 8.0 认证插件参数） |
| 宿主机环境变量 | 从 YAML 配置注入到容器的环境变量 |

### 2.4 认证与安全（client_registry.py + oauth_routes.py）— 1d

| 功能 | 说明 |
|------|------|
| API Key 认证 | Bearer Token 方式，格式 `mcp-{64位随机字符}` |
| OAuth 2.0 | Authorization Code 流程 + DCR 动态客户端注册（RFC 7591） |
| 客户端管理 | 注册/注销/列表/更新/API Key 轮换 |
| 线程安全 | ClientRegistry 全部操作使用 `threading.Lock` 保护 |
| 授权码过期 | OAuth code 10 分钟过期，自动清理 |
| 防重放 | 授权码用后标记 `used=True`，不可复用 |

### 2.5 MCP 协议支持（mcp_routes.py + dify_mcp.py）— 1d

| 功能 | 说明 |
|------|------|
| JSON-RPC | initialize / tools/list / tools/call / notifications/initialized |
| SSE 端点 | `/sse` 推送 endpoint 信息 + 每 30s 心跳 |
| 工具定义 | execute_sql / list_databases / list_db_versions 三个工具，含参数 schema |
| Dify 集成 | Dify 专用接口 `/api/dify/execute_sql` 和 OAuth 回调 `/console/api/mcp/oauth/callback` |
| OAuth 服务发现 | `/.well-known/oauth-authorization-server` 元数据端点 |

### 2.6 REST API（execute_routes.py + client_routes.py + api.py）— 0.5d

| 端点 | 方法 | 认证 | 说明 |
|------|------|------|------|
| `/api/databases` | GET | 无 | 支持的数据库类型列表 |
| `/api/databases/{type}/versions` | GET | 无 | 指定数据库的版本列表 |
| `/api/execute_sql` | POST | 需要 | SQL 执行核心接口 |
| `/api/dify/execute_sql` | POST | 需要 | Dify 专用执行接口 |
| `/api/health` | GET | 无 | 健康检查（config + docker） |
| `/api/shutdown` | POST | 无 | 停止服务 |
| `/api/clients/*` | CRUD | 无 | 客户端注册/列表/注销/更新/轮换 |
| `/register` `/authorize` `/token` | POST/GET/POST | 无 | OAuth 2.0 认证流程 |
| `/` `/mcp` `/sse` `/messages` | GET/POST | 无 | MCP JSON-RPC + SSE |
| Swagger / ReDoc | `/docs` `/redoc` | 无 | FastAPI 自动生成 |

### 2.7 配置管理（config_manager.py）— 0.25d

| 功能 | 说明 |
|------|------|
| YAML 加载 | 读取 `config/databases.yaml` |
| Pydantic 校验 | VersionConfig / DBTypeConfig / DatabaseConfig 三层模型校验 |
| 线程安全 | 配置变更通过 `threading.Lock` 保护 |
| 查询接口 | get_db_config / get_supported_databases / get_db_versions / is_config_valid |

### 2.8 审计与运维（main.py + exceptions.py + dependencies.py）— 0.25d

| 功能 | 说明 |
|------|------|
| 结构化审计 | 记录 client_id、client_name、db_type、version、query_preview、result_status |
| 日志滚动 | RotatingFileHandler，10MB/文件，保留 5 个备份 |
| 进程管理 | `--daemon` 后台启动、`--stop` 停止、`--restart` 重启，跨平台（Win32 + Unix） |
| PID 文件 | 记录后台进程 PID，进程存活检测 |
| 异常体系 | MCPError 基类 + 8 种子类，映射到 HTTP 状态码（400/404/422/500/502/504） |
| 依赖注入 | 惰性单例 get_client_registry / get_mcp_handler |

---

## 三、工作量汇总

> 现有功能总工作量：10 人天

| 模块 | 核心文件 | 工作量 | 说明 |
|------|----------|--------|------|
| SQL 执行引擎 | executor.py | 3d | 最复杂的模块。多语句拆分（分号、引号、Dollar-quote、注释、PL/SQL 块保护）；DDL 正则检测 + 7 种反向 DDL 生成；DML 事务回滚 / DDL 兜底销毁两条执行路径；事务型与非事务型 DB 的多语句编排策略；连接重试（24次/120s）；Unicode 空白字符标准化；Oracle 终止符处理 |
| Docker 容器管理 | docker_manager.py | 2d | 镜像自动拉取、容器幂等启动、端口自动映射（10s 重试获取 HostPort）、TCP 端口探测等待就绪；预热池机制（5min TTL，过期清理，关机全停）；环境变量/特权模式/自定义 command 注入；DDL 污染容器的销毁重建路径 |
| 数据库适配器 | adapters/ (base + 6 个) | 2d | 抽象基类定义 connect/execute/begin/rollback/commit/disconnect + execute_with_rollback 模板方法 + 装饰器自动注册。PostgreSQL(psycopg2,DDL 事务)、Vastbase(继承 PG,兼容性模式 A/B/PG/MSSQL 切换)、金仓(psycopg2,无 DDL 事务)、MySQL(pymysql,autocommit+utf8mb4)、Oracle(oracledb thin,SID/SERVICE_NAME,PL/SQL,耗时最长)、SQL Server(pymssql) |
| 认证与安全 | client_registry.py + oauth_routes.py | 1d | API Key 随机生成(mcp-xxx)+Bearer Token 提取+双向映射；客户端 CRUD+Key 轮换；OAuth 2.0 Authorization Code 流程 + DCR 动态注册(RFC 7591)；授权码 10min 过期+一次性使用防重放；全部操作 threading.Lock 线程安全 |
| MCP 协议 | mcp_routes.py + dify_mcp.py | 1d | JSON-RPC 入口（initialize/tools/list/tools/call/notifications），参数 schema 自动生成；SSE 端点异步推送 endpoint+30s 心跳；Dify 平台专用 execute_sql 接口+OAuth 回调+OAuth 服务发现元数据 |
| REST API | execute_routes.py + client_routes.py + api.py | 0.5d | SQL 执行端点（async 线程池执行+审计日志）、数据库信息端点（类型列表/版本列表）、客户端管理端点（5个CRUD）、健康检查（config+docker 双状态）+ 关机端点；FastAPI lifespan 容器预热清理 + Swagger/ReDoc 自动文档 |
| 配置管理 | config_manager.py | 0.25d | YAML 加载 + Pydantic 三层模型校验；查询接口（get_db_config/get_supported_databases/get_db_versions/is_config_valid）；线程安全的配置读写 |
| 审计与运维 | main.py + exceptions.py + dependencies.py | 0.25d | 进程管理（--daemon/--stop/--restart，跨平台 Win32+Unix PID 管理）；双通道日志（控制台+RotatingFileHandler 滚动）；8 个异常子类精确映射 HTTP 状态码；惰性单例依赖注入 |

---

## 四、后续待开发功能

| # | 功能 | 工作量 | 说明 |
|---|------|--------|------|
| 1 | 客户端持久化 (SQLite) | 2d | ClientRegistry 目前全部内存存储，重启丢失。新增 SQLite 存储层 |
| 2 | API Key 哈希存储 | 1d | 注册时 bcrypt 哈希入库，只返回一次明文 |
| 3 | 速率限制 | 2d | 基于 client_id 的内存令牌桶，可配置 QPS |
| 4 | 健康检查增强 | 1d | 增加各 db_type 已运行容器的连通性探测 |
| 5 | 统一查询超时 | 2d | executor 层 asyncio.timeout 兜底 + 各适配器实现超时设置 |

---

## 五、系统设计：数据库并发访问

### 5.1 工作流程图

```
                         客户端请求
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│                    FastAPI 路由层 (async)                     │
│                                                              │
│  /api/execute_sql  ──→  asyncio.wait_for(timeout=300s)       │
│                                │                             │
│  /mcp (JSON-RPC)    ──→  asyncio.wait_for(timeout=300s)      │
│                                │                             │
│                    asyncio.to_thread(executor.execute)        │
└────────────────────────────────┼─────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│                     Executor (同步线程)                       │
│                                                              │
│  execute()                                                   │
│    ├─ _call_with_timeout(adapter.execute, sql)  ◄── 30s 超时 │
│    ├─ 多语句拆分 (_split_sql)                                 │
│    ├─ DDL 检测 / 反向 DDL 生成                               │
│    └─ lease = pool.lease(db_type, version, config)            │
└────────────────────────────────┼─────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│                    ContainerPool (单例)                       │
│                                                              │
│  lease(db_type, version, config)                             │
│    │                                                         │
│    ├─► _ensure_healthy()                                     │
│    │     ├─ 容器已存在且 HEALTHY? ──→ 直接返回                │
│    │     ├─ 容器不存在? ──→ _start_entry()                   │
│    │     │     ├─ pull image (if not exists)                 │
│    │     │     ├─ docker run (CPU/Mem 限制)                  │
│    │     │     ├─ _wait_for_db_ready (120s)                  │
│    │     │     └─ 创建 PooledDB 连接池                       │
│    │     └─ 容器 UNHEALTHY? ──→ 销毁 → 重建                  │
│    │                                                         │
│    ├─► semaphore.acquire(timeout=30s)                        │
│    │     ├─ 拿到槽位? ──→ 继续                                │
│    │     └─ 超时? ──→ 返回 "Too many concurrent requests"    │
│    │                                                         │
│    ├─► connection_pool.connection()  // 借一个 DB 连接        │
│    │                                                         │
│    └─► active_leases += 1                                    │
│                                                              │
│  返回 ContainerLease(self, entry, conn)                      │
└────────────────────────────────┼─────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│                    执行 SQL & 归还                            │
│                                                              │
│  with lease:                    // ContainerLease __enter__   │
│    adapter.use_connection(conn)                               │
│    result = adapter.execute(sql)                              │
│                                                              │
│  lease.__exit__()               // 无论成功失败都执行          │
│    ├─ pool.release(entry, conn)                              │
│    │     ├─ conn.close()         // 归还到 PooledDB 池        │
│    │     ├─ active_leases -= 1                                │
│    │     └─ semaphore.release()  // 释放槽位                  │
│    │                                                         │
│    └─ if mark_for_destroy:                                   │
│         pool.destroy_container()  // DDL 污染 → 销毁容器      │
└──────────────────────────────────────────────────────────────┘


### 5.2 容器状态机

                    docker run
    STOPPED ────────────────────► STARTING
       ▲                              │
       │                          _wait_for_db_ready() 成功
       │                              │
       │                              ▼
       │                          HEALTHY ───────────────┐
       │                              │                   │
       │                              │              连续3次端口不通
       │                              │                   │
       │    idle TTL到期               │                   ▼
       │    + active_leases=0          │              UNHEALTHY
       │                              │                   │
       │                              ▼                   │
       │                          DESTROYING ◄───────────┘
       │                              │       下次请求触发重建
       │                          docker stop
       │                          docker rm
       │                              │
       └──────────────────────────────┘


### 5.3 并发控制三层架构

   ┌──────────────┐    ┌──────────────────┐    ┌─────────────────┐
   │  路由层       │    │  容器槽位层       │    │  连接池层        │
   │              │    │                  │    │                 │
   │ async 接收   │───►│ BoundedSemaphore │───►│ PooledDB        │
   │ to_thread    │    │ max=10           │    │ maxconnections  │
   │ 300s 兜底    │    │ acquire 阻塞30s  │    │ = 10            │
   │              │    │ 超时→拒绝        │    │ 线程安全        │
   └──────────────┘    └──────────────────┘    └─────────────────┘
        无限并发              最多10个              池内10个连接
        不阻塞                请求进入              复用不重建

---

## 六、系统设计：容器资源限制

### 6.1 工作流程图

```
                    服务启动 / 首次请求
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│              资源限制三级合并 (_resolve_resource_limits)       │
│                                                              │
│  Level 1: 内置默认值                                          │
│  ┌─────────────────────────────────────────────────────┐     │
│  │ postgresql: 1核/512MB    mysql:      1核/512MB       │     │
│  │ vastbase:   1核/1GB      oracle:    1核/1GB          │     │
│  │ kingbase:   2核/2GB      sqlserver: 2核/2GB          │     │
│  └─────────────────────────────────────────────────────┘     │
│                           │                                  │
│                           ▼                                  │
│  Level 2: databases.yaml 覆盖                                 │
│  ┌─────────────────────────────────────────────────────┐     │
│  │ vastbase:                                             │     │
│  │   versions:                                           │     │
│  │     "2.2.15":                                         │     │
│  │       resources:                                      │     │
│  │         cpu: 2       ◄── 覆盖默认值 1核 → 2核          │     │
│  │         memory: "2g"  ◄── 覆盖默认值 1GB → 2GB        │     │
│  └─────────────────────────────────────────────────────┘     │
│                           │                                  │
│                           ▼                                  │
│  Level 3: 环境变量覆盖 (优先级最高)                            │
│  ┌─────────────────────────────────────────────────────┐     │
│  │ export MCP_RESOURCE_CPU_VASTBASE=4                    │     │
│  │ export MCP_RESOURCE_MEM_VASTBASE=4g                   │     │
│  │                   ◄── 最终生效: 4核/4GB                │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                              │
│  合并结果: {"cpu": 4, "memory": "4g"}                        │
└────────────────────────────────┼─────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│              容器创建 (_create_and_start)                     │
│                                                              │
│  docker_client.containers.run(                               │
│      image=config['image'],                                  │
│      name=container_name,                                    │
│      ports={f"{port}/tcp": None},   // 端口随机映射           │
│      detach=True,                                            │
│      remove=False,                                           │
│      privileged=config.get('privileged', False),             │
│                                                              │
│      nano_cpus = 4 * 1e9,          ◄── CPU 限制 (纳秒)       │
│      mem_limit = "4g",             ◄── 内存限制              │
│  )                                                           │
└────────────────────────────────┼─────────────────────────────┘
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│                  Docker daemon (cgroup)                       │
│                                                              │
│  ┌──────────────────────────────────────┐                    │
│  │          容器 cgroup 命名空间          │                    │
│  │                                      │                    │
│  │  cpu.cfs_quota_us = 400000           │ ← nano_cpus 转换   │
│  │  cpu.cfs_period_us = 100000          │                    │
│  │  → 4 核硬上限 (超出则限流)            │                    │
│  │                                      │                    │
│  │  memory.limit_in_bytes = 4GB         │ ← mem_limit        │
│  │  → 超过则 OOM Kill                   │                    │
│  └──────────────────────────────────────┘                    │
│                                                              │
│  数据库进程认为自己有多少算力，但实际上被 cgroup 硬限制住       │
└──────────────────────────────────────────────────────────────┘


### 6.2 覆盖规则示意

  ┌──────────┐     ┌──────────┐     ┌──────────┐
  │  默认值   │ ──► │  YAML    │ ──► │  环境变量 │ ──► 最终生效
  │ (内置)    │     │ (版本级) │     │ (全局级) │
  └──────────┘     └──────────┘     └──────────┘
   优先级最低         优先级中          优先级最高
```

## 七、系统设计：Vastbase 多版本可定制构建

### 7.1 工作流程图

```
                        客户端请求
                            │
                    api/execute_sql
                    { db_type: "vastbase",
                      version: "3.0.8.299958",
                      tags: ["standard"],      // 或 tags: ["custom"]
                      db_params: {"work_mem": "4MB"},
                      mounts: ["/path/to/postgresql.conf:/etc/conf/postgresql.conf"]
                    }
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│                    请求分流 (executor)                        │
│                                                              │
│  config.get('tags') 含 "standard"?                           │
│         │                                                   │
│    YES  │              NO  (含 "custom" 或 db_params/mounts) │
│         ▼                                                   │
│  ┌──────────────────┐     ┌─────────────────────────────┐    │
│  │  标准池化路径     │     │  定制一次性路径              │    │
│  │                  │     │                             │    │
│  │ pool.lease()     │     │ pool.lease_ephemeral()      │    │
│  │   ├─ 复用容器    │     │   ├─ docker build (if need) │    │
│  │   ├─ 信号量限流  │     │   ├─ docker run + params    │    │
│  │   ├─ 连接池借还  │     │   ├─ 单连接直接执行         │    │
│  │   └─ 容器复用    │     │   └─ docker stop + rm       │    │
│  └──────────────────┘     └─────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘


### 7.2 镜像构建流程 (首次请求)

  databases.yaml 配置:
  ┌────────────────────────────────────────┐
  │ vastbase:                              │
  │   versions:                            │
  │     "3.0.8.299958":                    │
  │       port: 5432                       │
  │       adapter: "VastbaseAdapter"       │
  │       built: false    ◄── 尚未构建      │
  │       username: "vbmcp"               │
  │       ...                             │
  └────────────────────────────────────────┘
               │
               ▼
  ┌──────────────────────────────────────────────────────────────┐
  │              _ensure_image(version, config)                   │
  │                                                              │
  │  1. 检查本地镜像 vastbase:3.0.8.299958 是否存在?              │
  │         │                                                   │
  │    存在 │                 不存在                              │
  │         ▼                                                   │
  │    直接返回              ┌─────────────────────┐             │
  │                          │  docker build        │             │
  │                          │  ┌─────────────────┐ │             │
  │                          │  │ Dockerfile 模板  │ │             │
  │                          │  │                 │ │             │
  │                          │  │ ARG VERSION     │ │             │
  │                          │  │ ARG PORT        │ │             │
  │                          │  │                 │ │             │
  │                          │  │ FROM base       │ │             │
  │                          │  │ RUN install ... │ │             │
  │                          │  └─────────────────┘ │             │
  │                          │                       │             │
  │                          │  build上下文:         │             │
  │                          │  /images/vastbase/   │             │
  │                          │    3.0.8.299958/     │             │
  │                          │    ├─ Dockerfile     │             │
  │                          │    └─ deps/          │             │
  │                          │                       │             │
  │                          │  产物:                │             │
  │                          │  vastbase:3.0.8.299958              │
  │                          └─────────────────────┘             │
  └──────────────────────────────────────────────────────────────┘


### 7.3 定制容器执行流程 (lease_ephemeral)

  lease_ephemeral(db_type, version, config)
       │
       ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 1. _ensure_image(version, config)     // 构建或复用镜像       │
  └──────────────────────────────┬───────────────────────────────┘
                                 │
                                 ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 2. _create_ephemeral_container()                             │
  │                                                              │
  │    container_name = f"db-mcp-ephemeral-{key}-{random_suffix}" │
  │                                                              │
  │    run_kwargs = {                                            │
  │        image: "vastbase:3.0.8.299958",                       │
  │        name: container_name,                                 │
  │        auto_remove: True,    // 兜底: 即使崩溃也自动清理     │
  │        ...                                                   │
  │    }                                                         │
  │                                                              │
  │    // db_params 注入 (转为环境变量)                           │
  │    if config.get('db_params'):                               │
  │        env['OTHER_PG_CONF'] = encode_db_params(db_params)    │
  │        例: {"work_mem":"2MB","wal_buffers":"16MB"}           │
  │             → "work_mem=2MB\\nwal_buffers=16MB"              │
  │                                                              │
  │    // mounts 注入 (转为 docker volumes)                      │
  │    if config.get('mounts'):                                  │
  │        run_kwargs['volumes'] = config['mounts']              │
  │        例: ["/path/to/postgresql.conf:/etc/vb/postgresql.conf"]│
  │                                                              │
  │    // tags: label 注入                                       │
  │    run_kwargs['labels'] = {'db-mcp-tags': ','.join(tags)}    │
  │                                                              │
  │    container = docker.containers.run(**run_kwargs)           │
  └──────────────────────────────┬───────────────────────────────┘
                                 │
                                 ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 3. _wait_for_db_ready(host, port)  // 数据库连接验证          │
  └──────────────────────────────┬───────────────────────────────┘
                                 │
                                 ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 4. 直接连接执行 (不走连接池)                                  │
  │                                                              │
  │    conn = psycopg2.connect(host, port, user, password, db)    │
  │    adapter.use_connection(conn)                               │
  │    result = adapter.execute(sql)                              │
  │    adapter.disconnect()                                       │
  └──────────────────────────────┬───────────────────────────────┘
                                 │
                                 ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 5. 销毁容器 (ContainerLease.__exit__)                        │
  │                                                              │
  │    docker.stop(container_name)                               │
  │    docker.rm(container_name)                                 │
  │                                                              │
  │    (auto_remove=True 兜底: 即使上述失败, daemon 也会清理)     │
  └──────────────────────────────────────────────────────────────┘


### 7.4 标准 vs 定制路径对比

  ┌─────────────────────────────────────────────────────────────────┐
  │                        标准路径                │    定制路径     │
  ├─────────────────────────────────────────────────────────────────┤
  │  容器命名    db-mcp-vastbase-3.0.8.299958      │  加随机后缀     │
  │  容器复用    复用, 请求间共享                   │  用完即毁       │
  │  并发控制    BoundedSemaphore(10)              │  无限制         │
  │  连接池      PooledDB(max=10)                  │  单连接直连     │
  │  事务回滚    支持                               │  支持           │
  │  健康监控    30s 检测, 3 次失败→标记不健康      │  无             │
  │  空闲回收    5min TTL                          │  无 (立刻销毁)  │
  │  资源限制    cpu + memory 限制                  │  默认值         │
  │  镜像来源    预置 image 字段                    │  docker build   │
  │  参数注入    不支持                             │  db_params +    │
  │                                                │  mounts         │
  └─────────────────────────────────────────────────────────────────┘


### 7.5 镜像缓存与清理

  请求 version=3.0.8.299958
       │
       ▼
  ┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
  │ 检查本地镜像  │─No─►│ docker build │────►│ 缓存到本地        │
  │              │     │ (分钟级)     │     │ vastbase:3.0.8... │
  └──────┬───────┘     └──────────────┘     └──────────────────┘
         │ Yes
         ▼
   直接使用缓存 (秒级)

  清理策略:
  - 定制容器用完即 docker rm (auto_remove=True 兜底)
  - 孤儿镜像 (无容器引用 + 超过 N 天未使用) → 定时清理
  - docker image prune --filter "label=db-mcp"
