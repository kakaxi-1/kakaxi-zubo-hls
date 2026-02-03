#!/bin/bash
set -e

echo "====== [0] 检查配置文件 ======"
if [ ! -f "/app/iptv_config.json" ]; then
    echo "创建默认配置文件..."
    cat > /app/iptv_config.json << 'EOF'
{
  "categories": {},
  "mapping": {},
  "third_party_urls": {},
  "settings": {
    "RUN_INTERVAL_HOURS": 2,
    "FFMPEG_MAX_DETECT_TIME": 40,
    "FFMPEG_TEST_DURATION": 15,
    "RESPONSE_TIME_THRESHOLD": 10,
    "STREAM_STABLE_THRESHOLD": 0.5
  }
}
EOF
fi

echo "====== [1] 首次生成 IPTV.txt ======"
python /app/iptv.py

echo "====== [2] 启动 Flask 服务 ======"
exec python /app/server.py