import docker
import logging
import time
import socket
from docker.errors import DockerException, NotFound
from typing import Dict, Any, Optional, Tuple
from .exceptions import (
    DockerError,
    DockerContainerStartError,
    DockerContainerPortError,
)

logger = logging.getLogger(__name__)

class DockerManager:
    def __init__(self):
        try:
            self.client = docker.from_env()
        except DockerException as e:
            raise DockerError(f"Docker client initialization failed: {str(e)}")
        self._warm_pool: Dict[str, float] = {}
        self._pool_ttl = 300  # 5 minutes idle TTL for warm containers

    def warmup(self, db_type: str, version: str, config: Dict[str, Any]) -> Optional[Tuple[str, int]]:
        """Pre-start a container and add it to the warm pool for reuse."""
        container_name = f"db-mcp-{db_type.lower()}-{version.lower()}"
        try:
            existing = self.client.containers.get(container_name)
            if existing.status == 'running':
                port = config['port']
                try:
                    host_port = self._get_host_port(existing, port)
                except DockerContainerPortError:
                    logger.warning(f"Warmup: container {container_name} has no port mapping, removing...")
                    self._remove_container(container_name)
                else:
                    self._warm_pool[container_name] = time.time()
                    logger.info(f"Warmup: reusing existing container {container_name}")
                    return existing.id, host_port
            else:
                self._remove_container(container_name)
        except NotFound:
            pass

        try:
            container_id, host_port = self.start_container(db_type, version, config)
            self._warm_pool[container_name] = time.time()
            logger.info(f"Warmup: container {container_name} ready on port {host_port}")
            return container_id, host_port
        except Exception as e:
            logger.warning(f"Warmup failed for {container_name}: {e}")
            return None

    def _try_reuse_warm_container(self, container_name: str, port: int) -> Optional[Tuple[str, int]]:
        """Check the warm pool for a reusable container."""
        if container_name not in self._warm_pool:
            return None
        try:
            existing = self.client.containers.get(container_name)
            if existing.status == 'running':
                try:
                    host_port = self._get_host_port(existing, port)
                except DockerContainerPortError:
                    logger.warning(
                        f"Warm container {container_name} has no port mapping, removing..."
                    )
                    self._remove_container(container_name)
                    self._warm_pool.pop(container_name, None)
                    return None
                del self._warm_pool[container_name]
                logger.info(f"Reusing warm container: {container_name}")
                return existing.id, host_port
            else:
                self._warm_pool.pop(container_name, None)
                self._remove_container(container_name)
        except NotFound:
            pass
        self._warm_pool.pop(container_name, None)
        return None

    def stop_all_warm_containers(self) -> None:
        """Stop all containers in the warm pool. Called on server shutdown."""
        for name in list(self._warm_pool.keys()):
            try:
                container = self.client.containers.get(name)
                container.stop()
                logger.info(f"Stopped warm container on shutdown: {name}")
            except NotFound:
                pass
            except Exception as e:
                logger.warning(f"Failed to stop container {name}: {e}")
            self._warm_pool.pop(name, None)

    def _cleanup_stale_warm_containers(self) -> None:
        """Remove containers that have been idle too long."""
        now = time.time()
        stale = [
            name for name, ts in self._warm_pool.items()
            if now - ts > self._pool_ttl
        ]
        for name in stale:
            try:
                container = self.client.containers.get(name)
                container.stop()
                logger.info(f"Cleaned up stale warm container: {name}")
            except NotFound:
                pass
            except Exception as e:
                logger.warning(f"Failed to clean up warm container {name}: {e}")
            self._warm_pool.pop(name, None)

    def _remove_container(self, container_name: str) -> None:
        """Stop and remove a container by name, ignoring errors."""
        try:
            existing = self.client.containers.get(container_name)
            try:
                existing.stop()
            except Exception:
                pass
            existing.remove()
            logger.info(f"Removed stale container: {container_name}")
        except NotFound:
            pass
        except Exception as e:
            logger.warning(f"Failed to remove container {container_name}: {e}")

    def start_container(self, db_type: str, version: str, config: Dict[str, Any], container_name: str = None) -> Tuple[str, int]:
        image = config['image']
        port = config['port']
        if container_name is None:
            container_name = f"db-mcp-{db_type.lower()}-{version.lower()}"
        else:
            container_name = container_name.lower()

        logger.info(f"Starting container for {db_type}:{version} (image={image}, port={port}, name={container_name})")

        self._cleanup_stale_warm_containers()

        # Try warm pool first
        warm_result = self._try_reuse_warm_container(container_name, port)
        if warm_result:
            logger.info(f"Reused warm container {container_name}, id={warm_result[0][:12]}")
            return warm_result

        try:
            logger.info(f"Checking image: {image}")
            self._pull_image_if_not_exists(image)
            logger.info(f"Image ready: {image}")

            env = config.get('env')
            if not env:
                if db_type == 'vastbase':
                    env = {
                        'VB_USERNAME': config.get('username', ''),
                        'VB_PASSWORD': config.get('password', ''),
                        'VB_DBCOMPATIBILITY': 'A',
                    }
                else:
                    env = {
                        'POSTGRES_USER': config.get('username', ''),
                        'POSTGRES_PASSWORD': config.get('password', ''),
                        'POSTGRES_DB': config.get('database', '')
                    }
            logger.info(f"Container env keys: {list(env.keys()) if env else 'none'}")

            # Check for existing container — only reuse if running with valid port
            try:
                existing = self.client.containers.get(container_name)
                logger.info(f"Found existing container {container_name}: id={existing.id[:12]}, status={existing.status}")
                if existing.status == 'running':
                    try:
                        host_port = self._get_host_port(existing, port)
                        logger.info(f"Container already running: {existing.id[:12]} on port {host_port}")
                        return existing.id, host_port
                    except DockerContainerPortError:
                        logger.warning(
                            f"Running container {container_name} has no port mapping, removing..."
                        )
                else:
                    logger.warning(
                        f"Existing container {container_name} is in '{existing.status}' state, removing..."
                    )
                self._remove_container(container_name)
            except NotFound:
                logger.info(f"No existing container named {container_name}")

            logger.info(f"Creating+starting container: name={container_name}, image={image}, port={port}")
            run_kwargs = {
                'image': image,
                'name': container_name,
                'ports': {f"{port}/tcp": None},
                'detach': True,
                'remove': False,
                'environment': env,
                'privileged': config.get('privileged', False),
            }
            if config.get('command'):
                run_kwargs['command'] = config['command']
            container = self.client.containers.run(**run_kwargs)
            logger.info(f"Container created: id={container.id[:12]}, status={container.status}")

            # Verify container actually started
            container.reload()
            logger.info(f"Container after reload: id={container.id[:12]}, status={container.status}")
            if container.status != 'running':
                raise DockerContainerStartError(
                    f"Container {container_name} failed to start (status: {container.status})"
                )

            host_port = self._get_host_port(container, port)
            logger.info(f"Container started: id={container.id[:12]}, host_port={host_port} for {db_type}:{version}")
            return container.id, host_port
        except DockerException as e:
            logger.exception(f"Failed to start container for {db_type}:{version}")
            raise DockerContainerStartError(f"Failed to start container: {str(e)}")

    def _pull_image_if_not_exists(self, image: str) -> None:
        try:
            self.client.images.get(image)
            logger.debug(f"Image found locally: {image}")
        except NotFound:
            logger.info(f"Pulling Docker image: {image}")
            self.client.images.pull(image)
            logger.info(f"Image pulled: {image}")

    def _get_host_port(self, container: docker.models.containers.Container, container_port: int) -> int:
        """Get the host port mapped to the container port, with retry.

        Docker may take a moment to assign the host port after the container
        starts, especially on Windows. We reload and retry for up to 10 seconds.
        """
        port_key = f"{container_port}/tcp"
        deadline = time.time() + 10
        attempt = 0
        while True:
            try:
                container.reload()
            except Exception:
                pass
            ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
            if port_key in ports and ports[port_key]:
                try:
                    host_port = int(ports[port_key][0]['HostPort'])
                    logger.info(f"Got host port mapping: {container_port}/tcp -> {host_port} (attempt {attempt})")
                    return host_port
                except (ValueError, KeyError, TypeError):
                    pass
            attempt += 1
            if time.time() > deadline:
                logger.error(f"Failed to get host port after {attempt} attempts. Ports: {ports}")
                raise DockerContainerPortError(
                    f"Failed to get host port mapping for {container_port}/tcp. "
                    f"Ports: {ports}"
                )
            time.sleep(0.5)

    def stop_container(self, container_id: str) -> None:
        try:
            container = self.client.containers.get(container_id)
            container.stop()
            logger.info(f"Container stopped: {container_id[:12]}")
        except NotFound:
            logger.warning(f"Container not found when stopping: {container_id[:12]}")
        except DockerException as e:
            logger.error(f"Failed to stop container {container_id[:12]}: {str(e)}")

    def is_port_open(self, host: str, port: int, timeout: int = 1) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                result = sock.connect_ex((host, port))
                return result == 0
        except Exception:
            return False

    def wait_for_port(self, host: str, port: int, max_wait: int = 120, interval: int = 2) -> bool:
        start_time = time.time()
        while time.time() - start_time < max_wait:
            if self.is_port_open(host, port):
                return True
            time.sleep(interval)
        return False

    # db_type -> known compatibility modes (matches normalized values used in container names)
    _COMPAT_MODES = {
        'vastbase': ['A', 'B', 'C', 'PG', 'MSSQL'],
        'kingbase': ['oracle', 'mysql', 'pg', 'sqlserver'],
    }

    def _check_container_status(self, container_name: str, port: int) -> dict:
        """Check a single container by name, return status dict or None if not found."""
        try:
            container = self.client.containers.get(container_name)
            item = {
                "container_name": container_name,
                "container_running": container.status == "running",
                "port_reachable": False,
            }
            if item["container_running"]:
                try:
                    host_port = self._get_host_port(container, port)
                    item["host_port"] = host_port
                    item["port_reachable"] = self.is_port_open("localhost", host_port)
                except DockerContainerPortError:
                    pass
            return item
        except NotFound:
            return None
        except DockerException:
            return None

    def get_containers_status(self, databases_config: dict) -> list[dict]:
        """Return container and port status for all configured database types and versions,
        including compatibility mode variants."""
        result = []
        for db_type, db_cfg in databases_config.items():
            versions = db_cfg.get("versions", {})
            compat_modes = self._COMPAT_MODES.get(db_type, [])
            for version, ver_cfg in versions.items():
                base_name = f"db-mcp-{db_type.lower()}-{version.lower()}"
                names_to_check = [base_name]
                for mode in compat_modes:
                    names_to_check.append(f"{base_name}-{mode.lower()}")

                for container_name in names_to_check:
                    status = self._check_container_status(container_name, ver_cfg["port"])
                    if status is not None:
                        result.append({
                            "db_type": db_type,
                            "version": version,
                            **status,
                        })
                    # For base name (no compat mode), always report even if container missing
                    elif container_name == names_to_check[0]:
                        result.append({
                            "db_type": db_type,
                            "version": version,
                            "container_name": container_name,
                            "container_running": False,
                            "port_reachable": False,
                        })
        return result
