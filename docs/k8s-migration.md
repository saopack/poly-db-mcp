# poly-db-mcp K8s 迁移方案

> 2026-06-05

## 1. 目标

将 DB-MCP 从单机 Docker 部署迁移到 K8s 集群，突破单机资源瓶颈。核心能力不变：事务回滚、DDL 容器销毁、连接池、临时定制容器。

## 2. 部署架构

```
┌────────────────────────── K8s 集群（代码都在集群内，无外部依赖）──────────────────────────┐
│                                                                                            │
│                            ┌───────────────┐                                              │
│                            │  Ingress / LB │   → 外部 AI 平台（Dify 等）访问               │
│                            └───────┬───────┘                                              │
│                                    │                                                      │
│                            ┌───────▼───────┐                                              │
│                            │   Gateway     │  Deployment, replicas: 2                     │
│                            │   Service     │  ClusterIP                                   │
│                            └───────┬───────┘                                              │
│                                    │ routing.yaml → Service DNS 路由                       │
│                   ┌────────────────┼────────────────┐                                     │
│                   ▼                ▼                ▼                                     │
│       ┌─────────────────┐┌─────────────────┐┌─────────────────┐                           │
│       │ Node Service    ││ Node Service    ││ Node Service    │  ClusterIP，Gateway 路由用  │
│       │ "node-pg"       ││ "node-mysql"    ││ "node-oracle"   │                           │
│       └────────┬────────┘└────────┬────────┘└────────┬────────┘                           │
│                │                  │                  │                                     │
│       ┌────────▼────────┐┌────────▼────────┐┌────────▼────────┐                           │
│       │ Node Deployment ││ Node Deployment ││ Node Deployment │  --role node               │
│       │ (负责 pg/vb/kb) ││ (负责mysql/mss) ││ (负责 oracle)   │                           │
│       │                 ││                 ││                 │                           │
│       │ ★ 启动时调用    ││ ★ 启动时调用    ││ ★ 启动时调用    │  集群内调 K8s API           │
│       │   K8s API 动态  ││   K8s API 动态  ││   K8s API 动态  │  创建/删除 DB Pod           │
│       │   创建 DB Pod   ││   创建 DB Pod   ││   创建 DB Pod   │                             │
│       └────────┬────────┘└────────┬────────┘└────────┬────────┘                           │
│                │                  │                  │                                     │
│       ┌────────┴────────┐┌────────┴────────┐┌────────┴────────┐                           │
│       │ DB Pod 集群     ││ DB Pod 集群     ││ DB Pod 集群     │  Node 动态创建的             │
│       │                 ││                 ││                 │                             │
│       │ ┌─────────────┐ ││ ┌─────────────┐ ││ ┌─────────────┐ │                             │
│       │ │postgresql-14│ ││ │ mysql-8-0   │ ││ │ oracle-21c  │ │                             │
│       │ │ Pod + Svc   │ ││ │ Pod + Svc   │ ││ │ Pod + Svc   │ │                             │
│       │ └─────────────┘ ││ └─────────────┘ ││ └─────────────┘ │                             │
│       │ ┌─────────────┐ ││ ┌─────────────┐ ││ ┌─────────────┐ │                             │
│       │ │vastbase-3.0.9│ ││ │ mssql-2019  │ ││ │ oracle-19c  │ │                             │
│       │ │ Pod + Svc   │ ││ │ Pod + Svc   │ ││ │ Pod + Svc   │ │                             │
│       │ └─────────────┘ ││ └─────────────┘ ││ └─────────────┘ │                             │
│       │ ┌─────────────┐ ││                 ││                 │                             │
│       │ │kingbase-V8   │││                 ││                 │                             │
│       │ │ Pod + Svc   │││                 ││                 │                             │
│       │ └─────────────┘ ││                 ││                 │                             │
│       └─────────────────┘└─────────────────┘└─────────────────┘                             │
│                                                                                            │
│  ┌──────────────────────────────────────────────────────────────────────────────────────┐  │
│  │ K8s API Server                                                                       │  │
│  │   ← Node Pod 通过 ServiceAccount + RBAC 调（集群内，不需要外部 kubeconfig）           │  │
│  └──────────────────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

**关键点**：

- Node 部署在集群内，通过 ServiceAccount 调 K8s API，**不需要外部 kubeconfig**
- DB Pod 是 Node 代码动态创建的裸 Pod，不是 StatefulSet，不是 Deployment
- 每个 DB Pod 配一个 Service，方便 Node 通过 DNS 直连
- Gateway 无状态，水平扩展，通过 Service DNS 路由到各 Node

## 3. 核心流程

### 3.1 启动预热

```
Node Pod 启动
  → 读 databases.yaml
  → 遍历 prewarm: true 的 DB 版本:
      for each (db_type, version) in prewarm_list:
        1. K8s API: create Pod(image=postgres:14, env={...}, resources={...})
        2. K8s API: create Service(port=5432, selector={app: "postgresql-14"})
        3. WaitPortReady(pod-name.service:5432)
        4. 建立 DBUtils 连接池
  → 开始接收 API 请求
```

### 3.2 SQL 执行（DML — 事务回滚）

```
POST /api/execute_sql {"db_type":"postgresql","version":"14","query":"UPDATE ..."}
  → ContainerPool.lease("postgresql", "14")
    → 找到已有 Entry，从连接池借连接
    → adapter.execute_with_rollback(sql)
      → BEGIN → execute → ROLLBACK
    → release: 连接归还连接池
  → 返回结果

Pod 不受影响，连接池继续复用
```

### 3.3 DDL 执行（非事务型 DB — Pod 销毁重建）

```
POST /api/execute_sql {"db_type":"mysql","version":"8.0","query":"CREATE TABLE ..."}
  → ContainerPool.lease("mysql", "8.0")
    → adapter.execute(sql)              # MySQL DDL 不支持回滚
    → lease.mark_for_destroy()
    → release → destroy_container()
      → entry.connection_pool.close()   # 先关池
      → K8s API: delete Pod "mysql-8-0" # 删 Pod
      → K8s API: create Pod "mysql-8-0" # 重建（同名，Service 自动指向新 Pod）
      → WaitPodReady("mysql-8-0")
      → 重建 DBUtils 连接池              # Service DNS 不变
  → 返回结果
```

### 3.4 临时容器（GUC 自定义配置）

```
POST /api/execute_sql {
  "db_type":"vastbase","version":"3.0.9",
  "query":"SELECT 1",
  "params":"dolphin_server_port=3308"
}
  → MD5(params) = "a1b2c3d4"
  → pod_name = "vastbase-3.0.9-a1b2c3d4"
  → 查是否已有此 Pod:
      有 & HEALTHY → 复用连接池
      没有 → K8s API:
           create ConfigMap(自定义 postgresql.conf)
           create Pod(vastbase, env:OTHER_PG_CONF=params, volume:ConfigMap)
           create Service(port=5432)
           WaitReady → 建连接池
  → 午夜清理: 当天没用过的临时 Pod 全部删除
```

## 4. 代码改造

### 4.1 抽象层：`RuntimeBackend`

抽取 Docker 操作，Docker 和 K8s 各自实现同一套接口：

```python
# src/backend.py — 新建

class RuntimeBackend(ABC):
    """数据库实例生命周期管理。Docker 和 K8s 各自实现。"""

    @abstractmethod
    def ensure_image(self, image: str) -> None: ...

    @abstractmethod
    def create_instance(self, name: str, config: dict, db_type: str,
                        ephemeral_kwargs: dict = None) -> DatabaseEndpoint: ...

    @abstractmethod
    def get_or_start_instance(self, name: str, config: dict,
                               db_type: str) -> DatabaseEndpoint: ...

    @abstractmethod
    def remove_instance(self, name: str) -> None: ...

    @abstractmethod
    def check_health(self, host: str, port: int) -> bool: ...


@dataclass
class DatabaseEndpoint:
    host: str          # Docker: 127.0.0.1 ; K8s: pod-name.service.ns.svc.cluster.local
    port: int          # Docker: 随机映射 ; K8s: config 里的固定端口
    instance_id: str   # Docker: container_id ; K8s: pod_name
    instance_name: str # Docker: container_name ; K8s: pod_name
```

### 4.2 ContainerPool 改动

| 方法 | 改动 |
|------|------|
| `__init__` | 注入 `backend: RuntimeBackend` |
| `_pull_image_if_not_exists` | → `self._backend.ensure_image(image)` |
| `_create_and_start` | → `self._backend.create_instance(name, config, db_type)` |
| `_get_or_create_container` | → `self._backend.get_or_start_instance(name, config, db_type)` |
| `_create_ephemeral_container` | → `self._backend.create_instance(name, config, db_type, ephemeral_kwargs)` |
| `_remove_container` | → `self._backend.remove_instance(name)` |
| `_get_host_port` | 移除（K8s 端口固定） |
| `_resolve_resource_limits` | 移除（K8s Pod spec 里定义） |
| 连接池 host 参数 | `127.0.0.1` → `endpoint.host` |
| 健康检查 host | `127.0.0.1` → `endpoint.host` |
| **所有 lease/释放/信号量/状态机逻辑** | **不动** |

### 4.3 K8s 后端实现

```python
# src/k8s_backend.py — 新建

class K8sBackend(RuntimeBackend):
    def __init__(self, namespace: str = "db-mcp"):
        self._namespace = namespace
        # 集群内通过 ServiceAccount 鉴权
        from kubernetes import config
        config.load_incluster_config()
        self._core = client.CoreV1Api()

    def ensure_image(self, image: str):
        pass  # Pod spec 里 imagePullPolicy 管

    def create_instance(self, name, config, db_type, ephemeral_kwargs=None):
        # 1. 如果有 ephemeral_kwargs → 先建 ConfigMap
        # 2. 构建 Pod spec（image, env, ports, resources, volumes）
        # 3. 构建 Service spec（port, selector）
        # 4. create Pod + Service
        # 5. Wait Pod Ready
        # 6. return DatabaseEndpoint(host = f"{name}.{ns}.svc.cluster.local", ...)

    def remove_instance(self, name):
        # delete Pod + delete Service（ephemeral 的连 ConfigMap 一起删）

    def check_health(self, host, port):
        return ContainerPool._is_port_open(host, port)
```

### 4.4 运行时选择

```python
# api.py lifespan
def _create_backend() -> RuntimeBackend:
    runtime = os.environ.get("MCP_RUNTIME", "docker")
    if runtime == "k8s":
        namespace = os.environ.get("MCP_K8S_NAMESPACE", "db-mcp")
        return K8sBackend(namespace=namespace)
    return DockerBackend()

backend = _create_backend()
pool = ContainerPool(backend=backend)
```

### 4.5 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MCP_RUNTIME` | `docker` | `docker` 或 `k8s` |
| `MCP_K8S_NAMESPACE` | `db-mcp` | 所有 DB Pod 创建在这个命名空间 |

## 5. K8s 资源清单

### 5.1 固定资源（手动 `kubectl apply`）

```yaml
# --- Namespace ---
apiVersion: v1
kind: Namespace
metadata:
  name: db-mcp

# --- ServiceAccount + RBAC（Node 调 K8s API 用）---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: db-mcp-node
  namespace: db-mcp
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: db-mcp-node
  namespace: db-mcp
rules:
  - apiGroups: [""]
    resources: ["pods", "services", "configmaps"]
    verbs: ["get", "list", "watch", "create", "update", "delete"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: db-mcp-node
  namespace: db-mcp
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: db-mcp-node
subjects:
  - kind: ServiceAccount
    name: db-mcp-node
    namespace: db-mcp

# --- Secret（DB 密码）---
apiVersion: v1
kind: Secret
metadata:
  name: db-credentials
  namespace: db-mcp
stringData:
  postgres_password: "postgres123"
  mysql_password: "mysql123"
  oracle_password: "oracle123"
  sa_password: "SqlServer123!"

# --- Gateway ---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: db-mcp-gateway
  namespace: db-mcp
spec:
  replicas: 2
  selector:
    matchLabels:
      app: db-mcp-gateway
  template:
    metadata:
      labels:
        app: db-mcp-gateway
    spec:
      containers:
        - name: gateway
          image: db-mcp:latest
          command: ["python", "-m", "src.main"]
          args: ["--role", "gateway", "--host", "0.0.0.0", "--port", "8000"]
          ports:
            - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: db-mcp-gateway
  namespace: db-mcp
spec:
  selector:
    app: db-mcp-gateway
  ports:
    - port: 8000
      targetPort: 8000

# --- Node（示例：负责 PostgreSQL 家族）---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: db-mcp-node-pg
  namespace: db-mcp
spec:
  replicas: 1
  selector:
    matchLabels:
      app: db-mcp-node-pg
  template:
    metadata:
      labels:
        app: db-mcp-node-pg
    spec:
      serviceAccountName: db-mcp-node
      containers:
        - name: node
          image: db-mcp:latest
          command: ["python", "-m", "src.main"]
          args: ["--role", "node", "--host", "0.0.0.0", "--port", "8000"]
          env:
            - name: MCP_RUNTIME
              value: "k8s"
            - name: MCP_K8S_NAMESPACE
              value: "db-mcp"
          ports:
            - containerPort: 8000
---
apiVersion: v1
kind: Service
metadata:
  name: db-mcp-node-pg
  namespace: db-mcp
spec:
  selector:
    app: db-mcp-node-pg
  ports:
    - port: 8000
      targetPort: 8000
```

### 5.2 动态资源（Node 代码创建）

每个 DB 版本自动创建一对资源：

```yaml
# Node 调用 K8s API 创建 ↓
---
apiVersion: v1
kind: Service
metadata:
  name: postgresql-14
  namespace: db-mcp
spec:
  selector:
    app: postgresql-14
  ports:
    - port: 5432
      targetPort: 5432
---
apiVersion: v1
kind: Pod
metadata:
  name: postgresql-14
  namespace: db-mcp
  labels:
    app: postgresql-14
spec:
  containers:
    - name: postgresql
      image: postgres:14
      env:
        - name: POSTGRES_USER
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: postgres_user
        - name: POSTGRES_PASSWORD
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: postgres_password
      ports:
        - containerPort: 5432
      resources:
        requests:
          cpu: "1"
          memory: "512Mi"
        limits:
          cpu: "2"
          memory: "2Gi"
```

## 6. 文件改动清单

### 新建

| 文件 | 用途 |
|------|------|
| `src/backend.py` | `RuntimeBackend` ABC + `DockerBackend`（现有 Docker 逻辑提取） |
| `src/k8s_backend.py` | `K8sBackend`（K8s API 创建/删除 Pod + Service + ConfigMap） |
| `docs/k8s-migration.md` | 本文档 |

### 修改

| 文件 | 变更 |
|------|------|
| `src/container_pool.py` | 注入 backend，Docker SDK 调用 → backend 方法 |
| `src/api.py` | lifespan 中按 `MCP_RUNTIME` 构造对应 backend |
| `src/main.py` | `MCP_RUNTIME` 环境变量识别 |
| `src/config_manager.py` | 支持 `k8s_` 前缀的资源覆盖字段（可选） |
| `requirements.txt` | 加 `kubernetes` Python SDK |

### 不改

| 文件 | 理由 |
|------|------|
| `src/gateway/` | 无状态，与运行时无关 |
| `src/adapters/` | 纯 DB-API |
| `src/routes/` | 只消费 ContainerPool 接口 |
| `src/executor.py` | 租约接口不变 |
| `src/exceptions.py` | 异常体系不变 |

## 7. 实施步骤

### Step 1: 后端接口提取
- 新建 `src/backend.py`：ABC + DockerBackend
- ContainerPool 注入 backend，Docker 模式下行为不变
- 验证：`MCP_RUNTIME=docker` 全部现有测试通过

### Step 2: K8sBackend 实现
- 新建 `src/k8s_backend.py`
- 实现 Pod + Service + ConfigMap 创建/删除（`kubernetes` SDK）
- api.py 中按 `MCP_RUNTIME` 选择 backend

### Step 3: K8s 部署测试
- `kubectl apply` 部署 Gateway + Node + RBAC
- 验证 Node 启动后自动创建 DB Pod
- 验证 SQL 执行 + DDL destroy + 临时容器

## 8. Docker vs K8s 对比

| | Docker 模式 | K8s 模式 |
|---|---|---|
| 容器管理 | `docker` Python SDK | `kubernetes` Python SDK |
| 创建 | `docker.containers.run()` | `core_api.create_namespaced_pod()` |
| 删除 | `container.stop()` + `.remove()` | `core_api.delete_namespaced_pod()` |
| 连接地址 | `127.0.0.1:随机端口` | `pod-name.ns.svc.cluster.local:固定端口` |
| 配置注入 | 写临时文件 → 目录挂载 | ConfigMap 挂载 |
| 端口 | Docker 随机映射 | Service 固定端口 |
| 资源限制 | `mem_limit` + `nano_cpus` | Pod spec `resources` |
| 镜像管理 | `images.pull()` | `imagePullPolicy` |
| 健康检查 | 后台线程 TCP 连接 | 保留 TCP 连接检查 + 可选 K8s probe |
| 运行位置 | 本地机器 | 集群内 Pod（ServiceAccount 鉴权） |
