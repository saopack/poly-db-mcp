#!/usr/bin/env bash
# ============================================================================
# DB-MCP Server 服务管理脚本
#
# 用法:
#   ./service.sh start          启动服务（守护进程模式）
#   ./service.sh stop           停止服务
#   ./service.sh restart        重启服务
#   ./service.sh status         查看服务状态
#   ./service.sh foreground     前台运行（调试用）
#
# 环境变量（可选）:
#   MCP_HOST    监听地址，默认 0.0.0.0
#   MCP_PORT    监听端口，默认 8000
#   LOG_DIR     日志目录，默认 ./logs/
#   VENV_DIR    虚拟环境目录，默认 ./venv/
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${MCP_HOST:-0.0.0.0}"
PORT="${MCP_PORT:-8000}"
LOG_DIR="${LOG_DIR:-$SCRIPT_DIR/logs}"
PID_FILE="$LOG_DIR/mcp-server.pid"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/venv}"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- 激活虚拟环境（如果存在）----
activate_venv() {
    if [ -f "$VENV_DIR/Scripts/activate" ]; then
        # Windows (Git Bash)
        source "$VENV_DIR/Scripts/activate"
    elif [ -f "$VENV_DIR/bin/activate" ]; then
        # Linux / macOS / WSL
        source "$VENV_DIR/bin/activate"
    fi
}

# ---- 安装依赖（首次运行）----
install_deps() {
    if [ ! -f "$VENV_DIR/Scripts/activate" ] && [ ! -f "$VENV_DIR/bin/activate" ]; then
        log_info "创建虚拟环境: $VENV_DIR"
        python -m venv "$VENV_DIR"
    fi
    activate_venv
    if ! python -c "import fastapi" 2>/dev/null; then
        log_info "安装依赖..."
        pip install -r requirements.txt -q
    fi
}

# ---- 检查 Python ----
check_python() {
    if ! command -v python &>/dev/null; then
        log_error "未找到 python，请安装 Python 3.10+"
        exit 1
    fi
}

# ---- status ----
cmd_status() {
    if [ ! -f "$PID_FILE" ]; then
        echo "状态: 未运行 (无 PID 文件)"
        return 1
    fi

    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -z "$pid" ]; then
        echo "状态: 未运行 (PID 文件为空)"
        return 1
    fi

    if kill -0 "$pid" 2>/dev/null; then
        echo "状态: 运行中 (PID: $pid, 端口: $PORT)"
        return 0
    else
        echo "状态: 未运行 (PID $pid 不存在，清理过期 PID 文件)"
        rm -f "$PID_FILE"
        return 1
    fi
}

# ---- start ----
cmd_start() {
    if cmd_status &>/dev/null; then
        log_warn "服务已在运行"
        return 0
    fi

    check_python
    install_deps

    log_info "启动 DB-MCP Server (${HOST}:${PORT}) ..."
    python -m src.main --daemon --host "$HOST" --port "$PORT" --log-dir "$LOG_DIR"

    sleep 2
    if cmd_status &>/dev/null; then
        log_info "启动成功"
    else
        log_error "启动失败，查看日志: $LOG_DIR/mcp-server.log"
        return 1
    fi
}

# ---- stop ----
cmd_stop() {
    if ! cmd_status &>/dev/null; then
        log_info "服务未在运行"
        return 0
    fi

    log_info "停止服务..."
    check_python
    activate_venv
    python -m src.main --stop --log-dir "$LOG_DIR"

    # 等 10s
    for _ in $(seq 1 20); do
        if ! cmd_status &>/dev/null; then
            log_info "已停止"
            return 0
        fi
        sleep 0.5
    done
    log_warn "停止超时，手动清理 PID"
    rm -f "$PID_FILE"
}

# ---- restart ----
cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

# ---- foreground ----
cmd_foreground() {
    check_python
    install_deps
    log_info "前台启动 DB-MCP Server (${HOST}:${PORT}) ..."
    log_info "日志输出到控制台，Ctrl+C 停止"
    python -m src.main --host "$HOST" --port "$PORT" --log-dir "$LOG_DIR"
}

# ---- main ----
case "${1:-}" in
    start)      cmd_start ;;
    stop)       cmd_stop ;;
    restart)    cmd_restart ;;
    status)     cmd_status ;;
    foreground) cmd_foreground ;;
    *)
        echo "用法: $0 {start|stop|restart|status|foreground}"
        echo ""
        echo "  start       后台启动服务"
        echo "  stop        停止服务"
        echo "  restart     重启服务"
        echo "  status      查看服务状态"
        echo "  foreground  前台运行（调试用，Ctrl+C 停止）"
        echo ""
        echo "环境变量:"
        echo "  MCP_HOST=${HOST}"
        echo "  MCP_PORT=${PORT}"
        echo "  LOG_DIR=${LOG_DIR}"
        exit 1
        ;;
esac
