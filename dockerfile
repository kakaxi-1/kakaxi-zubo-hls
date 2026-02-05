FROM python:3.9-slim-bullseye

ENV TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    tzdata \
    curl && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    dpkg-reconfigure -f noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

RUN echo "容器时区设置：" && \
    date && \
    cat /etc/timezone && \
    ls -la /etc/localtime

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN echo '{"categories": {}, "mapping": {}, "third_party_urls": {}, "settings": {}}' > /app/iptv_config_default.json
RUN echo '{}' > /app/iptv_config.json

RUN mkdir -p /app/logs /app/hls /app/ip /app/rtp /app/web

RUN chmod +x /app/start.sh

EXPOSE 5020

ENV HLS_ROOT=/app/hls
ENV PORT=5020

CMD ["/app/start.sh"]