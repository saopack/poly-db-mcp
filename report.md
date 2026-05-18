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

### 2.6 REST API（validation_routes.py + client_routes.py + api.py）— 0.5d

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
| REST API | validation_routes.py + client_routes.py + api.py | 0.5d | SQL 执行端点（async 线程池执行+审计日志）、数据库信息端点（类型列表/版本列表）、客户端管理端点（5个CRUD）、健康检查（config+docker 双状态）+ 关机端点；FastAPI lifespan 容器预热清理 + Swagger/ReDoc 自动文档 |
| 配置管理 | config_manager.py | 0.25d | YAML 加载 + Pydantic 三层模型校验；查询接口（get_db_config/get_supported_databases/get_db_versions/is_config_valid）；线程安全的配置读写 |
| 审计与运维 | main.py + exceptions.py + dependencies.py | 0.25d | 进程管理（--daemon/--stop/--restart，跨平台 Win32+Unix PID 管理）；双通道日志（控制台+RotatingFileHandler 滚动）；8 个异常子类精确映射 HTTP 状态码；惰性单例依赖注入 |

---

## 四、后续待开发功能

| # | 功能 | 工作量 | 说明 |
|---|------|--------|------|
| 1 | 客户端持久化 (SQLite) | 2d | ClientRegistry 目前全部内存存储，重启丢失。新增 SQLite 存储层 |
| 2 | API Key 哈希存储 | 1d | 注册时 bcrypt 哈希入库，只返回一次明文 |
| 3 | 速率限制 | 2d | 基于 client_id 的内存令牌桶，可配置 QPS |
| 4 | SQL 危险操作拦截 | 2d | 可配置黑名单正则，拦截 DROP DATABASE、无 WHERE 的 DELETE 等 |
| 5 | 健康检查增强 | 1d | 增加各 db_type 已运行容器的连通性探测 |
| 6 | 统一查询超时 | 2d | executor 层 asyncio.timeout 兜底 + 各适配器实现超时设置 |
