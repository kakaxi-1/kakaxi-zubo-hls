#!/bin/bash
set -e

export TZ=Asia/Shanghai

ln -snf /usr/share/zoneinfo/$TZ /etc/localtime
echo $TZ > /etc/timezone

echo "====== [1] 开始配置，准备采集 ======"

CONFIG_FILE="/app/config/iptv_config.json" 

if [ -d "$CONFIG_FILE" ]; then
    rm -rf "$CONFIG_FILE"
fi

if [ ! -f "$CONFIG_FILE" ]; then
    echo "{}" > "$CONFIG_FILE"
fi

if [ -f "$CONFIG_FILE" ]; then
    if python3 -m json.tool "$CONFIG_FILE" > /dev/null 2>&1; then
        :
    else
        echo "{}" > "$CONFIG_FILE"
    fi
fi

echo "====== [2] 开始采集，耐心等待 ======"
cd /app && python iptv.py

echo "====== [3] 执行完毕，启动服务 ======"
exec python server.py
