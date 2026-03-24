@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ===================================
echo Codex Register Fix - 启动脚本
echo ===================================

:: 检查虚拟环境
if not exist ".venv" (
    echo 错误: 未找到虚拟环境，请先运行 install.bat
    pause
    exit /b 1
)

:: 激活虚拟环境
echo 激活虚拟环境...
call .venv\Scripts\activate.bat

:: 检查依赖
python -c "import fastapi" 2>nul
if %errorlevel% neq 0 (
    echo 错误: 依赖未安装，请先运行 install.bat
    pause
    exit /b 1
)

:: 创建必要的目录
if not exist "data" mkdir data
if not exist "logs" mkdir logs

:: 默认配置
set HOST=0.0.0.0
set PORT=8000
set DEBUG=0
set EXTRA_ARGS=

:: 解析命令行参数
:parse_args
if "%~1"=="" goto end_parse
if /i "%~1"=="--host" (
    set HOST=%~2
    shift
    shift
    goto parse_args
)
if /i "%~1"=="--port" (
    set PORT=%~2
    shift
    shift
    goto parse_args
)
if /i "%~1"=="--debug" (
    set DEBUG=1
    set EXTRA_ARGS=!EXTRA_ARGS! --debug
    shift
    goto parse_args
)
if /i "%~1"=="--password" (
    set WEBUI_ACCESS_PASSWORD=%~2
    shift
    shift
    goto parse_args
)
if /i "%~1"=="--help" (
    echo 用法: %~nx0 [选项]
    echo.
    echo 选项:
    echo   --host HOST          监听主机 ^(默认: 0.0.0.0^)
    echo   --port PORT          监听端口 ^(默认: 8000^)
    echo   --debug              启用调试模式
    echo   --password PASSWORD  设置访问密码
    echo   --help               显示此帮助信息
    echo.
    echo 环境变量:
    echo   HOST                 监听主机
    echo   PORT                 监听端口
    echo   DEBUG                调试模式 ^(0/1^)
    echo   WEBUI_ACCESS_PASSWORD 访问密码
    pause
    exit /b 0
)
shift
goto parse_args

:end_parse

:: 导出环境变量
set WEBUI_HOST=%HOST%
set WEBUI_PORT=%PORT%
set DEBUG=%DEBUG%

echo.
echo 启动配置:
echo   主机: %HOST%
echo   端口: %PORT%
echo   调试: %DEBUG%
echo.

:: 启动服务
echo 启动服务...
python webui.py --host %HOST% --port %PORT% %EXTRA_ARGS%

if %errorlevel% neq 0 (
    echo.
    echo 服务启动失败
    pause
    exit /b 1
)
