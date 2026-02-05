import os
import subprocess
import threading
import time
import glob
import shutil
import requests
import urllib3
import queue
import re
import io
import select
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HLS_ROOT = os.getenv("HLS_ROOT", "hls")

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
        except Exception as e:
            self.executor = None
    
    def submit(self, func, *args, **kwargs):
        if self.executor is None:
            self._initialize()
        
        try:
            return self.executor.submit(func, *args, **kwargs)
        except Exception as e:
            self._initialize()
            if self.executor:
                return self.executor.submit(func, *args, **kwargs)
            raise
    
    def shutdown(self, wait=True):
        if self.executor:
            try:
                self.executor.shutdown(wait=wait)
            except Exception as e:
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

class Channel:
    STATE_IDLE = "IDLE"
    STATE_STARTING = "STARTING"
    STATE_RUNNING = "RUNNING"
    STATE_STOPPING = "STOPPING"
    STATE_STOPPED = "STOPPED"
    
    def __init__(self, name, sources):
        self.name = name
        self.sources = list(sources)
        self.lock = threading.Lock()
        self.active_checker = None
        self.proc = None
        
        self.state = self.STATE_IDLE
        self.output_dir = os.path.join(HLS_ROOT, name)
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.stream_thread = None
        self.check_thread = None
        self.check_running = False
        self.check_interval = 5
        self.cleanup_timer = None
        self.cleanup_delay = 20
        self.session_id = None
        self.last_touch_time = 0
        
        self.hls_ready = False
        self.hls_ready_time = 0
        self.proc_start_time = 0
        self.race_concurrency = 3
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.current_source_index = 0
        
        self.data_queue = None
        self.writer_thread = None
        self.writer_running = False
        self.reader_thread = None
        self.reader_running = False
        self.current_response = None
        self.source_lock = threading.Lock()
        self.last_successful_write = 0
        self.consecutive_failures = 0
        self.pipeline_started = False
        self.pipeline_ready = False
        
        self.buffer_size = 10 * 1024 * 1024
        self.max_retry_delay = 5.0
        self.min_retry_delay = 0.1
        
        self.last_log_time = 0
        self.log_lock = threading.Lock()
        self.ffmpeg_log_thread = None
        self.ffmpeg_log_running = False
        
        self.bad_packet_count = 0
        self.max_bad_packets = 10
        self.consecutive_empty_chunks = 0
        self.max_consecutive_empty = 5
        
        self.stream_stats = {'video': 0, 'audio': 0, 'other': 0}
        self.last_stats_reset = time.time()
        self.stats_reset_interval = 30
        
        self.last_chunk_time = 0
        self.slow_chunk_count = 0
        self.max_slow_chunks = 10
        
        self.reader_start_time = 0
        self.reader_warmup_data = 0
        self.reader_warmup_chunks = 0
        self.warmup_time = 5.0
        self.warmup_data = 300 * 1024
        
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
            max_retries=3
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def _pipeline_dead(self):
        if not self.pipeline_ready:
            return False
        
        if not self.reader_thread or not self.reader_thread.is_alive():
            return True
        
        if not self.writer_thread or not self.writer_thread.is_alive():
            return True
        
        if time.time() - self.last_successful_write > 5.0:
            return True
        
        return False

    def set_active_checker(self, checker):
        with self.lock:
            self.active_checker = checker
            if checker:
                self._start_check_thread()
    
    def _start_check_thread(self):
        if self.active_checker is None:
            return
        
        if self.check_thread is None or not self.check_thread.is_alive():
            self.check_running = True
            self.check_thread = threading.Thread(
                target=self._check_loop,
                daemon=True,
                name=f"Check-{self.name}"
            )
            self.check_thread.start()
    
    def _check_loop(self):
        last_check_time = 0
        
        while self.check_running:
            current_time = time.time()
            need_check = False
            
            with self.lock:
                if self.state == self.STATE_RUNNING and current_time - self.last_touch_time > 30:
                    need_check = True
            
            if not need_check and (current_time - last_check_time >= self.check_interval):
                need_check = True
            
            if need_check:
                try:
                    self._safe_check_and_manage()
                    last_check_time = current_time
                except Exception as e:
                    pass
            
            time.sleep(self.check_interval)
    
    def _safe_check_and_manage(self):
        if not self.active_checker: return
        
        should_be_active = self.active_checker.is_active(self.name)
        
        with self.lock:
            if self.state == self.STATE_RUNNING and should_be_active:
                if not self._check_hls_files_exist():
                    self._stop_stream()
                    return
                if not self._check_ts_size():
                    self._stop_stream()
                    return
            
            if should_be_active and self.state in [self.STATE_IDLE, self.STATE_STOPPED]:
                self._start_stream()
            elif not should_be_active and self.state == self.STATE_RUNNING:
                self._stop_stream_with_delay()
    
    def _stop_stream_with_delay(self):
        def delayed_stop():
            time.sleep(3)
            with self.lock:
                if self.state == self.STATE_RUNNING and self.active_checker and not self.active_checker.is_active(self.name):
                    self._stop_stream()
                    time.sleep(2)
                    self._clean_hls_if_inactive()
        
        threading.Thread(target=delayed_stop, daemon=True).start()
    
    def _clean_hls_if_inactive(self):
        if self.state == self.STATE_STOPPED and self.active_checker and not self.active_checker.is_active(self.name):
            self._clean_hls_immediate()
    
    def _check_hls_files_exist(self):
        m3u8_path = os.path.join(self.output_dir, "index.m3u8")
        if not os.path.exists(m3u8_path):
            return False
        
        try:
            ts_files = glob.glob(os.path.join(self.output_dir, "seg_*.ts"))
            return len(ts_files) > 0
        except:
            return False
    
    def stop_check_thread(self):
        self.check_running = False
        if self.check_thread and self.check_thread.is_alive():
            self.check_thread.join(timeout=2)
        self.check_thread = None
    
    def touch(self):
        with self.lock:
            self.last_touch_time = time.time()
            
            if self.state == self.STATE_RUNNING:
                if self._check_hls_ready():
                    return True
                else:
                    return self._wait_for_hls_ready(timeout=5)
            
            if self.active_checker and self.active_checker.is_active(self.name):
                self._start_stream()
                return self._wait_for_hls_ready(timeout=15)
            
            return False
    
    def _wait_for_hls_ready(self, timeout=15):
        start_time = time.time()
        check_interval = 0.5
        
        while time.time() - start_time < timeout:
            if self._check_hls_ready():
                self.hls_ready = True
                self.hls_ready_time = time.time()
                return True
            time.sleep(check_interval)
        
        return False
    
    def _check_hls_ready(self):
        if self.state != self.STATE_RUNNING: return False
        if self.proc is None or self.proc.poll() is not None: return False
        
        m3u8_path = os.path.join(self.output_dir, "index.m3u8")
        if not os.path.exists(m3u8_path): return False
        
        try:
            ts_files = glob.glob(os.path.join(self.output_dir, "seg_*.ts"))
            if len(ts_files) < 1: return False
            
            latest_ts = max(ts_files, key=os.path.getmtime)
            if os.path.getsize(latest_ts) < 10 * 1024: return False
            
            return True
        except:
            return False

    def _connect_worker(self, url):
        try:
            r = self.session.get(
                url,
                stream=True,
                timeout=5,
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
        except Exception as e:
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
        self.pipeline_start_time = time.time() 
        
        self.pipeline_started = True
        self.pipeline_ready = False
        
        self.data_queue = queue.Queue(maxsize=150)
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
    
    def _writer_loop(self):
        empty_cycle_count = 0
        last_success_time = time.time()
        has_successful_write = False
        
        while self.writer_running:
            try:
                try:
                    data = self.data_queue.get(timeout=0.1)
                    empty_cycle_count = 0
                except queue.Empty:
                    empty_cycle_count += 1
                    if empty_cycle_count >= 5 and self._should_keep_alive():
                        data = self._generate_keepalive_data()
                        empty_cycle_count = 0
                    else:
                        continue
                
                if self._safe_write_to_ffmpeg(data):
                    last_success_time = time.time()
                    self.last_successful_write = last_success_time
                    self.consecutive_failures = 0
                    
                    if not has_successful_write:
                        has_successful_write = True
                        self.pipeline_ready = True
                else:
                    self.consecutive_failures += 1
                    if self.consecutive_failures > 3:
                        break
                
            except Exception as e:
                time.sleep(0.05)
        
        self.writer_running = False
    
    def _reader_loop(self):
        retry_delay = self.min_retry_delay
        has_received_data = False
        
        while self.reader_running:
            try:
                with self.source_lock:
                    if self.current_source_index >= len(self.sources):
                        self.current_source_index = 0
                    source_url = self.sources[self.current_source_index]
                
                self._reset_quality_counters()
                
                response = self._connect_source(source_url)
                if not response:
                    with self.source_lock:
                        self.current_source_index = (self.current_source_index + 1) % len(self.sources)
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 1.5, self.max_retry_delay)
                    continue
                
                retry_delay = self.min_retry_delay
                
                self.reader_start_time = time.time()
                self.reader_warmup_data = 0
                self.reader_warmup_chunks = 0
                
                with self.source_lock:
                    if self.current_response:
                        try:
                            self.current_response.close()
                        except:
                            pass
                    self.current_response = response
                
                try:
                    successful_chunks = 0
                    received_any_data = False
                    
                    for chunk in response.iter_content(chunk_size=65536):
                        if not self.reader_running:
                            break
                        
                        received_any_data = True
                        
                        if not has_received_data:
                            has_received_data = True
                        
                        if chunk:
                            successful_chunks += 1
                            
                            if successful_chunks % 10 == 0:
                                retry_delay = self.min_retry_delay
                            
                            quality_check = self._check_data_quality(chunk)
                            if not quality_check["valid"]:
                                if (self.slow_chunk_count >= self.max_slow_chunks or
                                    self.bad_packet_count >= self.max_bad_packets or
                                    self.consecutive_empty_chunks >= self.max_consecutive_empty):
                                    
                                    self._reader_switch_source()
                                    break
                            
                            try:
                                if self.data_queue.full():
                                    try:
                                        self.data_queue.get_nowait()
                                    except:
                                        pass
                                self.data_queue.put(chunk, block=False)
                            except queue.Full:
                                pass
                        else:
                            self.consecutive_empty_chunks += 1
                            if self.consecutive_empty_chunks >= self.max_consecutive_empty:
                                self._reader_switch_source()
                                break
                    
                    if received_any_data:
                        self._reader_switch_source()
                    else:
                        with self.source_lock:
                            self.current_source_index = (self.current_source_index + 1) % len(self.sources)
                            
                except Exception as e:
                    self._reader_switch_source()
                
                finally:
                    with self.source_lock:
                        if self.current_response == response:
                            try:
                                self.current_response.close()
                            except:
                                pass
                            self.current_response = None
                
                time.sleep(0.5)
                
            except Exception as e:
                with self.source_lock:
                    self.current_source_index = (self.current_source_index + 1) % len(self.sources)
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, self.max_retry_delay)
        
        self.reader_running = False
    
    def _check_data_quality(self, chunk):
        result = {"valid": True, "reason": ""}
        chunk_len = len(chunk)
        current_time = time.time()
        
        if not hasattr(self, 'reader_start_time'):
            self.reader_start_time = current_time
            self.reader_warmup_data = 0
            self.reader_warmup_chunks = 0
        
        self.reader_warmup_data += chunk_len
        self.reader_warmup_chunks += 1
        
        in_warmup = (current_time - self.reader_start_time < self.warmup_time and 
                     self.reader_warmup_data < self.warmup_data)
        
        if in_warmup:
            if chunk_len == 0:
                result["valid"] = False
                result["reason"] = "热身期收到空数据"
                return result
            
            if self.last_chunk_time > 0:
                time_gap = current_time - self.last_chunk_time
                if time_gap > 10.0:
                    pass
            
            self.last_chunk_time = current_time
            return result
        
        if chunk_len < 10:
            self.bad_packet_count += 1
            if self.bad_packet_count >= self.max_bad_packets:
                result["valid"] = False
                result["reason"] = f"坏包过多: {self.bad_packet_count}/{self.max_bad_packets}"
            return result
        
        if chunk_len >= 100:
            self.bad_packet_count = max(0, self.bad_packet_count - 1)
        
        if self.last_chunk_time > 0:
            time_gap = current_time - self.last_chunk_time
            
            if time_gap > 3.0:
                self.slow_chunk_count += 1
                if self.slow_chunk_count >= self.max_slow_chunks:
                    result["valid"] = False
                    result["reason"] = f"数据速率持续过慢: {self.slow_chunk_count}/{self.max_slow_chunks}"
                    return result
            else:
                self.slow_chunk_count = max(0, self.slow_chunk_count - 1)
        
        self.last_chunk_time = current_time
        
        return result
    
    def _reset_quality_counters(self):
        self.slow_chunk_count = 0
        self.last_chunk_time = 0
        self.bad_packet_count = 0
        self.consecutive_empty_chunks = 0
        
        if hasattr(self, 'reader_start_time'):
            del self.reader_start_time
        if hasattr(self, 'reader_warmup_data'):
            del self.reader_warmup_data
        if hasattr(self, 'reader_warmup_chunks'):
            del self.reader_warmup_chunks
    
    def _reader_switch_source(self):
        if not self.reader_running:
            return
        
        with self.source_lock:
            if self.current_response:
                try:
                    self.current_response.close()
                except:
                    pass
                self.current_response = None
            
            if self.sources:
                self.current_source_index = (self.current_source_index + 1) % len(self.sources)
        
        self._reset_quality_counters()
        self.consecutive_failures = 0
        
        time.sleep(0.5)
    
    def _connect_source(self, url):
        try:
            response = self.session.get(
                url,
                stream=True,
                timeout=5
            )
            
            if response.status_code == 200:
                return response
            else:
                response.close()
                return None
                
        except Exception as e:
            return None
    
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
    
    def _generate_keepalive_data(self):
        ts_packet = b'\x47' + b'\x00' * 187
        return ts_packet
    
    def _should_keep_alive(self):
        if not self.proc or self.proc.poll() is not None:
            return False
        
        if time.time() - self.last_successful_write > 2.0:
            return True
        
        return False
    
    def _restart_data_pipeline(self):
        if self.state != self.STATE_STARTING:
            return False
        
        self._stop_data_pipeline()
        time.sleep(0.5)
        self._start_data_pipeline()
        return True

    def _start_stream(self):
        if self.state in [self.STATE_STARTING, self.STATE_RUNNING]:
            return
        
        self.state = self.STATE_STARTING
        self.session_id = f"{int(time.time())}_{id(self)}"
        self.hls_ready = False
        
        if self.cleanup_timer:
            self.cleanup_timer.cancel()
            self.cleanup_timer = None
        
        if self.stream_thread is None or not self.stream_thread.is_alive():
            self.stream_thread = threading.Thread(
                target=self._streaming_loop,
                daemon=True,
                name=f"Stream-{self.name}"
            )
            self.stream_thread.start()
    
    def _stop_stream(self):
        if self.state == self.STATE_STOPPED:
            return
        
        old_state = self.state
        self.state = self.STATE_STOPPING
        
        self.stop_check_thread()
        
        self._stop_data_pipeline()
        
        self._stop_ffmpeg_log_thread()
        
        self._kill_ffmpeg()
        
        if old_state in [self.STATE_RUNNING, self.STATE_STARTING]:
            self._clean_hls_immediate()
        
        self.state = self.STATE_STOPPED
        self.session_id = None
        self.last_touch_time = 0
        self.hls_ready = False
        self.pipeline_started = False
        self.pipeline_ready = False

    def _stop_ffmpeg_log_thread(self):
        self.ffmpeg_log_running = False
        if self.ffmpeg_log_thread and self.ffmpeg_log_thread.is_alive():
            self.ffmpeg_log_thread.join(timeout=1)
        self.ffmpeg_log_thread = None

    def _streaming_loop(self):
        self.state = self.STATE_STARTING
        current_session = self.session_id
        
        if not self.sources:
            self.state = self.STATE_STOPPED
            return
        
        if not self._start_ffmpeg(clean_old_files=True):
            self.state = self.STATE_STOPPED
            return
        
        self._start_data_pipeline()
        self.state = self.STATE_RUNNING
        
        try:
            while self.state == self.STATE_RUNNING and self.session_id == current_session:
                if self.proc is None or self.proc.poll() is not None:
                    break
                
                if self._pipeline_dead():
                    break
                
                time.sleep(0.5)
                
        except Exception:
            pass
        
        finally:
            if self.state not in [self.STATE_STOPPING, self.STATE_STOPPED]:
                self.state = self.STATE_STOPPING
                
                self._stop_data_pipeline()
                
                self._stop_ffmpeg_log_thread()
                
                self._kill_ffmpeg()
                
                self.state = self.STATE_STOPPED
    
    def _delayed_clean_hls(self):
        try:
            if self.active_checker and self.active_checker.is_active(self.name):
                return
            self._clean_hls_immediate()
        except Exception:
            pass
        finally:
            self.cleanup_timer = None
    
    def cleanup(self):
        self.stop_check_thread()
        self._stop_data_pipeline()
        self._stop_ffmpeg_log_thread()
        self._kill_ffmpeg()
        
        self.state = self.STATE_STOPPED
        
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
    
    def _clean_hls_safe(self):
        if not os.path.exists(self.output_dir):
            return
        
        if self.active_checker and self.active_checker.is_active(self.name):
            return
        
        try:
            if self.state == self.STATE_RUNNING:
                return
                
            m3u8_path = os.path.join(self.output_dir, "index.m3u8")
            if os.path.exists(m3u8_path):
                mtime = os.path.getmtime(m3u8_path)
                current_time = time.time()
                
                if mtime > current_time:
                    return
                    
                if abs(current_time - mtime) < 30:
                    return
        except:
            pass
        
        self._clean_hls_immediate()
    
    def _check_ts_size(self):
        try:
            ts_files = glob.glob(os.path.join(self.output_dir, "seg_*.ts"))
            if not ts_files:
                if self.proc_start_time == 0: return True
                return (time.time() - self.proc_start_time) < 5
            
            latest_ts = max(ts_files, key=os.path.getmtime)
            mtime = os.path.getmtime(latest_ts)
            if time.time() - mtime > 3: return False
            return True
        except:
            return True
    
    def _start_ffmpeg(self, clean_old_files=True):
        os.makedirs(self.output_dir, exist_ok=True)
        
        if clean_old_files:
            self._clean_old_ts_files()
        
        start_number = int(time.time() * 100) % 1000000
        
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-fflags", "+genpts+igndts+flush_packets",
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
            "-hls_flags", "delete_segments+append_list+independent_segments",
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
                bufsize=4096
            )
            self.proc_start_time = time.time()
            return True
        except Exception:
            self.proc = None
            return False
    
    def _kill_ffmpeg(self):
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

    def _clean_hls(self):
        self._clean_hls_immediate()
    
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