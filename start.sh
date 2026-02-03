#!/bin/bash
set -e

echo "====== [1] 检查配置文件 ======"
if [ ! -f "/app/iptv_config.json" ]; then
    echo "创建默认配置文件..."
    cat > /app/iptv_config.json << 'EOF'
{}
}
EOF
fi

echo "====== [2] 首次生成 IPTV.txt ======"
python /app/iptv.py

echo "====== [3] 启动 Flask 服务 ======"
exec python /app/server.py
