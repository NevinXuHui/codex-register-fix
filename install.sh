#!/bin/bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

SERVICE_NAME="codex-register-fix"
SERVICE_USER="${SUDO_USER:-${USER:-$(id -un)}}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
DEBUG="${DEBUG:-0}"
INSTALL_SYSTEMD=0
ENABLE_SYSTEMD=0
START_SYSTEMD=0

print_help() {
    echo "用法: $0 [选项]"
    echo ""
    echo "通用选项:"
    echo "  --host HOST            Web UI 监听主机 (默认: 0.0.0.0)"
    echo "  --port PORT            Web UI 监听端口 (默认: 8000)"
    echo "  --debug                启用调试模式"
    echo "  --help                 显示此帮助信息"
    echo ""
    echo "systemd 选项:"
    echo "  --systemd              安装 systemd 服务单元"
    echo "  --enable               安装后执行 systemctl enable"
    echo "  --start                安装后执行 systemctl restart"
    echo "  --service-name NAME    systemd 服务名 (默认: codex-register-fix)"
    echo "  --service-user USER    systemd 运行用户 (默认: 当前用户或 SUDO_USER)"
    echo ""
    echo "示例:"
    echo "  ./install.sh"
    echo "  ./install.sh --systemd --enable --start"
    echo "  ./install.sh --systemd --service-name codex-register --service-user deploy --host 127.0.0.1 --port 9000"
}

require_systemd_privilege() {
    if [ "$EUID" -eq 0 ]; then
        SYSTEMD_RUN=()
        return
    fi

    if command -v sudo >/dev/null 2>&1; then
        SYSTEMD_RUN=(sudo)
        return
    fi

    echo "错误: 安装 systemd 服务需要 root 权限或可用的 sudo"
    exit 1
}

install_systemd_service() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "错误: 当前系统未检测到 systemd/systemctl，无法安装 systemd 服务"
        exit 1
    fi

    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        echo "错误: systemd 运行用户不存在: $SERVICE_USER"
        exit 1
    fi

    require_systemd_privilege

    local service_group
    local service_file
    local debug_arg=""

    service_group="$(id -gn "$SERVICE_USER")"
    service_file="/etc/systemd/system/${SERVICE_NAME}.service"

    if [ "$DEBUG" = "1" ]; then
        debug_arg=" --debug"
    fi

    echo "安装 systemd 服务: ${SERVICE_NAME}.service"

    "${SYSTEMD_RUN[@]}" tee "$service_file" >/dev/null <<EOF
[Unit]
Description=Codex Register Fix Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$service_group
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
Environment=WEBUI_HOST=$HOST
Environment=WEBUI_PORT=$PORT
Environment=DEBUG=$DEBUG
EnvironmentFile=-$PROJECT_DIR/.env
ExecStart="$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/webui.py" --host "$HOST" --port "$PORT"$debug_arg
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    "${SYSTEMD_RUN[@]}" systemctl daemon-reload

    if [ "$ENABLE_SYSTEMD" = "1" ]; then
        echo "启用 systemd 服务..."
        "${SYSTEMD_RUN[@]}" systemctl enable "$SERVICE_NAME"
    fi

    if [ "$START_SYSTEMD" = "1" ]; then
        echo "启动 systemd 服务..."
        "${SYSTEMD_RUN[@]}" systemctl restart "$SERVICE_NAME"
    fi

    echo "✓ systemd 服务安装完成: ${SERVICE_NAME}.service"
    echo "  查看状态: ${SYSTEMD_RUN[*]:+${SYSTEMD_RUN[*]} }systemctl status $SERVICE_NAME"
    echo "  启动服务: ${SYSTEMD_RUN[*]:+${SYSTEMD_RUN[*]} }systemctl start $SERVICE_NAME"
    echo "  停止服务: ${SYSTEMD_RUN[*]:+${SYSTEMD_RUN[*]} }systemctl stop $SERVICE_NAME"
    echo "  开机自启: ${SYSTEMD_RUN[*]:+${SYSTEMD_RUN[*]} }systemctl enable $SERVICE_NAME"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --systemd)
            INSTALL_SYSTEMD=1
            shift
            ;;
        --enable)
            ENABLE_SYSTEMD=1
            shift
            ;;
        --start)
            START_SYSTEMD=1
            shift
            ;;
        --service-name)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --service-user)
            SERVICE_USER="$2"
            shift 2
            ;;
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --debug)
            DEBUG=1
            shift
            ;;
        --help)
            print_help
            exit 0
            ;;
        *)
            echo "未知选项: $1"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

echo "==================================="
echo "Codex Register Fix - 安装脚本"
echo "==================================="

# 检查 Python 版本
if ! command -v python3 >/dev/null 2>&1; then
    echo "错误: 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

PYTHON_VERSION="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
REQUIRED_VERSION="3.10"

if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "错误: Python 版本需要 3.10+，当前版本: $PYTHON_VERSION"
    exit 1
fi

echo "✓ Python 版本检查通过: $PYTHON_VERSION"

# 创建虚拟环境
if [ ! -d ".venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv ".venv"
    echo "✓ 虚拟环境创建完成"
else
    echo "✓ 虚拟环境已存在"
fi

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

# 升级 pip
echo "升级 pip..."
"$VENV_PYTHON" -m pip install --upgrade pip

# 安装依赖
echo "安装项目依赖..."
"$VENV_PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt"

# 创建必要的目录
echo "创建数据目录..."
mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/logs"

# 检查 .env 文件
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "复制 .env.example 到 .env..."
        cp ".env.example" ".env"
        echo "⚠ 请编辑 .env 文件配置必要的环境变量"
    else
        echo "⚠ 未找到 .env.example 文件"
    fi
else
    echo "✓ .env 文件已存在"
fi

if [ "$INSTALL_SYSTEMD" = "1" ]; then
    install_systemd_service
fi

echo ""
echo "==================================="
echo "✓ 安装完成！"
echo "==================================="
echo ""

echo "使用方法："
echo "  1. 编辑 .env 文件配置环境变量（如需要）"
echo "  2. 运行 ./run.sh 启动服务"

if [ "$INSTALL_SYSTEMD" = "1" ]; then
    echo "  3. 通过 systemctl 管理服务"
else
    echo "  3. 如需安装为 systemd 服务，可执行: ./install.sh --systemd --enable --start"
fi
