## License
This project is released for non-commercial use only.
Commercial use is strictly prohibited.

## Docker Compose
services:
  iptv-server:
    image: kakaxi088/zubo:latest
    container_name: zubo
    restart: unless-stopped
    ports:
      - "5020:5020"
    volumes:
      - ./config:/app/config
      - /etc/localtime:/etc/localtime:ro
    environment:
      - PORT=5020
      - CONFIG_FILE=/app/config/iptv_config.json
      - TZ=Asia/Shanghai

http://localhost:5020

http://localhost:5020/zubo.txt
