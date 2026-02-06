# kakaxi-zubo-hls

A lightweight IPTV relay and management server.

---

## License

This project is released **for non-commercial use only**.

Commercial use is strictly prohibited, including but not limited to:
- providing paid IPTV services
- selling access, subscriptions, or setup services
- using this project as part of any commercial offering

Any commercial use requires prior written permission from the author.

---


## Docker

```yaml
docker run -d \
  --name zubo \
  --restart unless-stopped \
  -p 5020:5020 \
  -v ./config:/app/config \
  -v /etc/localtime:/etc/localtime:ro \
  -e PORT=5020 \
  -e CONFIG_FILE=/app/config/iptv_config.json \
  -e TZ=Asia/Shanghai \
  kakaxi088/zubo:latest

⸻

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

⸻

Access

After the service is started, you can access:
	•	Web interface:
http://localhost:5020
	•	IPTV playlist:
http://localhost:5020/zubo.txt

⸻

Notes

This project is intended for personal learning and non-commercial use only.
Please ensure your usage complies with local laws and regulations.
