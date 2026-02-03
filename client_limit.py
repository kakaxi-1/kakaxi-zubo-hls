import time
import threading
from collections import defaultdict

MAX_CHANNELS_PER_IP = 2
IP_IDLE_TIMEOUT = 20

class ClientLimiter:
    def __init__(self):
        self.ip_channels = defaultdict(dict)
        self.lock = threading.Lock()

    def allow(self, ip, channel):
        now = time.time()
        with self.lock:
            chs = self.ip_channels[ip]

            for ch in list(chs.keys()):
                if now - chs[ch] > IP_IDLE_TIMEOUT:
                    del chs[ch]

            if channel not in chs and len(chs) >= MAX_CHANNELS_PER_IP:
                return False

            chs[channel] = now
            return True

    def touch(self, ip, channel):
        with self.lock:
            if channel in self.ip_channels[ip]:
                self.ip_channels[ip][channel] = time.time()