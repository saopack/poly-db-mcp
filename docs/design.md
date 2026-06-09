# poly-db-mcp 分布式架构设计

> v0.0.2 | 2026-06-03

## 1. 背景

poly-db-mcp 为 AI 平台提供 6 种数据库（Vastbase/金仓/PG/Oracle/MySQL/SQL Server）的沙箱 SQL 执行环境。当前单机跑 15-20 个 Docker 容器，面临**单机资源瓶颈**和**单点故障**两个问题。

**目标**：分散容器到多机，自动故障转移。Node 端代码不改。

## 2. 架构

```
                 AI 平台
                    │
            ┌───────▼───────┐
            │    Gateway     │  无状态路由 :8000
            │  routing.yaml  │  (db_type,ver) → Node[]
            │  自动 failover  │  primary 不通 → backup
            └───┬───┬───┬───┘
                │   │   │
       ┌────────▼┐ ┌▼───────┐ ┌▼──────────┐
       │ Node PG │ │Node Ora│ │ Node MySQL │
       │ PG/VB/KB│ │ Oracle │ │ MySQL/MSSQL│
       └─────────┘ └────────┘ └────────────┘
       (每台机器只部署自己负责的DB类型)
```

| 职责 | Gateway | Node |
|------|:---:|:---:|
| 路由 / failover / 聚合 | ✅ | — |
| Docker / 连接池 / SQL | — | ✅ |
| MCP / OAuth / 审计 | 路由 | ✅ 实际处理 |

**Gateway 无状态**，可部署多实例。Node 是当前单机部署，零改动。

## 3. 路由匹配

三级匹配，自动覆盖 Vastbase 临时容器（Nexus PSU/Build 版本）：

```
lookup(db_type, version)
  ├─ 1. 精确匹配   (vastbase, 3.0.9)       → node-pg
  ├─ 2. 前缀匹配   (vastbase, 3.0.9.psu01) → 匹配 3.0.9
  │               (vastbase, 3.0.8.29475) → 匹配 3.0.8
  └─ 3. 类型回退   (vastbase, 未知版本)     → any vastbase node
                  (clickhouse, 1.0)        → 404
```

## 4. 容灾

同一 `(db_type, version)` 声明在多个 Node，按配置顺序 = 优先级：

```yaml
nodes:
  node-pg:                        # primary
    address: "192.168.1.10:8000"
    databases: [{db_type: postgresql, versions: [12,13,14]}, ...]

  node-pg-backup:                 # failover
    address: "192.168.1.20:8000"
    databases: [{db_type: postgresql, versions: [12,13,14]}, ...]
```

故障转移流程：

```
请求 → primary 超时 → 日志 "trying next replica..." → backup 响应 ✓
全部不可达 → 502
```

Gateway 多实例：平台配两个地址，主不通切备。不需要 nginx。

| 故障 | 恢复 |
|------|------|
| Node 宕机 | Gateway 自动 failover 到备份 Node |
| Gateway 宕机 | 平台切换到备 Gateway |
| 单容器异常 | Node 本地健康检测自动重建 |
| 所有 Node 宕机 | 502，恢复后容器自动重建 |

## 5. 端点路由行为

| 端点 | Gateway 行为 |
|------|-------------|
| `POST /api/execute_sql` | 解析 db_type+version → lookup → forward(含failover) |
| `POST /api/dify/execute_sql` | 同上 |
| `GET /api/health` | scatter 广播所有 Node → 聚合 |
| `GET /api/databases` | scatter → 合并去重 |
| `POST /mcp` | `tools/list` → scatter；`tools/call` → lookup → forward |
| `GET /sse` | lookup → 流式透传到 primary |
| `POST /register` / `/token` / `/authorize` | → 转发到首个 Node（auth 统一存储） |
| `/api/clients/*` | → 转发到首个 Node |
| `/.well-known/oauth-authorization-server` | Gateway 自身返回（URL 指向 Gateway） |

> **约束**：OAuth 客户端数据存于首个 Node 内存。后续版本考虑共享存储或 Gateway 级 ClientRegistry。

## 6. 启动方式

```bash
# Node（默认）
python -m src.main --role node --port 8001

# Gateway
python -m src.main --role gateway --port 8000

# 环境变量
MCP_ROLE=gateway MCP_ROUTING_CONFIG=config/routing.yaml
```

## 7. 文件变更

**新建**：`src/gateway/{router,proxy,routes,app}.py` | `config/routing.yaml` | `tests/test_gateway_{router,proxy}.py` (47 单测)

**修改**：`src/main.py`（+`--role`）| `src/config_manager.py`（+`load_routing_config`）| `AGENTS.md`

**不动**：`container_pool` / `executor` / `adapters` / `routes/*` / `mcp/*` — Node 全栈零改动

## 8. 设计决策

| 决策 | 理由 |
|------|------|
| HTTP 代理，非 MCP 协议路由 | Node 零改动；SSE 直接透传 |
| 静态 YAML，非服务发现 | DB 版本→机器映射高度稳定 |
| 副本声明顺序，非轮询 | 容器有预热连接池，轮询打散缓存 |
| 三级版本匹配 | 临时容器版本不可预知 |
| SSE 仅 primary | 流中断无法无缝切换 |
| Gateway 不主动健康探测 | 保持完全无状态 |

## 9. 部署 Checklist

- [ ] 3-4 台机器，安装 Docker + Python 3.8+，`pip install -r requirements.txt`
- [ ] 每台 Node 裁剪 `databases.yaml`（仅保留自己负责的 DB）
- [ ] 编辑 `config/routing.yaml`，填入各机器 IP + 端口，可选配备份 Node
- [ ] Node: `python -m src.main --role node` | Gateway: `python -m src.main --role gateway`
- [ ] `curl gateway:8000/api/health` → 所有 Node 状态 OK
- [ ] 平台配 DB-MCP 地址为 Gateway URL（多 Gateway 则配双地址）
