@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================================
REM DB-MCP Server 服务管理脚本 (Windows)
REM
REM 用法:
REM   service.bat install     安装为 Windows 服务（使用 nssm）
REM   service.bat uninstall   卸载 Windows 服务
REM   service.bat start       启动服务（守护进程模式）
REM   service.bat stop        停止服务
REM   service.bat restart     重启服务
REM   service.bat status      查看服务状态
REM   service.bat foreground  前台运行（调试用）
REM
REM 环境变量（可选）:
REM   MCP_HOST    监听地址，默认 0.0.0.0
REM   MCP_PORT    监听端口，默认 8000
REM   LOG_DIR     日志目录，默认 .\logs\
REM ============================================================================

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not defined MCP_HOST set "MCP_HOST=0.0.0.0"
if not defined MCP_PORT set "MCP_PORT=8000"
if not defined LOG_DIR  set "LOG_DIR=%SCRIPT_DIR%logs"

set "PID_FILE=%LOG_DIR%\mcp-server.pid"
set "SERVICE_NAME=DB-MCP-Server"

REM ---- 检查 Python ----
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 未找到 python，请安装 Python 3.10+ 并添加到 PATH
    exit /b 1
)

REM ---- 安装依赖（如果 venv 不存在）----
if not exist "venv\Scripts\python.exe" (
    echo [INFO] 创建虚拟环境...
    python -m venv venv
    echo [INFO] 安装依赖...
    call venv\Scripts\activate.bat
    pip install -r requirements.txt -q
) else (
    call venv\Scripts\activate.bat
)

REM ---- 服务名称（用于 nssm）----
set "PYTHON_EXE=%SCRIPT_DIR%venv\Scripts\python.exe"

goto :%1 2>nul || goto :usage

:install
    where nssm >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] 未找到 nssm.exe
        echo   下载: https://nssm.cc/download
        echo   或使用 Chocolatey: choco install nssm
        exit /b 1
    )
    nssm stop "%SERVICE_NAME%" >nul 2>&1
    nssm install "%SERVICE_NAME%" "%PYTHON_EXE%" "-m" "src.main" "--host" "%MCP_HOST%" "--port" "%MCP_PORT%" "--log-dir" "%LOG_DIR%"
    nssm set "%SERVICE_NAME%" AppDirectory "%SCRIPT_DIR%"
    nssm set "%SERVICE_NAME%" DisplayName "DB-MCP Server"
    nssm set "%SERVICE_NAME%" Description "Database SQL execution service for Dify MCP"
    nssm set "%SERVICE_NAME%" Start SERVICE_AUTO_START
    nssm set "%SERVICE_NAME%" AppStdout "%LOG_DIR%\stdout.log"
    nssm set "%SERVICE_NAME%" AppStderr "%LOG_DIR%\stderr.log"
    nssm set "%SERVICE_NAME%" AppRotateFiles 1
    nssm set "%SERVICE_NAME%" AppRotateBytes 10485760
    echo [INFO] 服务已安装: %SERVICE_NAME%
    echo   启动: nssm start %SERVICE_NAME%
    echo   或直接运行: service.bat start
    goto :eof

:uninstall
    nssm stop "%SERVICE_NAME%" >nul 2>&1
    nssm remove "%SERVICE_NAME%" confirm
    echo [INFO] 服务已卸载: %SERVICE_NAME%
    goto :eof

:start
    call :do_status >nul 2>&1
    if !errorlevel! equ 0 (
        echo [WARN] 服务已在运行
        goto :eof
    )
    echo [INFO] 启动 DB-MCP Server (%MCP_HOST%:%MCP_PORT%) ...
    python -m src.main --daemon --host "%MCP_HOST%" --port "%MCP_PORT%" --log-dir "%LOG_DIR%"
    timeout /t 3 /nobreak >nul
    call :do_status
    goto :eof

:stop
    call :do_status >nul 2>&1
    if !errorlevel! neq 0 (
        echo [INFO] 服务未在运行
        goto :eof
    )
    echo [INFO] 停止服务...
    python -m src.main --stop --log-dir "%LOG_DIR%"
    goto :eof

:restart
    call :stop
    timeout /t 2 /nobreak >nul
    call :start
    goto :eof

:status
    call :do_status
    goto :eof

:foreground
    echo [INFO] 前台启动 DB-MCP Server (%MCP_HOST%:%MCP_PORT%) ...
    echo [INFO] 日志输出到控制台，Ctrl+C 停止
    python -m src.main --host "%MCP_HOST%" --port "%MCP_PORT%" --log-dir "%LOG_DIR%"
    goto :eof

:do_status
    if not exist "%PID_FILE%" (
        echo 状态: 未运行 ^(无 PID 文件^)
        exit /b 1
    )
    set /p PID=<"%PID_FILE%"
    if "%PID%"=="" (
        echo 状态: 未运行 ^(PID 文件为空^)
        exit /b 1
    )
    tasklist /fi "PID eq %PID%" 2>nul | findstr "%PID%" >nul
    if %errorlevel% equ 0 (
        echo 状态: 运行中 ^(PID: %PID%, 端口: %MCP_PORT%^)
        exit /b 0
    ) else (
        echo 状态: 未运行 ^(PID %PID% 不存在^)
        del "%PID_FILE%" 2>nul
        exit /b 1
    )

:usage
    echo 用法: %~nx0 {install^|uninstall^|start^|stop^|restart^|status^|foreground}
    echo.
    echo   install      安装为 Windows 服务（需先安装 nssm）
    echo   uninstall    卸载 Windows 服务
    echo   start        后台启动服务
    echo   stop         停止服务
    echo   restart      重启服务
    echo   status       查看服务状态
    echo   foreground   前台运行（调试用，Ctrl+C 停止）
    echo.
    echo 环境变量:
    echo   MCP_HOST=%MCP_HOST%
    echo   MCP_PORT=%MCP_PORT%
    echo   LOG_DIR=%LOG_DIR%
    exit /b 1
