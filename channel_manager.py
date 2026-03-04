import os
import subprocess
import threading
import time
import glob
import shutil
import requests
import urllib3
import queue
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import List, Tuple, Optional, Any

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HLS_ROOT = os.getenv("HLS_ROOT", "hls")

ACTIVE_WINDOW = 8
MIN_UPTIME = 13
CHANNEL_CHECK_INTERVAL = 3

RACE_CONCURRENCY = 10
RACE_FIRST_CHUNK_SIZE = 188 * 700
RACE_CONNECT_TIMEOUT = (2.0, 3.0)
RACE_SWITCH_COOLDOWN = 3

_global_executor = None

class GlobalThreadPool:
    _instance = None
    _lock = threading.RLock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    self.max_workers = 20
                    self.executor = None
                    self._initialize()
                    self._initialized = True
    
    def _initialize(self):
        try:
            self.executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="GlobalWorker"
            )
        except Exception:
            self.executor = None
    
    def submit(self, func, *args, **kwargs):
        if self.executor is None:
            self._initialize()
        
        try:
            return self.executor.submit(func, *args, **kwargs)
        except Exception:
            self._initialize()
            if self.executor:
                return self.executor.submit(func, *args, **kwargs)
            raise
    
    def shutdown(self, wait=True):
        if self.executor:
            try:
                self.executor.shutdown(wait=wait)
            except Exception:
                pass
            finally:
                self.executor = None
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def submit_task(cls, func, *args, **kwargs):
        instance = cls.get_instance()
        return instance.submit(func, *args, **kwargs)
    
    @classmethod
    def shutdown_all(cls):
        if cls._instance:
            cls._instance.shutdown()
    
    @classmethod
    def shutdown(cls):
        cls.shutdown_all()

def get_global_executor():
    global _global_executor
    if _global_executor is None:
        _global_executor = ThreadPoolExecutor(
            max_workers=20,
            thread_name_prefix="GlobalWorker"
        )
    return _global_executor

def submit_global_task(func, *args, **kwargs):
    executor = get_global_executor()
    return executor.submit(func, *args, **kwargs)

def shutdown_global_pool():
    global _global_executor
    if _global_executor:
        _global_executor.shutdown(wait=True)
        _global_executor = None


class IPActivityManager:
    def __init__(self):
        self.channel_activities = {}
        self.ip_running_channels = defaultdict(set)
        self.lock = threading.RLock()
        self._cleanup_thread = None
        self._running = False
    
    def record_access(self, ip, channel_name):
        current_time = time.time()
        
        with self.lock:
            if channel_name not in self.channel_activities:
                self.channel_activities[channel_name] = {}
            
            self.channel_activities[channel_name][ip] = current_time
    
    def get_active_ips(self, channel_name):
        current_time = time.time()
        active_ips = {}
        
        with self.lock:
            if channel_name in self.channel_activities:
                for ip, last_seen in self.channel_activities[channel_name].items():
                    if current_time - last_seen <= ACTIVE_WINDOW:
                        active_ips[ip] = last_seen
        return active_ips
    
    def is_channel_active(self, channel_name):
        return len(self.get_active_ips(channel_name)) > 0
    
    def can_start_channel(self, ip, channel_name, limit=5):
        with self.lock:
            running = self.ip_running_channels[ip]
            if channel_name in running:
                return True
            return len(running) < limit
    
    def mark_channel_started(self, ip, channel_name):
        with self.lock:
            self.ip_running_channels[ip].add(channel_name)
    
    def mark_channel_stopped(self, ip, channel_name):
        with self.lock:
            if channel_name in self.ip_running_channels[ip]:
                self.ip_running_channels[ip].remove(channel_name)
            if not self.ip_running_channels[ip]:
                del self.ip_running_channels[ip]
    
    def cleanup_expired_ips(self):
        current_time = time.time()
        
        with self.lock:
            for channel_name in list(self.channel_activities.keys()):
                if channel_name in self.channel_activities:
                    expired_ips = []
                    for ip, last_seen in self.channel_activities[channel_name].items():
                        if current_time - last_seen > ACTIVE_WINDOW:
                            expired_ips.append(ip)
                    
                    for ip in expired_ips:
                        del self.channel_activities[channel_name][ip]
                        self.mark_channel_stopped(ip, channel_name)
                    
                    if not self.channel_activities[channel_name]:
                        del self.channel_activities[channel_name]
    
    def start_cleanup_thread(self):
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="IPCleaner"
        )
        self._cleanup_thread.start()
    
    def _cleanup_loop(self):
        while self._running:
            try:
                self.cleanup_expired_ips()
            except Exception:
                pass
            time.sleep(2)
    
    def stop(self):
        self._running = False
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2)


class RaceConnectionResult:
    """竞速连接结果"""
    def __init__(self, response: Optional[requests.Response] = None, 
                 first_chunk: Optional[bytes] = None, 
                 url: str = "", 
                 source_index: int = -1,
                 success: bool = False):
        self.response = response
        self.first_chunk = first_chunk
        self.url = url
        self.source_index = source_index
        self.success = success
        self.connect_time = 0.0
        self.error = None


class Channel:
    STATE_IDLE = "IDLE"
    STATE_STARTING = "STARTING"
    STATE_RUNNING = "RUNNING"
    STATE_STOPPING = "STOPPING"
    STATE_STOPPED = "STOPPED"
    
    def __init__(self, name, sources):
        self.name = name
        self.sources = list(sources)
        self.lock = threading.RLock()
        self.ip_activity_manager = None
        self.proc = None
        
        self.state = self.STATE_IDLE
        self.output_dir = os.path.join(HLS_ROOT, name)
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.stream_thread = None
        self.check_thread = None
        self.check_running = False
        
        self.hls_ready = False
        self.proc_start_time = 0
        self.executor = ThreadPoolExecutor(max_workers=RACE_CONCURRENCY + 2)
        self.current_source_index = 0
        
        self.start_time = 0
        self.last_active_ts = 0
        
        self.data_queue = None
        self.writer_thread = None
        self.writer_running = False
        self.reader_thread = None
        self.reader_running = False
        self.current_response = None
        self.source_lock = threading.RLock()
        self.last_successful_write = 0
        self.consecutive_failures = 0
        self.pipeline_started = False
        self.pipeline_ready = False
        
        self.buffer_size = 10 * 1024 * 1024
        
        self.last_read_time = 0
        self.read_timeout = 3.0
        
        self.bitrate_window_start = 0
        self.bitrate_bytes = 0
        self.min_bitrate = 752 * 1024
        self.max_chunk_gap = 2.0
        self.min_chunk_size = 47 * 1024
        self.low_bitrate_count = 0
        
        self.last_switch_time = 0
        self.race_futures = []
        self.race_in_progress = False
        self.race_lock = threading.RLock()
        self.failed_sources = {}
        self.failed_source_ttl = 45
        
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Connection": "keep-alive",
            "Accept": "*/*"
        })
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=0
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        self.last_touch_time = 0

    def _trigger_race(self):
        """【修改1】统一竞速触发入口，避免重复触发"""
        if not self.race_in_progress:
            submit_global_task(self._race_switch_source)

    def _pipeline_dead(self):
        if not self.pipeline_ready:
            return False
        
        if not self.reader_thread or not self.reader_thread.is_alive():
            return True
        
        if not self.writer_thread or not self.writer_thread.is_alive():
            return True
        
        # 【修改5】放宽判定阈值，从12秒改为18秒，避免误判
        if time.time() - self.last_successful_write > 18.0:
            if self.pipeline_ready:
                return True
        
        try:
            ts_files = glob.glob(os.path.join(self.output_dir, "seg_*.ts"))
            if ts_files:
                latest_ts = max(ts_files, key=os.path.getmtime)
                if time.time() - os.path.getmtime(latest_ts) > 18:
                    return True
        except:
            pass
        
        return False

    def _pipeline_switch_source(self):
        """改进的切源方法，使用竞速机制"""
        if self.state != self.STATE_RUNNING:
            return
        
        current_time = time.time()
        if current_time - self.last_switch_time < RACE_SWITCH_COOLDOWN:
            return
        
        self.last_switch_time = current_time
        
        self._stop_current_reader()
        
        self._trigger_race()

    def _stop_current_reader(self):
        """停止当前的reader"""
        with self.source_lock:
            if self.current_response:
                try:
                    self.current_response.close()
                except:
                    pass
                self.current_response = None
            
            self._cancel_race_futures()

    def _cancel_race_futures(self):
        """取消所有竞速任务"""
        with self.race_lock:
            for future in self.race_futures:
                if not future.done():
                    future.cancel()
            self.race_futures.clear()
            self.race_in_progress = False

    def _get_eligible_sources(self, count: int = RACE_CONCURRENCY) -> List[Tuple[int, str]]:
        """
        获取符合条件的源用于竞速
        返回 [(index, url), ...]
        """
        if not self.sources:
            return []
        
        current_time = time.time()
        eligible_sources = []
        
        expired_failures = [
            idx for idx, ts in self.failed_sources.items() 
            if current_time - ts > self.failed_source_ttl
        ]
        for idx in expired_failures:
            del self.failed_sources[idx]
        
        all_sources = []
        for idx, url in enumerate(self.sources):
            if idx in self.failed_sources:
                continue
            all_sources.append((idx, url))
        
        if not all_sources:
            self.failed_sources.clear()
            all_sources = list(enumerate(self.sources))
        
        start_idx = (self.current_source_index + 1) % len(self.sources)
        
        ordered_sources = []
        for i in range(len(all_sources)):
            idx = (start_idx + i) % len(self.sources)
            for src_idx, src_url in all_sources:
                if src_idx == idx:
                    ordered_sources.append((src_idx, src_url))
                    break
        
        return ordered_sources[:count]

    def _race_connect_worker(self, source_index: int, url: str) -> RaceConnectionResult:
        """竞速连接工作线程 - 【修改4】增强超时控制"""
        result = RaceConnectionResult(
            url=url, 
            source_index=source_index,
            success=False
        )
        start_time = time.time()
        
        try:
            session = requests.Session()
            session.verify = False
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Connection": "keep-alive"
            })
            
            response = session.get(
                url,
                stream=True,
                timeout=RACE_CONNECT_TIMEOUT
            )
            
            if response.status_code == 200:
                # 设置底层socket超时
                try:
                    if hasattr(response.raw, '_fp') and hasattr(response.raw._fp, 'fp'):
                        if hasattr(response.raw._fp.fp, 'raw'):
                            sock = response.raw._fp.fp.raw._sock
                            sock.settimeout(2.0)
                except:
                    pass
                
                # 【修改4】设置decode_content=False，避免自动解码影响超时
                response.raw.decode_content = False
                
                first_chunk = next(response.iter_content(chunk_size=RACE_FIRST_CHUNK_SIZE))
                
                if first_chunk and len(first_chunk) > 0:
                    result.response = response
                    result.first_chunk = first_chunk
                    result.success = True
                    result.connect_time = time.time() - start_time
                else:
                    response.close()
            else:
                response.close()
                
        except Exception as e:
            result.error = str(e)
            result.connect_time = time.time() - start_time
        
        return result

    def _race_switch_source(self):
        """执行竞速切源 - 热切换数据源，不重启FFmpeg"""
        with self.race_lock:
            if self.race_in_progress:
                return
            self.race_in_progress = True
        
        try:
            candidates = self._get_eligible_sources(RACE_CONCURRENCY)
            if not candidates:
                with self.race_lock:
                    self.race_in_progress = False
                return
            
            futures = []
            for idx, url in candidates:
                future = self.executor.submit(self._race_connect_worker, idx, url)
                futures.append(future)
            
            with self.race_lock:
                self.race_futures = futures
            
            winner = None
            completed = 0
            total = len(futures)
            
            while completed < total and not winner:
                for future in as_completed(futures, timeout=5):
                    completed += 1
                    if future.cancelled():
                        continue
                    
                    try:
                        result = future.result(timeout=0.1)
                        if result and result.success:
                            winner = result
                            for f in futures:
                                if f is not future and not f.done():
                                    f.cancel()
                            break
                    except:
                        continue
                    
                if winner:
                    break
            
            if winner:
                with self.source_lock:
                    # 1. 关掉旧的 HTTP 响应，释放连接
                    if self.current_response:
                        try:
                            self.current_response.close()
                        except:
                            pass
                    
                    # 2. 切换新源
                    self.current_response = winner.response
                    self.current_source_index = winner.source_index
                    
                    # 3. 【修改2】安全清空队列，不设上限
                    if self.data_queue:
                        # 完全清空队列，避免旧数据残留
                        while not self.data_queue.empty():
                            try:
                                self.data_queue.get_nowait()
                            except queue.Empty:
                                break
                            except:
                                break
                    
                    # 4. 将第一块数据放入队列
                    if winner.first_chunk and self.data_queue:
                        try:
                            self.data_queue.put(winner.first_chunk, block=False)
                        except Exception:
                            pass
                    
                    self.last_read_time = time.time()
                    self.bitrate_window_start = time.time()
                    self.bitrate_bytes = len(winner.first_chunk) if winner.first_chunk else 0
                    self.consecutive_failures = 0
                    
                    if winner.source_index in self.failed_sources:
                        del self.failed_sources[winner.source_index]
                    
            else:
                for idx, _ in candidates:
                    self.failed_sources[idx] = time.time()
                
        except Exception as e:
            pass
        
        finally:
            with self.race_lock:
                self.race_in_progress = False
                self.race_futures.clear()

    def set_ip_activity_manager(self, manager):
        with self.lock:
            self.ip_activity_manager = manager
    
    def start_check_thread(self):
        if self.check_thread is None or not self.check_thread.is_alive():
            self.check_running = True
            self.check_thread = threading.Thread(
                target=self._check_loop,
                daemon=True,
                name=f"Check-{self.name}"
            )
            self.check_thread.start()
    
    def _check_loop(self):
        while self.check_running:
            try:
                self._check_and_manage()
            except Exception:
                pass
            time.sleep(CHANNEL_CHECK_INTERVAL)
    
    def _check_and_manage(self):
        current_time = time.time()
        
        with self.lock:
            should_stop = False
            
            if self.state in [self.STATE_RUNNING, self.STATE_STARTING]:
                has_active_ips = False
                if self.ip_activity_manager:
                    has_active_ips = self.ip_activity_manager.is_channel_active(self.name)
                
                if not has_active_ips:
                    inactive_time = current_time - self.last_active_ts if self.last_active_ts > 0 else float('inf')
                    uptime = current_time - self.start_time if self.start_time > 0 else 0
                    
                    if inactive_time >= ACTIVE_WINDOW and uptime >= MIN_UPTIME:
                        should_stop = True
                
                if self.reader_running:
                    if current_time - self.last_read_time > self.read_timeout:
                        self._trigger_race()
                        self.last_read_time = current_time
            
            if should_stop:
                if self.ip_activity_manager:
                    active_ips = self.ip_activity_manager.get_active_ips(self.name)
                    for ip in active_ips.keys():
                        self.ip_activity_manager.mark_channel_stopped(ip, self.name)
                self._safe_stop_stream()
            elif self.state in [self.STATE_IDLE, self.STATE_STOPPED]:
                if self.ip_activity_manager and self.ip_activity_manager.is_channel_active(self.name):
                    self._start_stream()
    
    def _safe_stop_stream(self):
        if self.state == self.STATE_STOPPED:
            return
        
        old_state = self.state
        self.state = self.STATE_STOPPING
        
        if self.ip_activity_manager:
            active_ips = self.ip_activity_manager.get_active_ips(self.name)
            for ip in active_ips.keys():
                self.ip_activity_manager.mark_channel_stopped(ip, self.name)
        
        self._cancel_race_futures()
        
        self._stop_data_pipeline()
        self._kill_ffmpeg()
        
        if old_state in [self.STATE_RUNNING, self.STATE_STARTING]:
            self._clean_hls_immediate()
        
        self.state = self.STATE_STOPPED
        self.start_time = 0
        self.last_active_ts = 0
        self.hls_ready = False
        self.pipeline_started = False
        self.pipeline_ready = False
        self.last_read_time = 0
    
    def touch(self):
        current_time = time.time()
        
        with self.lock:
            self.last_touch_time = current_time
            self.last_active_ts = current_time
            
            if self.state == self.STATE_RUNNING:
                if self._check_hls_ready():
                    return True
                else:
                    return self._wait_for_hls_ready(timeout=5)
            
            if self.state in [self.STATE_IDLE, self.STATE_STOPPED]:
                self._start_stream()
                return self._wait_for_hls_ready(timeout=15)
            
            return False
    
    def _wait_for_hls_ready(self, timeout=15):
        start_time = time.time()
        check_interval = 0.5
        
        while time.time() - start_time < timeout:
            if self._check_hls_ready():
                self.hls_ready = True
                return True
            time.sleep(check_interval)
        
        return False
    
    def _check_hls_ready(self):
        if self.state != self.STATE_RUNNING: 
            return False
        if self.proc is None or self.proc.poll() is not None: 
            return False
        
        m3u8_path = os.path.join(self.output_dir, "index.m3u8")
        if not os.path.exists(m3u8_path): 
            return False
        
        try:
            ts_files = glob.glob(os.path.join(self.output_dir, "seg_*.ts"))
            if len(ts_files) < 1: 
                return False
            
            latest_ts = max(ts_files, key=os.path.getmtime)
            if os.path.getsize(latest_ts) < 5 * 1024: 
                return False
            
            return True
        except:
            return False

    def _connect_worker(self, url):
        try:
            r = self.session.get(
                url,
                stream=True,
                timeout=(3, 5),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Connection": "keep-alive"
                }
            )
            if r.status_code == 200:
                first_chunk = next(r.iter_content(chunk_size=65536))
                return (r, first_chunk, url)
            else:
                r.close()
                return None
        except Exception:
            return None

    def _race_connect(self, candidate_urls):
        if not candidate_urls:
            return None
        
        futures = []
        winner = None
        
        for url in candidate_urls:
            f = self.executor.submit(self._connect_worker, url)
            futures.append(f)
        
        try:
            for f in as_completed(futures):
                result = f.result()
                if result:
                    winner = result
                    break 
        except Exception:
            pass

        return winner

    def _get_next_source_index(self):
        if not self.sources:
            return 0
        self.current_source_index = (self.current_source_index + 1) % len(self.sources)
        return self.current_source_index

    def _start_data_pipeline(self):
        if self.pipeline_started:
            return
        
        self.last_successful_write = time.time()
        
        self.pipeline_started = True
        self.pipeline_ready = False
        
        self.data_queue = queue.Queue(maxsize=300)
        self.writer_running = True
        self.writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name=f"Writer-{self.name}"
        )
        self.writer_thread.start()
        
        self.reader_running = True
        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"Reader-{self.name}"
        )
        self.reader_thread.start()
        
        self._trigger_race()
    
    def _stop_data_pipeline(self):
        if not self.pipeline_started:
            return
            
        self.reader_running = False
        
        with self.source_lock:
            if self.current_response:
                try:
                    self.current_response.close()
                except:
                    pass
                self.current_response = None
        
        self.writer_running = False
        
        if self.data_queue:
            try:
                while not self.data_queue.empty():
                    try:
                        self.data_queue.get_nowait()
                    except:
                        break
            except:
                pass
        
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=2)
        
        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=2)
        
        self.pipeline_started = False
        self.pipeline_ready = False
        self.last_read_time = 0
    
    def _writer_loop(self):
        """【修改3】writer永不退出，失败超过阈值后重置计数器"""
        while self.writer_running:
            try:
                try:
                    data = self.data_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                
                # 检查FFmpeg进程状态，如果崩溃则尝试重启（加锁）
                if self.proc is None or self.proc.poll() is not None:
                    if self.writer_running:
                        with self.lock:
                            if (self.proc is None or self.proc.poll() is not None) and self.writer_running:
                                self._start_ffmpeg(clean_old_files=False)
                                time.sleep(0.2)
                        continue
                
                if self._safe_write_to_ffmpeg(data):
                    self.last_successful_write = time.time()
                    self.consecutive_failures = 0
                    
                    if not self.pipeline_ready:
                        self.pipeline_ready = True
                else:
                    self.consecutive_failures += 1
                    # 【修改3】不退出线程，只是重置计数器并继续
                    if self.consecutive_failures > 3:
                        self.consecutive_failures = 0
                        continue
                
            except Exception:
                time.sleep(0.05)
        
        self.writer_running = False
    
    def _connect_source(self, url):
        try:
            response = self.session.get(
                url,
                stream=True,
                timeout=(1.5, 2.5)
            )

            if response.status_code == 200:
                try:
                    sock = response.raw._fp.fp.raw._sock
                    sock.settimeout(1.0)
                except:
                    pass
                return response
            else:
                response.close()
                return None

        except Exception:
            return None
    
    def _reader_loop(self):
        """直接使用current_response，不再重复连接"""
        while self.reader_running:
            try:
                response_to_use = None
                with self.source_lock:
                    if not self.sources:
                        time.sleep(0.5)
                        continue
                    
                    if not self.current_response:
                        if not self.race_in_progress:
                            self._trigger_race()
                        time.sleep(0.1)
                        continue
                    
                    response_to_use = self.current_response

                self.last_read_time = time.time()
                self.bitrate_window_start = time.time()
                self.bitrate_bytes = 0

                for chunk in response_to_use.iter_content(chunk_size=188 * 174):
                    if not self.reader_running:
                        break
                        
                    if not chunk:
                        if self.reader_running:
                            self._trigger_race()
                        break

                    now = time.time()
                    
                    if now - self.last_read_time > self.max_chunk_gap:
                        if self.reader_running:
                            self._trigger_race()
                        break
                    
                    self.bitrate_bytes += len(chunk)
                    if now - self.bitrate_window_start >= 3.0:
                        bitrate = self.bitrate_bytes / 3.0
                        if bitrate < self.min_bitrate:
                            if self.low_bitrate_count >= 1:
                                if self.reader_running:
                                    self._trigger_race()
                                self.low_bitrate_count = 0
                                break
                            else:
                                self.low_bitrate_count += 1
                        else:
                            self.low_bitrate_count = 0
                        self.bitrate_window_start = now
                        self.bitrate_bytes = 0

                    self.last_read_time = now

                    if self.data_queue.full():
                        try:
                            self.data_queue.get_nowait()
                        except:
                            pass

                    try:
                        self.data_queue.put(chunk, block=False)
                    except:
                        pass
                
                if self.reader_running:
                    self._trigger_race()

            except Exception:
                if self.reader_running:
                    self._trigger_race()
                time.sleep(0.1)

        self.reader_running = False
    
    def _reader_switch_source(self):
        if not self.reader_running:
            return
        
        self._trigger_race()
    
    def _safe_write_to_ffmpeg(self, data):
        if not self.proc or self.proc.poll() is not None:
            return False
        
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False
        except Exception:
            return False

    def _start_stream(self):
        if self.state in [self.STATE_STARTING, self.STATE_RUNNING]:
            return
        
        self.state = self.STATE_STARTING
        self.start_time = time.time()
        self.last_active_ts = time.time()
        self.hls_ready = False
        self.last_read_time = time.time()
        
        if self.stream_thread is None or not self.stream_thread.is_alive():
            self.stream_thread = threading.Thread(
                target=self._streaming_loop,
                daemon=True,
                name=f"Stream-{self.name}"
            )
            self.stream_thread.start()
    
    def _streaming_loop(self):
        self.state = self.STATE_STARTING
        
        if not self.sources:
            self.state = self.STATE_STOPPED
            return
        
        if not self._start_ffmpeg(clean_old_files=True):
            self.state = self.STATE_STOPPED
            return
        
        self._start_data_pipeline()
        self.state = self.STATE_RUNNING
        
        try:
            ffmpeg_dead_count = 0
            while self.state == self.STATE_RUNNING:
                if self.proc is None or self.proc.poll() is not None:
                    ffmpeg_dead_count += 1
                    if ffmpeg_dead_count > 4:
                        break
                    time.sleep(0.5)
                    if self.state == self.STATE_RUNNING:
                        with self.lock:
                            if self.proc is None or self.proc.poll() is not None:
                                self._start_ffmpeg(clean_old_files=False)
                    continue
                else:
                    ffmpeg_dead_count = 0
                
                if self._pipeline_dead():
                    if not self.race_in_progress:
                        self._trigger_race()
                    time.sleep(0.5)
                    continue
                
                time.sleep(0.5)
                
        except Exception:
            pass
        
        finally:
            if self.state not in [self.STATE_STOPPING, self.STATE_STOPPED]:
                self.state = self.STATE_STOPPING
                
                self._stop_data_pipeline()
                self._kill_ffmpeg()
                
                self.state = self.STATE_STOPPED
                self.start_time = 0
                self.last_active_ts = 0
                self.last_read_time = 0
    
    def _kill_ffmpeg(self):
        with self.lock:
            if not self.proc:
                return
            
            try:
                if self.proc.stdin:
                    try:
                        self.proc.stdin.close()
                    except:
                        pass
                
                time.sleep(0.1)
                
                self.proc.terminate()
                
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=2)
                    except:
                        pass
                
            except Exception:
                pass
            finally:
                self.proc = None
    
    def _start_ffmpeg(self, clean_old_files=True):
        with self.lock:
            os.makedirs(self.output_dir, exist_ok=True)
            
            if clean_old_files:
                self._clean_old_ts_files()
            
            start_number = int(time.time() * 100) % 1000000

            hls_flags = "delete_segments+split_by_time+independent_segments+omit_endlist"
            if not clean_old_files and os.path.exists(os.path.join(self.output_dir, "index.m3u8")):
                hls_flags += "+append_list"
            
            cmd = [
                "ffmpeg",
                "-loglevel", "warning",
                "-fflags", "nobuffer+genpts+discardcorrupt+igndts+flush_packets",
                "-flags", "low_delay",
                "-err_detect", "ignore_err",
                "-f", "mpegts", 
                "-i", "pipe:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "128k",
                "-ac", "2",
                "-ar", "44100",
                "-f", "hls",
                "-hls_time", "2",
                "-hls_list_size", "6",
                "-hls_flags", hls_flags,
                "-start_number", str(start_number),
                "-hls_segment_filename",
                os.path.join(self.output_dir, "seg_%06d.ts"),
                os.path.join(self.output_dir, "index.m3u8")
            ]
            
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=1048576
                )
                self.proc_start_time = time.time()
                return True
            except Exception:
                self.proc = None
                return False
    
    def _clean_old_ts_files(self):
        try:
            for file in glob.glob(os.path.join(self.output_dir, "seg_*.ts")):
                try:
                    os.remove(file)
                except:
                    pass
            m3u8_file = os.path.join(self.output_dir, "index.m3u8")
            if os.path.exists(m3u8_file):
                os.remove(m3u8_file)
        except:
            pass

    def stop_check_thread(self):
        self.check_running = False
        if self.check_thread and self.check_thread.is_alive():
            self.check_thread.join(timeout=2)
        self.check_thread = None
    
    def cleanup(self):
        if self.ip_activity_manager:
            active_ips = self.ip_activity_manager.get_active_ips(self.name)
            for ip in active_ips.keys():
                self.ip_activity_manager.mark_channel_stopped(ip, self.name)
        
        self.stop_check_thread()
        
        self._cancel_race_futures()
        
        self._stop_data_pipeline()
        self._kill_ffmpeg()
        
        self.state = self.STATE_STOPPED
        self.start_time = 0
        self.last_active_ts = 0
        
        try:
            self.session.close()
        except:
            pass
        
        time.sleep(1)
        self._clean_hls_immediate()
        
        try:
            self.executor.shutdown(wait=False)
        except:
            pass
    
    def _clean_hls_immediate(self):
        if not os.path.exists(self.output_dir):
            return
        
        try:
            for file in os.listdir(self.output_dir):
                file_path = os.path.join(self.output_dir, file)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path, ignore_errors=True)
                except:
                    pass
            
            try:
                if not os.listdir(self.output_dir):
                    os.rmdir(self.output_dir)
            except:
                pass
        except:
            pass
