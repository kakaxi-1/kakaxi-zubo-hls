import os
import time
from collections import defaultdict
from watchdog.events import FileSystemEventHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IPTV_FILE = os.path.join(BASE_DIR, "IPTV.txt")


def load_iptv():
    data = defaultdict(set)

    if not os.path.exists(IPTV_FILE):
        return data

    with open(IPTV_FILE, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if "#genre#" in line:
                continue
            if "更新时间" in line:
                continue

            if "," in line:
                channel_name, url = line.split(",", 1)
                if "kakaxi.indevs.in/LOGO/Disclaimer.mp4" not in url and "LOGO/Disclaimer.mp4" not in url:
                    data[channel_name].add(url)

    return data


class IPTVWatcher(FileSystemEventHandler):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.iptv_file = IPTV_FILE

    def on_modified(self, event):
        src_path = event.src_path
        iptv_file_path = os.path.abspath(self.iptv_file)
        
        if os.path.abspath(src_path) == iptv_file_path:
            self._process_iptv_update()

    def _process_iptv_update(self):
        time.sleep(2)
        
        try:
            raw_data = load_iptv()
            new_data = {}
            for channel_name, url_set in raw_data.items():
                new_data[channel_name] = list(url_set)
            
            if self.manager:
                if hasattr(self.manager, 'reload'):
                    self.manager.reload(new_data)
        except Exception:
            pass