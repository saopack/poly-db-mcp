import os
import sys
import gzip
import glob
import signal
import logging
import argparse
import subprocess
import time
from logging.handlers import TimedRotatingFileHandler

import uvicorn


PID_FILE_NAME = "mcp-server.pid"
LOG_FILE_NAME = "mcp-server.log"

# Role-aware file names: avoid Gateway and Node stepping on each other
_ROLE_SUFFIX = ""


def _set_role(role: str) -> None:
    global _ROLE_SUFFIX, PID_FILE_NAME, LOG_FILE_NAME
    _ROLE_SUFFIX = f"-{role}"
    PID_FILE_NAME = f"mcp-{role}.pid"
    LOG_FILE_NAME = f"mcp-{role}.log"


def _pid_file_path(log_dir: str) -> str:
    return os.path.join(log_dir, PID_FILE_NAME)


def _read_pid(log_dir: str) -> int | None:
    path = _pid_file_path(log_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            pid = int(f.read().strip())
        return pid
    except (ValueError, OSError):
        return None


def _write_pid(log_dir: str, pid: int) -> None:
    os.makedirs(log_dir, exist_ok=True)
    with open(_pid_file_path(log_dir), "w") as f:
        f.write(str(pid))


def _remove_pid(log_dir: str) -> None:
    path = _pid_file_path(log_dir)
    if os.path.exists(path):
        os.remove(path)


def _is_process_running(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, ProcessLookupError):
        return False


def _kill_process(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def _rotator(source: str, dest: str) -> None:
    """Compress old log file to *dest* (which already includes .gz from _namer)."""
    try:
        with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
            f_out.writelines(f_in)
        os.remove(source)
    except OSError:
        pass


def _namer(name: str) -> str:
    """Append .gz extension for TimedRotatingFileHandler namer."""
    return name + (".gz" if not name.endswith(".gz") else "")


def _cleanup_old_logs(log_dir: str, base_name: str, retention_days: int) -> int:
    """Remove rotated log files older than *retention_days*.

    Returns the number of files deleted.
    """
    now = time.time()
    cutoff = now - retention_days * 86400
    deleted = 0
    # Match rotated files:  base_name.YYYY-MM-DD  or  base_name.YYYY-MM-DD.gz
    pattern = os.path.join(log_dir, base_name + ".[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*")
    for path in glob.glob(pattern):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                deleted += 1
                logger = logging.getLogger(__name__)
                logger.info("Cleaned up old log: %s", os.path.basename(path))
        except OSError:
            pass
    return deleted


def configure_logging(log_dir: str | None = None, retention_days: int = 30):
    """
    配置日志：同时输出到控制台和文件（按天切分，自动压缩归档）。

    Args:
        log_dir: 日志目录路径，默认从环境变量 LOG_DIR 读取，回退到 ./logs/
        retention_days: 归档日志保留天数，默认 30 天。设为 0 表示不清理。
    """
    if log_dir is None:
        log_dir = os.environ.get("LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))

    os.makedirs(log_dir, exist_ok=True)

    # 清理过期日志（在创建新 handler 之前，避免误删当前文件）
    _cleanup_old_logs(log_dir, LOG_FILE_NAME, retention_days)

    log_file = os.path.join(log_dir, LOG_FILE_NAME)

    # 根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 文件 handler — 按天切分（每天午夜滚动），滚动时自动 gzip 压缩
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=retention_days,  # TimedRotatingFileHandler 按文件数量保留
        encoding="utf-8",
        utc=False,
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    # 设置压缩回调（Python 3.3+）
    file_handler.rotator = _rotator
    file_handler.namer = _namer
    root_logger.addHandler(file_handler)

    logging.info(f"Log file: {log_file}  (daily rotation, {retention_days}d retention)")


def run_server(host: str, port: int, log_dir: str | None = None):
    """启动 uvicorn 服务（前台）"""
    if log_dir:
        _write_pid(log_dir, os.getpid())
    try:
        uvicorn.run(
            "src.api:app",
            host=host,
            port=port,
            reload=False,
            log_config=None,  # 使用我们自己的 logging 配置
        )
    finally:
        if log_dir:
            _remove_pid(log_dir)


def run_gateway_server(host: str, port: int, log_dir: str | None = None):
    """启动 Gateway uvicorn 服务（前台）"""
    if log_dir:
        _write_pid(log_dir, os.getpid())
    try:
        uvicorn.run(
            "src.gateway.app:create_gateway_app",
            host=host,
            port=port,
            reload=False,
            log_config=None,
            factory=True,
        )
    finally:
        if log_dir:
            _remove_pid(log_dir)


def run_daemon(host: str, port: int, log_dir: str, role: str = "node"):
    """
    后台启动：spawn 一个独立的子进程运行服务，父进程退出。

    支持 Windows 和 Unix。
    """
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    args = [sys.executable, "-m", "src.main", "--host", host, "--port", str(port),
            "--log-dir", log_dir, "--role", role]

    if sys.platform == "win32":
        # Windows: 使用 CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            args,
            cwd=script_dir,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        # Unix: 标准后台启动
        proc = subprocess.Popen(
            args,
            cwd=script_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    _write_pid(log_dir, proc.pid)
    role_label = "Gateway" if role == "gateway" else "Node"
    print(f"{role_label} started in background (PID: {proc.pid})")
    print(f"  Role: {role}")
    print(f"  Host: {host}:{port}")
    print(f"  PID:  {os.path.join(log_dir, PID_FILE_NAME)}")
    print(f"  Logs: {os.path.join(log_dir, LOG_FILE_NAME)}")
    print(f"  Stop: python -m src.main --role {role} --stop")


def stop_server(log_dir: str) -> bool:
    """停止后台运行的服务。返回 True 表示成功停止。"""
    pid = _read_pid(log_dir)
    if pid is None:
        print(f"No PID file found ({_pid_file_path(log_dir)}). Server may not be running.")
        return False

    if not _is_process_running(pid):
        print(f"PID {pid} is not running. Removing stale PID file.")
        _remove_pid(log_dir)
        return False

    print(f"Stopping server (PID: {pid})...")
    if _kill_process(pid):
        # 等待进程退出
        deadline = time.time() + 10
        while time.time() < deadline:
            if not _is_process_running(pid):
                _remove_pid(log_dir)
                print("Server stopped.")
                return True
            time.sleep(0.5)
        print(f"Warning: Process {pid} did not exit within 10s")
        return False
    else:
        print(f"Failed to kill process {pid}")
        return False


def restart_server(host: str, port: int, log_dir: str, role: str = "node") -> bool:
    """重启服务：先停止，再启动。"""
    pid = _read_pid(log_dir)
    if pid is not None and _is_process_running(pid):
        print("Stopping current server...")
        if not stop_server(log_dir):
            print("Failed to stop server, aborting restart.")
            return False

    role_label = "Gateway" if role == "gateway" else "Node"
    print(f"Starting {role_label}...")
    run_daemon(host, port, log_dir, role)
    return True


def main():
    parser = argparse.ArgumentParser(description="MCP Database Execution Tool")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"), help="监听地址")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8000")), help="监听端口")
    parser.add_argument("--role", default=os.environ.get("MCP_ROLE", "node"),
                        choices=["node", "gateway"], help="进程角色：node（数据库执行节点）或 gateway（路由代理）")
    parser.add_argument("--daemon", "-d", action="store_true", help="后台启动（守护进程模式）")
    parser.add_argument("--stop", action="store_true", help="停止后台运行的服务")
    parser.add_argument("--restart", action="store_true", help="重启后台运行的服务")
    parser.add_argument("--log-dir", default=None, help="日志目录，默认 ./logs/")
    args = parser.parse_args()

    log_dir = args.log_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")

    _set_role(args.role)

    if args.stop:
        success = stop_server(log_dir)
        sys.exit(0 if success else 1)
    elif args.restart:
        success = restart_server(args.host, args.port, log_dir, args.role)
        sys.exit(0 if success else 1)
    elif args.daemon:
        # 后台模式：先初始化日志目录，再 spawn 子进程
        os.makedirs(log_dir, exist_ok=True)
        run_daemon(args.host, args.port, log_dir, args.role)
    else:
        configure_logging(log_dir)
        if args.role == "gateway":
            logging.info(f"Starting Gateway on {args.host}:{args.port}")
            run_gateway_server(args.host, args.port, log_dir)
        else:
            logging.info(f"Starting Node server on {args.host}:{args.port}")
            run_server(args.host, args.port, log_dir)


if __name__ == "__main__":
    main()
