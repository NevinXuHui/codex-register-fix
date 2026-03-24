#!/bin/bash

set -e

echo "==================================="
echo "Codex Register Fix - 启动脚本"
echo "==================================="

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "错误: 未找到虚拟环境，请先运行 ./install.sh"
    exit 1
fi

# 激活虚拟环境
echo "激活虚拟环境..."
source .venv/bin/activate

# 检查依赖
if ! python -c "import fastapi" 2>/dev/null; then
    echo "错误: 依赖未安装，请先运行 ./install.sh"
    exit 1
fi

# 创建必要的目录
mkdir -p data logs

# 解析命令行参数
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
DEBUG="${DEBUG:-0}"

while [[ $# -gt 0 ]]; do
    case $1 in
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
        --password)
            export WEBUI_ACCESS_PASSWORD="$2"
            shift 2
            ;;
        --help)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  --host HOST          监听主机 (默认: 0.0.0.0)"
            echo "  --port PORT          监听端口 (默认: 8000)"
            echo "  --debug              启用调试模式"
            echo "  --password PASSWORD  设置访问密码"
            echo "  --help               显示此帮助信息"
            echo ""
            echo "环境变量:"
            echo "  HOST                 监听主机"
            echo "  PORT                 监听端口"
            echo "  DEBUG                调试模式 (0/1)"
            echo "  WEBUI_ACCESS_PASSWORD 访问密码"
            exit 0
            ;;
        *)
            echo "未知选项: $1"
            echo "使用 --help 查看帮助"
            exit 1
            ;;
    esac
done

# 导出环境变量
export WEBUI_HOST="$HOST"
export WEBUI_PORT="$PORT"
export DEBUG="$DEBUG"

echo ""
echo "启动配置:"
echo "  主机: $HOST"
echo "  端口: $PORT"
echo "  调试: $DEBUG"
echo ""

# 启动服务
echo "启动服务..."
python webui.py --host "$HOST" --port "$PORT" $([ "$DEBUG" = "1" ] && echo "--debug")
