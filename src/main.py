import os
import sys
import signal
import logging
import argparse
import subprocess
import time
from logging.handlers import RotatingFileHandler

import uvicorn


PID_FILE_NAME = "mcp-server.pid"


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


def configure_logging(log_dir: str = None):
    """
    配置日志：同时输出到控制台和文件。

    Args:
        log_dir: 日志目录路径，默认从环境变量 LOG_DIR 读取，回退到 ./logs/
    """
    if log_dir is None:
        log_dir = os.environ.get("LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))

    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, "mcp-server.log")

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

    # 文件 handler（10MB 滚动，保留 5 个备份）
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    logging.info(f"Log file: {log_file}")


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


def run_daemon(host: str, port: int, log_dir: str):
    """
    后台启动：spawn 一个独立的子进程运行服务，父进程退出。

    支持 Windows 和 Unix。
    """
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if sys.platform == "win32":
        # Windows: 使用 CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            [sys.executable, "-m", "src.main", "--host", host, "--port", str(port), "--log-dir", log_dir],
            cwd=script_dir,
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        # Unix: 标准后台启动
        proc = subprocess.Popen(
            [sys.executable, "-m", "src.main", "--host", host, "--port", str(port), "--log-dir", log_dir],
            cwd=script_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    _write_pid(log_dir, proc.pid)
    print(f"Server started in background (PID: {proc.pid})")
    print(f"  Host: {host}:{port}")
    print(f"  Logs: {os.path.join(log_dir, 'mcp-server.log')}")
    print(f"  Stop: python -m src.main --stop")


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


def restart_server(host: str, port: int, log_dir: str) -> bool:
    """重启服务：先停止，再启动。"""
    pid = _read_pid(log_dir)
    if pid is not None and _is_process_running(pid):
        print("Stopping current server...")
        if not stop_server(log_dir):
            print("Failed to stop server, aborting restart.")
            return False

    print("Starting server...")
    run_daemon(host, port, log_dir)
    return True


def main():
    parser = argparse.ArgumentParser(description="MCP Database Execution Tool")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"), help="监听地址")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8000")), help="监听端口")
    parser.add_argument("--daemon", "-d", action="store_true", help="后台启动（守护进程模式）")
    parser.add_argument("--stop", action="store_true", help="停止后台运行的服务")
    parser.add_argument("--restart", action="store_true", help="重启后台运行的服务")
    parser.add_argument("--log-dir", default=None, help="日志目录，默认 ./logs/")
    args = parser.parse_args()

    log_dir = args.log_dir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")

    if args.stop:
        success = stop_server(log_dir)
        sys.exit(0 if success else 1)
    elif args.restart:
        success = restart_server(args.host, args.port, log_dir)
        sys.exit(0 if success else 1)
    elif args.daemon:
        # 后台模式：先初始化日志目录，再 spawn 子进程
        os.makedirs(log_dir, exist_ok=True)
        run_daemon(args.host, args.port, log_dir)
    else:
        configure_logging(log_dir)
        logging.info(f"Starting server on {args.host}:{args.port}")
        run_server(args.host, args.port, log_dir)


if __name__ == "__main__":
    main()
