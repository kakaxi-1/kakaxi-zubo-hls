FROM alpine:3.18 AS ffmpeg-builder

ARG TARGETARCH

RUN apk add --no-cache wget xz && \
    if [ "$TARGETARCH" = "amd64" ]; then \
        wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
        tar xf ffmpeg-release-amd64-static.tar.xz && \
        mv ffmpeg-*-static/ffmpeg /tmp/ffmpeg; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz && \
        tar xf ffmpeg-release-arm64-static.tar.xz && \
        mv ffmpeg-*-static/ffmpeg /tmp/ffmpeg; \
    fi && \
    chmod +x /tmp/ffmpeg


FROM python:3.9-slim-bullseye

ENV TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

COPY --from=ffmpeg-builder /tmp/ffmpeg /usr/local/bin/ffmpeg

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    rm -rf /root/.cache/pip

COPY . .

RUN mkdir -p /app/logs /app/hls /app/ip /app/rtp /app/web /app/config && \
    chown -R 1000:1000 /app && \
    chmod -R 755 /app

RUN find /app -name "*.pyc" -delete && \
    find /app -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

EXPOSE 5020

ENV HLS_ROOT=/app/hls
ENV PORT=5020
ENV CONFIG_FILE=/app/config/iptv_config.json

USER 1000:1000

CMD ["/app/start.sh"]
