#!/bin/bash
set -e

echo "====== [1] 开始配置，准备采集 ======"

if [ ! -f "$CONFIG_FILE" ] || ! python3 -m json.tool "$CONFIG_FILE" > /dev/null 2>&1; then
    echo "创建或重置配置文件..."
    echo "{}" > "$CONFIG_FILE"
fi

echo "====== [2] 开始采集，耐心等待 ======"
cd /app && python iptv.py

echo "====== [3] 执行完毕，启动服务 ======"
exec python server.py
