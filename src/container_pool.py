"""Container pool: thread-safe singleton managing Docker container lifecycle,
connection pooling, and concurrency control for database SQL execution.

Each container is identified by a (db_type, version, compatibility_mode) tuple.
Multiple concurrent requests share the same container and connection pool,
with a per-container semaphore limiting concurrent executions.
"""

import os
import base64
from datetime import date
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import time
import tempfile
import threading
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Optional, Tuple, Any

import docker
from docker.errors import DockerException, NotFound

logger = logging.getLogger(__name__)


class ContainerState(Enum):
    STARTING = auto()
    HEALTHY = auto()
    UNHEALTHY = auto()
    DESTROYING = auto()
    STOPPED = auto()


class ContainerPoolCapacityError(Exception):
    """Raised when max concurrency is reached for a container and the
    semaphore acquire times out."""


@dataclass
class ContainerEntry:
    container_name: str
    container_id: Optional[str] = None
    host_port: Optional[int] = None
    db_type: str = ""
    version: str = ""
    compat_mode: str = ""
    state: ContainerState = ContainerState.STOPPED
    connection_pool: Optional[Any] = None
    active_leases: int = 0
    max_concurrency: int = 10
    semaphore: Optional[threading.BoundedSemaphore] = None
    last_used: float = 0.0
    idle_ttl: int = 86400
    destroying: bool = False
    exclusive: bool = False
    health_failures: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition)

    def __post_init__(self):
        if self.semaphore is None:
            self.semaphore = threading.BoundedSemaphore(self.max_concurrency)


class ContainerLease:
    """Context manager that holds a leased connection from the pool."""

    def __init__(self, pool: 'ContainerPool', entry: ContainerEntry, conn: Any):
        self._pool = pool
        self._entry = entry
        self.connection = conn
        self.container_id = entry.container_id
        self._mark_destroy = False

    def mark_for_destroy(self):
        self._mark_destroy = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._pool.release(self._entry, self.connection)
        if self._mark_destroy:
            self._pool.destroy_container(
                self._entry.db_type, self._entry.version, self._entry.compat_mode
            )
        return False


def _make_container_key(db_type: str, version: str, compat_mode: str = "") -> str:
    key = f"{db_type}-{version}"
    if compat_mode:
        key = f"{key}-{compat_mode}"
    return key.lower()


# ---------------------------------------------------------------------------
# Per-database resource limits (hard caps with headroom above official minimums)
# Each entry: {"cpu": <cores>, "memory": "<docker mem_limit string>"}
# Memory is set above official minimums to avoid OOM during normal operation.
# If a specific version needs more, override via YAML resources or env vars.
# ---------------------------------------------------------------------------

_DEFAULT_RESOURCE_LIMITS = {
    "postgresql": {"cpu": 1, "memory": "2g"},
    "vastbase":   {"cpu": 2, "memory": "4g"},
    "kingbase":   {"cpu": 2, "memory": "6g"},
    "mysql":      {"cpu": 1, "memory": "2g"},
    "oracle":     {"cpu": 2, "memory": "6g"},
    "sqlserver":  {"cpu": 2, "memory": "4g"},
    "mssql":      {"cpu": 2, "memory": "4g"},
}


def _resolve_resource_limits(db_type: str, config: dict) -> dict:
    """Return resolved cpu/memory limits for a container.

    1. Start with per-database defaults (official minimums).
    2. Override from config['resources'] in databases.yaml if present.
    3. Override from MCP_RESOURCE_CPU_<TYPE> / MCP_RESOURCE_MEM_<TYPE>
       env vars if set.
    """
    limits = dict(_DEFAULT_RESOURCE_LIMITS.get(db_type.lower(), {"cpu": 1, "memory": "512m"}))

    config_resources = config.get("resources")
    if config_resources:
        if "cpu" in config_resources:
            limits["cpu"] = config_resources["cpu"]
        if "memory" in config_resources:
            limits["memory"] = config_resources["memory"]

    env_cpu = os.environ.get(f"MCP_RESOURCE_CPU_{db_type.upper()}")
    if env_cpu:
        limits["cpu"] = float(env_cpu)

    env_mem = os.environ.get(f"MCP_RESOURCE_MEM_{db_type.upper()}")
    if env_mem:
        limits["memory"] = env_mem

    return limits


# ---------------------------------------------------------------------------
# Driver-specific connection pool factories
# ---------------------------------------------------------------------------

def _psycopg2_pool_factory(config: dict, host: str, port: int, max_conn: int) -> Any:
    import psycopg2
    from dbutils.pooled_db import PooledDB

    return PooledDB(
        creator=psycopg2,
        maxconnections=max_conn,
        mincached=min(2, max_conn),
        maxcached=min(5, max_conn),
        maxusage=100,
        blocking=True,
        ping=1,
        host=host,
        port=port,
        user=config.get('username', 'postgres'),
        password=config.get('password', ''),
        database=config.get('database', 'postgres'),
    )


def _pymysql_pool_factory(config: dict, host: str, port: int, max_conn: int) -> Any:
    import pymysql
    from dbutils.pooled_db import PooledDB

    timeout = config.get('statement_timeout', 30)
    return PooledDB(
        creator=pymysql,
        maxconnections=max_conn,
        mincached=min(2, max_conn),
        maxcached=min(5, max_conn),
        maxusage=100,
        blocking=True,
        host=host,
        port=port,
        user=config.get('username', 'root'),
        password=config.get('password', ''),
        database=config.get('database', 'test'),
        autocommit=True,
        charset='utf8mb4',
        read_timeout=timeout,
        write_timeout=timeout,
        connect_timeout=10,
    )


def _oracledb_pool_factory(config: dict, host: str, port: int, max_conn: int) -> Any:
    import oracledb
    from dbutils.pooled_db import PooledDB

    database = config.get('database', 'XE')
    dsn = oracledb.makedsn(host, port, service_name=database)

    def _creator():
        return oracledb.connect(
            user=config.get('username', 'SYSTEM'),
            password=config.get('password', 'oracle'),
            dsn=dsn,
            tcp_connect_timeout=10,
        )

    return PooledDB(
        creator=_creator,
        maxconnections=max_conn,
        mincached=min(2, max_conn),
        maxcached=min(5, max_conn),
        maxusage=100,
        blocking=True,
    )


def _pymssql_pool_factory(config: dict, host: str, port: int, max_conn: int) -> Any:
    import pymssql
    from dbutils.pooled_db import PooledDB

    timeout = config.get('statement_timeout', 30)
    return PooledDB(
        creator=pymssql,
        maxconnections=max_conn,
        mincached=min(2, max_conn),
        maxcached=min(5, max_conn),
        maxusage=100,
        blocking=True,
        server=host,
        port=port,
        user=config.get('username', 'sa'),
        password=config.get('password', ''),
        database=config.get('database', 'master'),
        autocommit=True,
        login_timeout=10,
        timeout=timeout,
    )


_POOL_FACTORIES = {
    'postgresql': _psycopg2_pool_factory,
    'vastbase': _psycopg2_pool_factory,
    'kingbase': _psycopg2_pool_factory,
    'mysql': _pymysql_pool_factory,
    'oracle': _oracledb_pool_factory,
    'sqlserver': _pymssql_pool_factory,
    'mssql': _pymssql_pool_factory,
}


# ======================================================================
# ContainerPool
# ======================================================================

class ContainerPool:
    _instance: Optional['ContainerPool'] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> 'ContainerPool':
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self._pool_lock = threading.Lock()
        self._entries: Dict[str, ContainerEntry] = {}
        self._create_locks: Dict[str, threading.Lock] = {}
        self._shutting_down = threading.Event()
        self._docker_client: Optional[Any] = None
        self._max_concurrency = int(os.environ.get('MCP_MAX_CONCURRENCY', '10'))
        self._lease_timeout = int(os.environ.get('MCP_LEASE_TIMEOUT', '30'))
        self._db_ready_timeout = int(os.environ.get('MCP_DB_READY_TIMEOUT', '300'))
        self._idle_ttl = int(os.environ.get('MCP_CONTAINER_IDLE_TTL', '86400'))
        self._health_interval = int(os.environ.get('MCP_HEALTH_CHECK_INTERVAL', '30'))
        self._health_thread: Optional[threading.Thread] = None
        self._last_cleanup_date: Optional[str] = None

    # ---- public API -------------------------------------------------------

    @property
    def docker_client(self):
        if self._docker_client is None:
            self._docker_client = docker.from_env(timeout=60)
        return self._docker_client

    def shutdown(self):
        self._shutting_down.set()
        logger.info("ContainerPool: shutdown initiated, draining leases...")
        deadline = time.time() + 30
        while time.time() < deadline:
            with self._pool_lock:
                active = sum(e.active_leases for e in self._entries.values())
            if active == 0:
                break
            time.sleep(0.5)
        with self._pool_lock:
            for entry in self._entries.values():
                if entry.connection_pool:
                    try:
                        entry.connection_pool.close()
                    except Exception as e:
                        logger.warning(f"Error closing connection pool: {e}")
                    entry.connection_pool = None
                entry.state = ContainerState.STOPPED
            self._entries.clear()
            self._create_locks.clear()
        logger.info("ContainerPool: shutdown complete (containers preserved)")

    def prewarm(self):
        from .config_manager import ConfigManager

        databases = ConfigManager._config.get("databases", {})
        for db_type, db_cfg in databases.items():
            versions = db_cfg.get("versions", {})
            for version, ver_cfg in versions.items():
                config = ConfigManager.get_db_config(db_type, version)
                if not config or not config.get('prewarm'):
                    continue
                try:
                    self._ensure_healthy(db_type, version, config)
                except Exception as e:
                    logger.warning(f"ContainerPool prewarm: {db_type}/{version} failed: {e}")

        self._start_health_monitor()

    def lease(self, db_type: str, version: str, config: dict,
              compat_mode: str = "") -> ContainerLease:
        if self._shutting_down.is_set():
            raise ContainerPoolCapacityError("Server is shutting down")

        key = _make_container_key(db_type, version, compat_mode)
        entry = self._ensure_healthy(db_type, version, config, compat_mode)

        acquired = entry.semaphore.acquire(timeout=self._lease_timeout)
        if not acquired:
            raise ContainerPoolCapacityError(
                f"Too many concurrent requests for {db_type} {version}. "
                f"Max: {entry.max_concurrency}. Please retry later."
            )

        try:
            with ThreadPoolExecutor(max_workers=1) as _pool:
                future = _pool.submit(entry.connection_pool.connection)
                conn = future.result(timeout=self._lease_timeout)
        except FutureTimeoutError:
            entry.semaphore.release()
            raise ContainerPoolCapacityError(
                f"Timed out waiting for a DB connection after {self._lease_timeout}s "
                f"for {db_type} {version}"
            )
        except Exception:
            entry.semaphore.release()
            raise

        with self._pool_lock:
            entry.active_leases += 1
            entry.last_used = time.time()

        return ContainerLease(self, entry, conn)

    def exclusive_lease(self, db_type: str, version: str, config: dict,
                        compat_mode: str = "") -> ContainerLease:
        """Acquire exclusive access to a container (for DDL on non-transactional DBs).

        Blocks until all active leases are released, then prevents new leases
        from being acquired until this lease is released.
        """
        if self._shutting_down.is_set():
            raise ContainerPoolCapacityError("Server is shutting down")

        key = _make_container_key(db_type, version, compat_mode)
        entry = self._ensure_healthy(db_type, version, config, compat_mode)

        deadline = time.time() + 60
        while True:
            with self._pool_lock:
                if entry.active_leases == 0 and not entry.exclusive:
                    entry.exclusive = True
                    entry.active_leases = 1
                    entry.last_used = time.time()
                    break
            if time.time() > deadline:
                raise ContainerPoolCapacityError(
                    f"Timed out waiting for exclusive access to {db_type} {version}"
                )
            time.sleep(0.2)

        try:
            conn = entry.connection_pool.connection()
        except Exception:
            with self._pool_lock:
                entry.exclusive = False
                entry.active_leases = 0
            raise

        return ContainerLease(self, entry, conn)

    def lease_ephemeral(self, db_type: str, version: str, config: dict,
                        compat_mode: str = "",
                        ephemeral_kwargs: dict = None) -> ContainerLease:
        """Acquire a container with custom config mounts (postgresql.conf etc.).

        The container is registered in the pool for reuse and will be cleaned
        up at midnight if unused that day.
        """
        if self._shutting_down.is_set():
            raise ContainerPoolCapacityError("Server is shutting down")

        suffix = os.urandom(4).hex()
        key = _make_container_key(db_type, version, compat_mode)
        container_key = f"{key}-{suffix}"
        container_name = f"db-mcp-{container_key}"

        entry = ContainerEntry(
            container_name=container_name,
            db_type=db_type,
            version=version,
            compat_mode=compat_mode,
            max_concurrency=self._max_concurrency,
            idle_ttl=self._idle_ttl,
        )
        with self._pool_lock:
            self._entries[container_key] = entry

        entry.state = ContainerState.STARTING
        logger.info(f"ContainerPool: starting container {container_name} with custom config")

        try:
            # Download and volume-mount binary from Nexus for PSU / ephemeral versions
            if config.get('needs_binary_prep'):
                config = self._prepare_binaries(config, db_type)

            self._pull_image_if_not_exists(config['image'])
            container_id, host_port = self._create_ephemeral_container(
                container_name, config, db_type, ephemeral_kwargs
            )
            entry.container_id = container_id
            entry.host_port = host_port
            logger.info(
                f"ContainerPool: container {container_name} started on port {host_port}"
            )

            logger.info(
                f"ContainerPool: Waiting for {db_type} on 127.0.0.1:{host_port} "
                f"(timeout={self._db_ready_timeout}s) ..."
            )
            if not self._wait_for_db_ready('127.0.0.1', host_port, config, db_type,
                                           max_wait=self._db_ready_timeout):
                self._remove_container(container_name)
                raise RuntimeError(
                    f"Container {container_name} database not ready "
                    f"after {self._db_ready_timeout}s on port {host_port}"
                )
            logger.info(f"ContainerPool: Database ready on 127.0.0.1:{host_port}")

            pool_factory = _POOL_FACTORIES.get(db_type)
            if pool_factory is None:
                raise RuntimeError(f"No connection pool factory for {db_type}")
            entry.connection_pool = pool_factory(config, '127.0.0.1', host_port,
                                                 entry.max_concurrency)

            entry.state = ContainerState.HEALTHY
            entry.health_failures = 0
            entry.last_used = time.time()
            logger.info(f"ContainerPool: container {container_name} ready on port {host_port}")

        except Exception as e:
            logger.error(f"ContainerPool: failed to start {container_name}: {e}")
            entry.state = ContainerState.STOPPED
            with self._pool_lock:
                if container_key in self._entries:
                    del self._entries[container_key]
            raise

        acquired = entry.semaphore.acquire(timeout=self._lease_timeout)
        if not acquired:
            raise ContainerPoolCapacityError(
                f"Too many concurrent requests for {db_type} {version}. "
                f"Max: {entry.max_concurrency}. Please retry later."
            )

        try:
            conn = entry.connection_pool.connection()
        except Exception:
            entry.semaphore.release()
            raise

        with self._pool_lock:
            entry.active_leases += 1
            entry.last_used = time.time()

        return ContainerLease(self, entry, conn)

    def _create_ephemeral_container(self, container_name: str, config: dict,
                                     db_type: str, ephemeral_kwargs: dict = None
                                     ) -> Tuple[str, int]:
        """Create a one-shot container with auto_remove and optional env/volume injection."""
        image = config['image']
        port = config['port']

        run_kwargs = {
            'image': image,
            'name': container_name,
            'ports': {f"{port}/tcp": None},
            'detach': True,
            'remove': False,
            'privileged': config.get('privileged', False),
        }

        # Apply resource limits (CPU / memory)
        if db_type:
            limits = _resolve_resource_limits(db_type, config)
            run_kwargs['mem_limit'] = limits['memory']
            run_kwargs['nano_cpus'] = int(limits['cpu'] * 1e9)
            logger.info(
                f"ContainerPool: {container_name} resource limits: "
                f"cpu={limits['cpu']}, memory={limits['memory']}"
            )

        env = config.get('env')
        if env:
            env = dict(env)

        if ephemeral_kwargs:
            if ephemeral_kwargs.get('params'):
                if env is None:
                    env = {}
                guc_params = ephemeral_kwargs['params']
                env['OTHER_PG_CONF'] = guc_params
                logger.info(
                    "ContainerPool: %s GUC parameters:\n%s",
                    container_name,
                    guc_params.strip(),
                )

            temp_dirs = []
            volumes = {}
            for conf_key, conf_filename in [
                ('postgresql_conf', 'postgresql.conf'),
                ('pg_hba_conf', 'pg_hba.conf'),
            ]:
                content = ephemeral_kwargs.get(conf_key)
                if content:
                    tmpdir = tempfile.mkdtemp(prefix=f"db-mcp-conf-{container_name}-")
                    temp_dirs.append(tmpdir)
                    conf_path = os.path.join(tmpdir, conf_filename)
                    try:
                        decoded = base64.b64decode(content.encode()).decode()
                    except Exception:
                        decoded = content
                    with open(conf_path, 'w', encoding='utf-8') as f:
                        f.write(decoded)
                    volumes[conf_path] = {
                        'bind': f'/docker-entrypoint-initdb.d/{conf_filename}',
                        'mode': 'ro',
                    }
                    _lines = decoded.strip().split('\n')
                    _preview = '\n'.join(_lines[:20])
                    _more = f" ... (+{len(_lines) - 20} more lines)" if len(_lines) > 20 else ""
                    logger.info(
                        "ContainerPool: %s %s (%d lines, %d bytes):\n%s%s",
                        container_name, conf_filename,
                        len(_lines), len(decoded),
                        _preview, _more,
                    )

            # Mount extra files to /docker-entrypoint-initdb.d/
            extra_files = ephemeral_kwargs.get('extra_files') if ephemeral_kwargs else None
            if extra_files:
                for ef in extra_files:
                    file_name = ef.name if hasattr(ef, 'name') else ef['name']
                    content = ef.content if hasattr(ef, 'content') else ef['content']
                    tmpdir = tempfile.mkdtemp(prefix=f"db-mcp-extra-{container_name}-")
                    temp_dirs.append(tmpdir)
                    local_path = os.path.join(tmpdir, file_name)
                    try:
                        decoded = base64.b64decode(content.encode()).decode()
                    except Exception:
                        decoded = content
                    with open(local_path, 'w', encoding='utf-8') as f:
                        f.write(decoded)
                    volumes[local_path] = {
                        'bind': f'/docker-entrypoint-initdb.d/{file_name}',
                        'mode': 'ro',
                    }
                    logger.info(
                        "ContainerPool: %s extra file %s (%d bytes)",
                        container_name, file_name, len(decoded),
                    )

            if volumes:
                run_kwargs['volumes'] = volumes

        # Merge config-level volumes (e.g. binary mount from PackageManager)
        config_volumes = config.get('volumes')
        if config_volumes:
            if 'volumes' not in run_kwargs:
                run_kwargs['volumes'] = {}
            run_kwargs['volumes'].update(config_volumes)

        if env:
            run_kwargs['environment'] = env

        if config.get('command'):
            run_kwargs['command'] = config['command']

        logger.info(
            "ContainerPool: starting ephemeral container %s:\n    %s",
            container_name,
            self._format_docker_run_command(run_kwargs),
        )

        container = self.docker_client.containers.run(**run_kwargs)
        container.reload()
        if container.status != 'running':
            raise RuntimeError(
                f"Ephemeral container {container_name} failed to start (status: {container.status})"
            )

        host_port = self._get_host_port(container, port)
        return container.id, host_port

    def release(self, entry: ContainerEntry, conn: Any):
        try:
            conn.close()  # returns to DBUtils pool
        except Exception as e:
            logger.warning(f"ContainerPool: error returning connection to pool: {e}")

        with self._pool_lock:
            entry.active_leases = max(0, entry.active_leases - 1)
            if entry.exclusive:
                entry.exclusive = False
            entry.last_used = time.time()

        entry.semaphore.release()

    def destroy_container(self, db_type: str, version: str, compat_mode: str = ""):
        key = _make_container_key(db_type, version, compat_mode)
        with self._pool_lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.destroying = True
            entry.state = ContainerState.DESTROYING

        deadline = time.time() + 30
        while time.time() < deadline:
            with self._pool_lock:
                if entry.active_leases == 0:
                    break
            time.sleep(0.2)

        with self._pool_lock:
            self._destroy_entry(entry, reason="DDL contamination")
            if key in self._entries:
                del self._entries[key]

    def get_status(self):
        with self._pool_lock:
            return [
                {
                    "key": key,
                    "container_name": e.container_name,
                    "state": e.state.name,
                    "host_port": e.host_port,
                    "active_leases": e.active_leases,
                    "max_concurrency": e.max_concurrency,
                    "destroying": e.destroying,
                    "exclusive": e.exclusive,
                    "health_failures": e.health_failures,
                }
                for key, e in self._entries.items()
            ]

    # ---- internal ----------------------------------------------------------

    def _start_health_monitor(self):
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_thread = threading.Thread(target=self._health_monitor_loop,
                                               daemon=True, name="container-health")
        self._health_thread.start()

    def _health_monitor_loop(self):
        while not self._shutting_down.wait(self._health_interval):
            # Snapshot healthy entries to avoid holding lock during socket I/O
            with self._pool_lock:
                snapshot = [
                    (key, entry) for key, entry in self._entries.items()
                    if entry.state == ContainerState.HEALTHY
                ]
            for key, entry in snapshot:
                if not self._is_port_open('127.0.0.1', entry.host_port):
                    with self._pool_lock:
                        if key not in self._entries:
                            continue
                        entry.health_failures += 1
                        if entry.health_failures >= 3 and entry.state == ContainerState.HEALTHY:
                            logger.warning(
                                f"ContainerPool: {key} health check failed {entry.health_failures} times, "
                                "marking UNHEALTHY"
                            )
                            entry.state = ContainerState.UNHEALTHY
                else:
                    with self._pool_lock:
                        if key in self._entries:
                            entry.health_failures = 0

            # Midnight cleanup: remove containers idle longer than idle_ttl
            today = date.today().isoformat()
            if self._last_cleanup_date != today:
                self._last_cleanup_date = today
                now = time.time()
                with self._pool_lock:
                    for key, entry in list(self._entries.items()):
                        if (entry.state == ContainerState.HEALTHY
                                and entry.active_leases == 0
                                and not entry.exclusive):
                            idle_seconds = now - entry.last_used
                            if idle_seconds > entry.idle_ttl:
                                logger.info(
                                    f"ContainerPool: midnight cleanup removing container {key} "
                                    f"(idle for {idle_seconds:.0f}s, ttl={entry.idle_ttl}s)"
                                )
                                self._destroy_entry(entry, reason="midnight cleanup")
                                del self._entries[key]

    def _ensure_healthy(self, db_type: str, version: str, config: dict,
                        compat_mode: str = "") -> ContainerEntry:
        key = _make_container_key(db_type, version, compat_mode)
        create_lock = self._get_create_lock(key)

        with create_lock:
            with self._pool_lock:
                entry = self._entries.get(key)
                if entry is not None:
                    if entry.state == ContainerState.HEALTHY:
                        return entry
                    if entry.state == ContainerState.DESTROYING:
                        raise ContainerPoolCapacityError(
                            f"Container for {db_type} {version} is being destroyed, please retry"
                        )
                    if entry.state == ContainerState.UNHEALTHY:
                        self._destroy_entry(entry, reason="unhealthy, will recreate")
                        del self._entries[key]
                        entry = None

            if entry is None:
                entry = ContainerEntry(
                    container_name=f"db-mcp-{key}",
                    db_type=db_type,
                    version=version,
                    compat_mode=compat_mode,
                    max_concurrency=self._max_concurrency,
                    idle_ttl=self._idle_ttl,
                )
                with self._pool_lock:
                    self._entries[key] = entry

            self._start_entry(entry, config, db_type)
            return entry

    def _get_create_lock(self, key: str) -> threading.Lock:
        with self._pool_lock:
            if key not in self._create_locks:
                self._create_locks[key] = threading.Lock()
            return self._create_locks[key]

    def _prepare_binaries(self, config: dict, db_type: str) -> dict:
        """Download DB binary from Nexus and add a volume mount to *config*.

        Returns a shallow copy of *config* with ``volumes`` updated so the
        original dict is not mutated.
        """
        from .config_manager import ConfigManager
        from .package_manager import PackageManager

        nexus_config = ConfigManager.get_nexus_config(db_type)
        if not nexus_config:
            raise RuntimeError(
                f"{db_type}: needs_binary_prep set but no nexus config found"
            )

        nexus_version = config.get('nexus_version', '')
        if not nexus_version:
            raise RuntimeError(
                f"{db_type}: needs_binary_prep set but no nexus_version in config"
            )

        package_mgr = PackageManager()
        binary_path = package_mgr.prepare_binaries(
            db_type, nexus_version, nexus_config,
        )

        config = dict(config)
        volumes = dict(config.get('volumes', {}))
        volumes[binary_path] = {'bind': '/home/vastbase/vastbase', 'mode': 'rw'}
        config['volumes'] = volumes

        logger.info(
            "ContainerPool: mounted Nexus binary %s → /home/vastbase/vastbase "
            "(%s %s)", binary_path, db_type, nexus_version,
        )
        return config

    def _start_entry(self, entry: ContainerEntry, config: dict, db_type: str):
        entry.state = ContainerState.STARTING

        try:
            # Download and volume-mount binary from Nexus for PSU / ephemeral versions
            if config.get('needs_binary_prep'):
                config = self._prepare_binaries(config, db_type)

            self._pull_image_if_not_exists(config['image'])
            container_id, host_port = self._get_or_create_container(
                entry.container_name, config, db_type
            )
            entry.container_id = container_id
            entry.host_port = host_port

            logger.info(
                f"ContainerPool: Waiting for {db_type} on 127.0.0.1:{host_port} "
                f"(timeout={self._db_ready_timeout}s) ..."
            )
            if not self._wait_for_db_ready('127.0.0.1', host_port, config, db_type,
                                           max_wait=self._db_ready_timeout):
                raise RuntimeError(f"Container {entry.container_name} database on port {host_port} not ready")
            logger.info(f"ContainerPool: Database ready on 127.0.0.1:{host_port}")

            pool_factory = _POOL_FACTORIES.get(db_type)
            if pool_factory is None:
                raise RuntimeError(f"No connection pool factory for {db_type}")
            logger.info(f"ContainerPool: creating connection pool for {entry.container_name}")
            entry.connection_pool = pool_factory(config, '127.0.0.1', host_port,
                                                 entry.max_concurrency)

            entry.state = ContainerState.HEALTHY
            entry.health_failures = 0
            entry.last_used = time.time()
            logger.info(f"ContainerPool: container {entry.container_name} ready on port {host_port}")

        except Exception as e:
            logger.error(f"ContainerPool: failed to start {entry.container_name}: {e}")
            entry.state = ContainerState.STOPPED
            raise

    def _get_or_create_container(self, container_name: str, config: dict,
                                  db_type: str = "") -> Tuple[str, int]:
        port = config['port']
        is_ephemeral = config.get('needs_binary_prep', False)

        try:
            existing = self.docker_client.containers.get(container_name)
        except NotFound:
            existing = None

        if existing is not None:
            if existing.status == 'running':
                try:
                    host_port = self._get_host_port(existing, port)
                    logger.info(
                        "ContainerPool: reusing running container %s on port %s",
                        container_name, host_port,
                    )
                    return existing.id, host_port
                except Exception:
                    logger.warning(
                        "Container %s has no port mapping, removing...",
                        container_name,
                    )
                    self._remove_container(container_name)
            elif existing.status in ('exited', 'stopped', 'created'):
                # Ephemeral containers must be recreated to ensure binary
                # mounts are applied correctly on restart.
                if is_ephemeral:
                    logger.info(
                        "Container %s exists (%s) but needs_binary_prep, removing "
                        "and recreating to ensure volume mounts",
                        container_name, existing.status,
                    )
                    self._remove_container(container_name)
                else:
                    logger.info(
                        "Container %s exists but is %s, restarting...",
                        container_name, existing.status,
                    )
                    try:
                        existing.start()
                        existing.reload()
                        host_port = self._get_host_port(existing, port)
                        logger.info(
                            "Container %s restarted on port %s",
                            container_name, host_port,
                        )
                        return existing.id, host_port
                    except Exception as e:
                        logger.warning(
                            "Failed to restart container %s: %s, removing...",
                            container_name, e,
                        )
                        self._remove_container(container_name)
            else:
                logger.warning(
                    "Container %s in unexpected status %s, removing...",
                    container_name, existing.status,
                )
                self._remove_container(container_name)

        return self._create_and_start(container_name, config, db_type)

    @staticmethod
    def _format_docker_run_command(run_kwargs: dict) -> str:
        """Build a human-readable ``docker run`` command string from run_kwargs."""
        parts = ["docker run"]

        if run_kwargs.get('detach'):
            parts.append("-d")

        name = run_kwargs.get('name')
        if name:
            parts.append(f"--name {name}")

        if run_kwargs.get('privileged'):
            parts.append("--privileged")

        mem = run_kwargs.get('mem_limit')
        if mem:
            parts.append(f"--memory {mem}")

        nano = run_kwargs.get('nano_cpus')
        if nano:
            cpu = nano / 1e9
            if cpu == int(cpu):
                parts.append(f"--cpus {int(cpu)}")
            else:
                parts.append(f"--cpus {cpu}")

        env = run_kwargs.get('environment')
        if env:
            for k, v in sorted(env.items()):
                parts.append(f"-e {k}={v}")

        volumes = run_kwargs.get('volumes')
        if volumes:
            for host_path, vol_cfg in sorted(volumes.items()):
                bind = vol_cfg.get('bind', host_path)
                mode = vol_cfg.get('mode', '')
                flag = f"-v {host_path}:{bind}"
                if mode:
                    flag += f":{mode}"
                parts.append(flag)

        ports = run_kwargs.get('ports')
        if ports:
            for container_port in sorted(ports.keys()):
                host_binding = ports[container_port]
                port_num = container_port.replace('/tcp', '')
                if host_binding is None:
                    parts.append(f"-p {port_num}")
                elif isinstance(host_binding, int):
                    parts.append(f"-p {host_binding}:{port_num}")
                elif isinstance(host_binding, (list, tuple)) and host_binding:
                    parts.append(f"-p {host_binding[0]}:{port_num}")

        parts.append(run_kwargs.get('image', ''))

        command = run_kwargs.get('command')
        if command:
            parts.append(command)

        return " \\\n    ".join(parts)

    def _create_and_start(self, container_name: str, config: dict,
                           db_type: str = "") -> Tuple[str, int]:
        image = config['image']
        port = config['port']

        run_kwargs = {
            'image': image,
            'name': container_name,
            'ports': {f"{port}/tcp": None},
            'detach': True,
            'remove': False,
            'privileged': config.get('privileged', False),
        }

        # Apply resource limits (CPU / memory)
        if db_type:
            limits = _resolve_resource_limits(db_type, config)
            run_kwargs['mem_limit'] = limits['memory']
            run_kwargs['nano_cpus'] = int(limits['cpu'] * 1e9)

        env = config.get('env')
        if env:
            run_kwargs['environment'] = env

        if config.get('command'):
            run_kwargs['command'] = config['command']

        volumes = config.get('volumes')
        if volumes:
            run_kwargs['volumes'] = volumes

        logger.info(
            "ContainerPool: starting container %s:\n    %s",
            container_name,
            self._format_docker_run_command(run_kwargs),
        )

        try:
            container = self.docker_client.containers.run(**run_kwargs)
        except DockerException as e:
            raise RuntimeError(f"Failed to create container {container_name}: {e}")

        container.reload()
        if container.status != 'running':
            raise RuntimeError(f"Container {container_name} failed to start (status: {container.status})")

        host_port = self._get_host_port(container, port)
        return container.id, host_port

    def _pull_image_if_not_exists(self, image: str):
        try:
            self.docker_client.images.get(image)
            logger.debug(f"Image found locally: {image}")
        except NotFound:
            logger.info(f"Pulling Docker image: {image}")
            self.docker_client.images.pull(image)

    def _get_host_port(self, container, container_port: int) -> int:
        port_key = f"{container_port}/tcp"
        deadline = time.time() + 10
        while True:
            try:
                container.reload()
            except Exception:
                pass
            ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
            if port_key in ports and ports[port_key]:
                try:
                    return int(ports[port_key][0]['HostPort'])
                except (ValueError, KeyError, TypeError):
                    pass
            if time.time() > deadline:
                raise RuntimeError(f"Failed to get host port mapping for {container_port}/tcp")
            time.sleep(0.5)

    def _remove_container(self, container_name: str):
        try:
            existing = self.docker_client.containers.get(container_name)
            try:
                existing.stop()
            except Exception:
                pass
            existing.remove()
            logger.info(f"Removed container: {container_name}")
        except NotFound:
            pass
        except Exception as e:
            logger.warning(f"Failed to remove container {container_name}: {e}")

    def _destroy_entry(self, entry: ContainerEntry, reason: str = ""):
        logger.info(f"ContainerPool: destroying container {entry.container_name} reason={reason}")
        if entry.connection_pool:
            try:
                entry.connection_pool.close()
            except Exception as e:
                logger.warning(f"Error closing connection pool: {e}")
            entry.connection_pool = None
        self._remove_container(entry.container_name)
        entry.state = ContainerState.STOPPED
        entry.container_id = None
        entry.host_port = None

    def _wait_for_port(self, host: str, port: int, max_wait: int = 120, interval: int = 2) -> bool:
        start = time.time()
        while time.time() - start < max_wait:
            if self._is_port_open(host, port):
                return True
            time.sleep(interval)
        return False

    @staticmethod
    def _is_port_open(host: str, port: int, timeout: float = 1) -> bool:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                return sock.connect_ex((host, port)) == 0
        except Exception:
            return False

    def _wait_for_db_ready(self, host: str, port: int, config: dict, db_type: str,
                           max_wait: int = 120, interval: int = 3) -> bool:
        """Wait until the database is ready to accept connections and execute queries."""
        start = time.time()
        attempt = 0
        last_error = None
        while time.time() - start < max_wait:
            attempt += 1
            ok, err = self._try_db_connect(host, port, config, db_type)
            if ok:
                return True
            if err != last_error:
                logger.info(
                    f"ContainerPool: DB not ready on {host}:{port} "
                    f"(attempt {attempt}): {err}"
                )
                last_error = err
            time.sleep(interval)
        logger.error(
            f"ContainerPool: DB still not ready after {attempt} attempts "
            f"({max_wait}s), last error: {last_error}"
        )
        return False

    def _try_db_connect(self, host: str, port: int, config: dict, db_type: str) -> tuple:
        """Try a single connection + SELECT 1 to verify the DB is truly ready.
        Returns (True, None) on success, (False, error_message) on failure.
        """
        try:
            dt = db_type.lower()
            if dt in ('postgresql', 'vastbase', 'kingbase'):
                import psycopg2
                conn = psycopg2.connect(
                    host=host, port=port,
                    user=config.get('username', 'postgres'),
                    password=config.get('password', ''),
                    database=config.get('database', 'postgres'),
                    connect_timeout=5,
                )
                conn.close()
                return True, None
            elif dt == 'mysql':
                import pymysql
                conn = pymysql.connect(
                    host=host, port=port,
                    user=config.get('username', 'root'),
                    password=config.get('password', ''),
                    database=config.get('database', 'test'),
                    connect_timeout=5,
                )
                conn.close()
                return True, None
            elif dt == 'oracle':
                import oracledb
                database = config.get('database', 'XE')
                dsn = oracledb.makedsn(host, port, service_name=database)
                conn = oracledb.connect(
                    user=config.get('username', 'SYSTEM'),
                    password=config.get('password', 'oracle'),
                    dsn=dsn,
                    tcp_connect_timeout=5,
                )
                conn.close()
                return True, None
            elif dt in ('sqlserver', 'mssql'):
                import pymssql
                conn = pymssql.connect(
                    server=host, port=port,
                    user=config.get('username', 'sa'),
                    password=config.get('password', ''),
                    database=config.get('database', 'master'),
                    login_timeout=5,
                )
                conn.close()
                return True, None
            else:
                port_open = self._is_port_open(host, port, timeout=3)
                if port_open:
                    return True, None
                return False, f"port {port} not reachable"
        except Exception as e:
            return False, str(e)
