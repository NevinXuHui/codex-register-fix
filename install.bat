@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ===================================
echo Codex Register Fix - 安装脚本
echo ===================================

:: 检查 Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo 错误: 未找到 python，请先安装 Python 3.10+
    exit /b 1
)

:: 检查 Python 版本
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo ✓ Python 版本: %PYTHON_VERSION%

:: 创建虚拟环境
if not exist ".venv" (
    echo 创建虚拟环境...
    python -m venv .venv
    echo ✓ 虚拟环境创建完成
) else (
    echo ✓ 虚拟环境已存在
)

:: 激活虚拟环境
echo 激活虚拟环境...
call .venv\Scripts\activate.bat

:: 升级 pip
echo 升级 pip...
python -m pip install --upgrade pip

:: 安装依赖
echo 安装项目依赖...
pip install -r requirements.txt

:: 创建必要的目录
echo 创建数据目录...
if not exist "data" mkdir data
if not exist "logs" mkdir logs

:: 检查 .env 文件
if not exist ".env" (
    if exist ".env.example" (
        echo 复制 .env.example 到 .env...
        copy .env.example .env >nul
        echo ⚠ 请编辑 .env 文件配置必要的环境变量
    ) else (
        echo ⚠ 未找到 .env.example 文件
    )
) else (
    echo ✓ .env 文件已存在
)

echo.
echo ===================================
echo ✓ 安装完成！
echo ===================================
echo.
echo 使用方法：
echo   1. 编辑 .env 文件配置环境变量（如需要）
echo   2. 运行 run.bat 启动服务
echo.

pause
