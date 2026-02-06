import os
import time
import subprocess
import urllib.parse
import json
import re
import sys
import threading
import shutil
import glob
from collections import OrderedDict, defaultdict
from flask import Flask, request, jsonify, send_file, send_from_directory, Response
from channel_manager import Channel, HLS_ROOT, GlobalThreadPool
from iptv_watcher import IPTVWatcher, load_iptv
import iptv
import pytz
from datetime import datetime
SHANGHAI_TZ = pytz.timezone('Asia/Shanghai')

def get_beijing_time():
    """获取北京时间"""
    return datetime.now(SHANGHAI_TZ)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv("CONFIG_FILE")
if not CONFIG_FILE:
    CONFIG_FILE = os.path.join(BASE_DIR, "iptv_config.json")

os.environ['BASE_DIR'] = BASE_DIR
os.environ['CONFIG_FILE'] = CONFIG_FILE
os.environ['LOG_DIR'] = os.getenv("LOG_DIR", os.path.join(BASE_DIR, "logs"))
os.environ['HLS_ROOT'] = os.getenv("HLS_ROOT", "hls")

IPTV_FILE = os.getenv("IPTV_FILE", os.path.join(BASE_DIR, "IPTV.txt"))
PROXY_FILE = os.getenv("PROXY_FILE", os.path.join(BASE_DIR, "zubo.txt"))
HLS_DIR = os.environ['HLS_ROOT']
LOG_DIR = os.environ['LOG_DIR']
WEB_DIR = os.path.join(BASE_DIR, "web")
PORT = int(os.getenv("PORT", "5020"))
PASSWORD_FILE = os.path.join(BASE_DIR, "web_password.json")

app = Flask(__name__, static_folder="web", static_url_path="/web")

ACTIVE_WINDOW = 8
MIN_UPTIME = 13
IP_CLEAN_INTERVAL = 2
CHANNEL_CHECK_INTERVAL = 3

class ServiceManager:
    _instance = None
    _lock = threading.RLock()
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, '_initialized'):
            with self._lock:
                if not hasattr(self, '_initialized'):
                    self._initialized = True
                    self.manager = None
                    self.global_cleaner = None
                    self.ip_activity_manager = None
                    self._watchdog_observer = None
                    self._is_shutting_down = False
    
    def initialize(self):
        if self._is_shutting_down:
            return False
        
        with self._lock:
            try:
                data = load_iptv()
                
                channels = {}
                for channel_name, sources in data.items():
                    if not sources:
                        continue
                    
                    try:
                        ch = Channel(channel_name, list(sources))
                        channels[channel_name] = ch
                    except Exception as e:
                        continue
                
                self.ip_activity_manager = IPActivityManager()
                
                self.manager = ChannelManager(channels, self.ip_activity_manager)

                self.ip_activity_manager.start_cleanup_thread()
                
                self.global_cleaner = GlobalCleaner()
                
                self._start_watchdog()
                
                return True
                
            except Exception as e:
                self.cleanup()
                return False
    
    def _start_watchdog(self):
        try:
            from watchdog.observers import Observer
            from iptv_watcher import IPTVWatcher
            
            watcher = IPTVWatcher(self.manager)
            self._watchdog_observer = Observer()
            self._watchdog_observer.schedule(watcher, BASE_DIR, recursive=False)
            self._watchdog_observer.start()
            
        except ImportError as e:
            pass
        except Exception as e:
            pass
    
    def get_manager(self):
        with self._lock:
            if not self.manager and not self._is_shutting_down:
                self.initialize()
            return self.manager
    
    def get_cleaner(self):
        with self._lock:
            return self.global_cleaner
    
    def cleanup(self):
        with self._lock:
            self._is_shutting_down = True
            
            if self._watchdog_observer:
                try:
                    self._watchdog_observer.stop()
                    self._watchdog_observer.join(timeout=2)
                except:
                    pass
                self._watchdog_observer = None
            
            if self.ip_activity_manager:
                try:
                    self.ip_activity_manager.stop()
                except:
                    pass
                self.ip_activity_manager = None
            
            if self.manager:
                try:
                    self.manager.cleanup()
                except:
                    pass
                self.manager = None
            
            if self.global_cleaner:
                try:
                    self.global_cleaner.stop()
                except:
                    pass
                self.global_cleaner = None
            
            try:
                GlobalThreadPool.shutdown()
            except:
                pass


class IPActivityManager:
    """IP 活跃管理器"""
    def __init__(self):
        self.channel_activities = {}  # channel_name -> {ip: last_seen_ts}
        self.lock = threading.RLock()
        self._cleanup_thread = None
        self._running = False
    
    def record_access(self, ip, channel_name):
        """记录 IP 对频道的访问"""
        current_time = time.time()
        
        with self.lock:
            if channel_name not in self.channel_activities:
                self.channel_activities[channel_name] = {}
            
            self.channel_activities[channel_name][ip] = current_time
    
    def get_active_ips(self, channel_name):
        """获取频道的活跃 IP"""
        current_time = time.time()
        active_ips = {}
        
        with self.lock:
            if channel_name in self.channel_activities:
                for ip, last_seen in self.channel_activities[channel_name].items():
                    if current_time - last_seen <= ACTIVE_WINDOW:
                        active_ips[ip] = last_seen
        
        return active_ips
    
    def is_channel_active(self, channel_name):
        """检查频道是否有活跃 IP"""
        return len(self.get_active_ips(channel_name)) > 0
    
    def cleanup_expired_ips(self):
        """清理过期 IP"""
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
                    
                    if not self.channel_activities[channel_name]:
                        del self.channel_activities[channel_name]
    
    def start_cleanup_thread(self):
        """启动 IP 清理线程"""
        self._running = True
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="IPCleaner"
        )
        self._cleanup_thread.start()
    
    def _cleanup_loop(self):
        """IP 清理循环"""
        while self._running:
            try:
                self.cleanup_expired_ips()
            except Exception:
                pass
            time.sleep(IP_CLEAN_INTERVAL)
    
    def stop(self):
        """停止清理线程"""
        self._running = False
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=2)


class GlobalCleaner:
    def __init__(self, check_interval=3600):
        self.check_interval = check_interval
        self.running = True
        
        self.cleaner_thread = threading.Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="GlobalCleaner"
        )
        self.cleaner_thread.start()
    
    def stop(self):
        self.running = False
        if self.cleaner_thread and self.cleaner_thread.is_alive():
            self.cleaner_thread.join(timeout=2)
    
    def _cleanup_loop(self):
        while self.running:
            time.sleep(self.check_interval)
            try:
                self._clean_empty_dirs()
            except Exception as e:
                pass
    
    def _clean_empty_dirs(self):
        try:
            if not os.path.exists(HLS_DIR):
                return
            
            current_time = time.time()
            file_timeout = 300
            
            cleaned_dirs = 0
            cleaned_files = 0
            
            for dir_name in os.listdir(HLS_DIR):
                dir_path = os.path.join(HLS_DIR, dir_name)
                
                if not os.path.isdir(dir_path):
                    continue
                
                try:
                    files = os.listdir(dir_path)
                    if not files:
                        shutil.rmtree(dir_path)
                        cleaned_dirs += 1
                        continue
                    
                    expired_files = []
                    for f in files:
                        file_path = os.path.join(dir_path, f)
                        if os.path.isfile(file_path):
                            mtime = os.path.getmtime(file_path)
                            if current_time - mtime > file_timeout:
                                expired_files.append(f)
                    
                    for f in expired_files:
                        file_path = os.path.join(dir_path, f)
                        os.remove(file_path)
                        cleaned_files += 1
                        
                except Exception as e:
                    pass
            
        except Exception as e:
            pass

def get_base_url():
    if 'X-Forwarded-Proto' in request.headers:
        proto = request.headers.get('X-Forwarded-Proto', 'http')
        host = request.headers.get('X-Forwarded-Host', request.host)
        return f"{proto}://{host}"

    return request.host_url.rstrip('/')

def write_zubo(base_url):
    try:
        if not os.path.exists(IPTV_FILE):
            return False

        with open(IPTV_FILE, "r", encoding="utf-8") as src, \
             open(PROXY_FILE, "w", encoding="utf-8") as dst:

            written_channels = set()
            skip_update_block = False

            for line in src:
                line = line.strip()

                if not line:
                    dst.write("\n")
                    continue

                if "更新时间,#genre#" in line:
                    skip_update_block = True
                    continue

                if skip_update_block:
                    skip_update_block = False
                    continue

                if "#genre#" in line:
                    dst.write(line + "\n")
                    continue

                if "," in line:
                    channel_name, url = line.split(",", 1)
                    channel_name = channel_name.strip()

                    if "LOGO/Disclaimer.mp4" in url:
                        continue

                    if channel_name in written_channels:
                        continue

                    written_channels.add(channel_name)

                    dst.write(
                        f"{channel_name},{base_url}/hls/{channel_name}/index.m3u8\n"
                    )
                else:
                    dst.write(line + "\n")

        return True

    except Exception as e:
        return False

def standard_response(code, msg, data=None):
    return {"code": code, "msg": msg, "data": data}

def validate_filename(filename):
    if not filename:
        return False
    if not filename.endswith('.txt'):
        return False
    name_only = filename[:-4]
    return bool(re.match(r'^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$', name_only))

def safe_join(directory, filename):
    base_path = os.path.abspath(directory)
    full_path = os.path.normpath(os.path.join(base_path, filename))
    
    if not full_path.startswith(base_path):
        raise ValueError(f"非法路径访问: {filename}")
    
    if '..' in filename or filename.startswith('/'):
        raise ValueError(f"非法路径: {filename}")
    
    return full_path

def _serve_file_directly(file_path, file_type):
    try:
        if file_type == "m3u8":
            mime_type = "application/vnd.apple.mpegurl"
            headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Access-Control-Allow-Origin": "*"
            }
        elif file_type == "ts":
            mime_type = "video/mp2t"
            headers = {
                "Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*"
            }
        else:
            mime_type = "application/octet-stream"
            headers = {"Access-Control-Allow-Origin": "*"}
        
        if not os.path.exists(file_path):
            return "文件不存在", 404
        
        response = send_file(
            file_path,
            mimetype=mime_type,
            conditional=True
        )
        
        for key, value in headers.items():
            response.headers[key] = value
            
        return response
    except Exception as e:
        return "文件服务失败", 500

def validate_config_settings(new_settings, old_settings):
    try:
        validated = old_settings.copy() if old_settings else {}
        
        if new_settings:
            for key, value in new_settings.items():
                validated[key] = value
        
        return True, "配置验证通过", validated
    except Exception as e:
        return False, f"配置验证失败: {str(e)}", {}

def _is_channel_ready(ch):
    m3u8_path = os.path.join(ch.output_dir, "index.m3u8")
    if not os.path.exists(m3u8_path):
        return False
    
    try:
        with open(m3u8_path, 'r') as f:
            content = f.read()
            return ".ts" in content 
    except:
        return False


class ChannelManager:
    def __init__(self, channels, ip_activity_manager):
        self.channels = channels
        self.lock = threading.RLock()
        self.ip_activity_manager = ip_activity_manager
        
        for name, ch in self.channels.items():
            ch.set_ip_activity_manager(ip_activity_manager)
            
            ch.start_check_thread()
    
    def record_ip_activity(self, channel_name, client_ip):
        """记录 IP 活跃时间"""
        if client_ip and self.ip_activity_manager:
            self.ip_activity_manager.record_access(client_ip, channel_name)
    
    def touch(self, channel_name, client_ip=None):
        """触摸频道"""
        with self.lock:
            ch = self.channels.get(channel_name)
            if not ch:
                return False
            
            if client_ip:
                self.record_ip_activity(channel_name, client_ip)
            
            result = ch.touch()
            
            return True
    
    def get_channel_status(self, channel_name):
        with self.lock:
            ch = self.channels.get(channel_name)
            if not ch:
                return None
            
            active_ips = {}
            if self.ip_activity_manager:
                active_ips = self.ip_activity_manager.get_active_ips(channel_name)
            
            status = {
                'name': ch.name,
                'state': ch.state,
                'proc_running': ch.proc is not None and ch.proc.poll() is None,
                'last_touch': ch.last_touch_time,
                'hls_ready': ch.hls_ready if hasattr(ch, 'hls_ready') else False,
                'hls_ready_time': ch.hls_ready_time if hasattr(ch, 'hls_ready_time') else 0,
                'active_clients': len(active_ips),
                'sources_count': len(ch.sources) if hasattr(ch, 'sources') else 0,
                'start_time': ch.start_time if hasattr(ch, 'start_time') else 0,
                'last_active_ts': ch.last_active_ts if hasattr(ch, 'last_active_ts') else 0
            }
            
            return status

    def reload(self, new_data):
        with self.lock:
            old_names = set(self.channels.keys())
            new_names = set(new_data.keys())
            to_remove = old_names - new_names
            
            for name in to_remove:
                ch = self.channels[name]
                ch.cleanup()
                del self.channels[name]
                if self.ip_activity_manager and name in self.ip_activity_manager.channel_activities:
                    del self.ip_activity_manager.channel_activities[name]
            
            for name, sources in new_data.items():
                if name in self.channels:
                    self.channels[name].sources = list(sources)
                else:
                    try:
                        ch = Channel(name, list(sources))
                        ch.set_ip_activity_manager(self.ip_activity_manager)
                        ch.start_check_thread()
                        self.channels[name] = ch
                    except Exception as e:
                        pass
    
    def cleanup(self):
        with self.lock:
            for name, ch in list(self.channels.items()):
                try:
                    ch.cleanup()
                except Exception as e:
                    pass
            self.channels.clear()


def execute_scheduled_update():
    """执行定时更新任务"""
    try:
        start_time = time.time()
        
        result = subprocess.run(
            ["python", "/app/iptv.py", "--no-wait"], 
            cwd=BASE_DIR, 
            timeout=300,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            new_data = load_iptv()
            service_manager = ServiceManager()
            manager = service_manager.get_manager()
            if manager:
                manager.reload(new_data)
            
        execution_time = time.time() - start_time
        current_time_str = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
        with open(os.path.join(LOG_DIR, "schedule_updates.log"), "a", encoding="utf-8") as f:
            f.write(f"[{current_time_str}] 定时更新完成，耗时: {execution_time:.1f}秒\n")
            
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        error_time = get_beijing_time().strftime('%Y-%m-%d %H:%M:%S')
        error_msg = f"[{error_time}] 定时更新错误: {str(e)}"
        with open(os.path.join(LOG_DIR, "schedule_errors.log"), "a", encoding="utf-8") as f:
            f.write(error_msg + "\n")

def start_beijing_scheduler():
    """
    北京时间调度器
    """
    print("🕒 启动北京时间调度器")

    last_run = set()

    def scheduler_loop():
        while True:
            try:
                now = get_beijing_time()
                today = now.strftime("%Y-%m-%d")
                current_hm = now.strftime("%H:%M")

                config = iptv.load_config()
                settings = config.get("settings", {})
                schedules = settings.get("schedules", [])

                for item in schedules:
                    if not isinstance(item, dict):
                        continue

                    if item.get("enabled") in [False, "false", "False", 0]:
                        continue

                    time_str = item.get("time")
                    if not time_str:
                        continue

                    run_key = f"{today}_{time_str}"

                    if current_hm == time_str and run_key not in last_run:
                        print(f"⏰ 命中定时任务 {time_str}")
                        execute_scheduled_update()
                        last_run.add(run_key)

                last_run_copy = set(last_run)
                for k in last_run_copy:
                    if not k.startswith(today):
                        last_run.remove(k)

            except Exception as e:
                print(f"❌ 定时调度异常: {e}")

            time.sleep(20)

    t = threading.Thread(
        target=scheduler_loop,
        daemon=True,
        name="BeijingScheduler"
    )
    t.start()


@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(__file__), "iptv_config.html")
    
    if not os.path.exists(html_path):
        return "iptv_config.html 文件不存在"
    
    return send_file(html_path)

@app.route("/zubo.txt")
def zubo():
    public_base = os.getenv("PUBLIC_BASE_URL")
    if public_base:
        base_url = public_base.rstrip("/")
    else:
        base_url = get_base_url()

    ok = write_zubo(base_url)
    if not ok:
        return "zubo.txt generate failed", 500

    return send_file(
        PROXY_FILE,
        mimetype="text/plain; charset=utf-8",
        as_attachment=False
    )

@app.route("/hls/<path:channel_path>")
def serve_hls(channel_path):
    service_manager = ServiceManager()
    manager = service_manager.get_manager()
    
    if manager is None:
        return "服务未初始化", 503
    
    client_ip = request.remote_addr
    
    try:
        decoded_path = urllib.parse.unquote(channel_path)
        channel_path = decoded_path
    except:
        pass
    
    parts = channel_path.split("/")
    parts = [part.strip() for part in parts if part.strip()]
    
    if not parts:
        return "路径错误", 400
    
    channel_name = parts[0]
    
    if len(parts) == 1:
        filename = "index.m3u8"
    else:
        filename = parts[-1]
        if not filename or filename.strip() == "":
            filename = "index.m3u8"
    
    if not (filename.endswith('.m3u8') or filename.endswith('.ts')):
        return "不支持的文件类型", 400
    
    hls_dir = os.path.join(HLS_DIR, channel_name)
    file_path = os.path.join(hls_dir, filename)
    
    if not file_path.startswith(HLS_ROOT):
        return "路径错误", 403
    
    is_m3u8 = filename.endswith(".m3u8")

    if client_ip:
        manager.record_ip_activity(channel_name, client_ip)
    
    if is_m3u8:
        success = _ensure_channel_ready(channel_name, manager, client_ip)
        
        if not success:
            return Response(
                status=503,
                mimetype="text/plain",
                headers={
                    "Retry-After": "3",
                    "Cache-Control": "no-cache",
                }
            )
        
        if not os.path.exists(file_path):
            return "Stream file sync error", 500
        
        return _serve_file_directly(file_path, "m3u8")
    
    if not os.path.exists(file_path):
        return "文件不存在", 404
    
    if client_ip:
        manager.record_ip_activity(channel_name, client_ip)
    
    return _serve_file_directly(file_path, "ts")

def _ensure_channel_ready(channel_name, manager, client_ip=None):
    """确保频道就绪，返回True表示频道可以播放"""
    ch = manager.channels.get(channel_name)
    if not ch:
        return False
    
    if client_ip:
        manager.record_ip_activity(channel_name, client_ip)
    
    result = ch.touch()
    
    return result


def load_password():
    default_password = {
        "password": "admin",
        "last_modified": time.time()
    }
    
    if not os.path.exists(PASSWORD_FILE):
        save_password("admin")
        return default_password
    
    try:
        with open(PASSWORD_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data
    except Exception as e:
        return default_password

def save_password(new_password):
    try:
        password_data = {
            "password": new_password,
            "last_modified": time.time()
        }
        with open(PASSWORD_FILE, "w", encoding="utf-8") as f:
            json.dump(password_data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        return False

def verify_password(input_password):
    stored_password = load_password()
    return input_password == stored_password.get("password", "admin")

@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        data = request.json
        if not data:
            return jsonify(standard_response(-1, "请求数据不能为空"))
        
        password = data.get("password", "").strip()
        
        if not password:
            return jsonify(standard_response(-1, "密码不能为空"))
        
        if verify_password(password):
            import hashlib
            import random
            session_token = hashlib.sha256(f"{password}{time.time()}{random.random()}".encode()).hexdigest()
            
            response_data = {
                "token": session_token,
                "message": "登录成功"
            }
            return jsonify(standard_response(200, "登录成功", response_data))
        else:
            return jsonify(standard_response(-1, "密码错误"))
            
    except Exception as e:
        return jsonify(standard_response(-1, f"登录验证失败: {str(e)}"))

@app.route("/api/auth/change-password", methods=["POST"])
def change_password():
    try:
        data = request.json
        if not data:
            return jsonify(standard_response(-1, "请求数据不能为空"))
        
        old_password = data.get("old_password", "").strip()
        new_password = data.get("new_password", "").strip()
        
        if not old_password or not new_password:
            return jsonify(standard_response(-1, "旧密码和新密码都不能为空"))
        
        if len(new_password) < 4:
            return jsonify(standard_response(-1, "新密码长度至少4位"))
        
        if not verify_password(old_password):
            return jsonify(standard_response(-1, "旧密码错误"))
        
        if save_password(new_password):
            return jsonify(standard_response(200, "密码修改成功"))
        else:
            return jsonify(standard_response(-1, "密码保存失败"))
            
    except Exception as e:
        return jsonify(standard_response(-1, f"修改密码失败: {str(e)}"))

@app.errorhandler(404)
def page_not_found(e):
    return jsonify(standard_response(-1, f"接口不存在：{str(e)}")), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify(standard_response(-1, f"请求方式错误：{str(e)}")), 405

@app.errorhandler(500)
def internal_server_error(e):
    return jsonify(standard_response(-1, "服务器内部错误，请查看日志")), 500

@app.errorhandler(400)
def bad_request(e):
    return jsonify(standard_response(-1, f"请求参数错误：{str(e)}")), 400

@app.route("/api/config", methods=["GET"])
def get_config():
    try:
        config = iptv.load_config()
        
        response_data = OrderedDict()
        
        categories = config.get("categories", {})
        if isinstance(categories, OrderedDict):
            categories_response = []
            for key, value in categories.items():
                categories_response.append([key, value])
            response_data["categories"] = categories_response
        else:
            response_data["categories"] = [[k, v] for k, v in categories.items()]
        
        mapping = config.get("mapping", {})
        if isinstance(mapping, OrderedDict):
            mapping_response = []
            for key, value in mapping.items():
                mapping_response.append([key, value])
            response_data["mapping"] = mapping_response
        else:
            response_data["mapping"] = [[k, v] for k, v in mapping.items()]
        
        response_data["third_party_urls"] = config.get("third_party_urls", {})
        response_data["settings"] = config.get("settings", {})
        
        return jsonify(standard_response(200, "获取配置成功", response_data))
        
    except Exception as e:
        default_config = iptv.DEFAULT_CONFIG
        response_data = OrderedDict()
        
        categories = default_config.get("categories", {})
        categories_array = [[k, v] for k, v in categories.items()]
        response_data["categories"] = categories_array
        
        mapping = default_config.get("mapping", {})
        mapping_array = [[k, v] for k, v in mapping.items()]
        response_data["mapping"] = mapping_array
        
        response_data["third_party_urls"] = default_config.get("third_party_urls", {})
        response_data["settings"] = default_config.get("settings", {})
        
        return jsonify(standard_response(200, f"使用默认配置", response_data))

@app.route("/api/config", methods=["POST"])
def update_config():
    try:
        new_config = request.json
        if not new_config:
            return jsonify(standard_response(-1, "配置数据不能为空"))

        old_config = iptv.load_config()
        
        final_config = OrderedDict()
        
        if "categories" in new_config:
            categories_data = new_config["categories"]
            
            if isinstance(categories_data, dict):
                if "_order" in categories_data and isinstance(categories_data["_order"], list):
                    final_config["categories"] = OrderedDict()
                    for key in categories_data["_order"]:
                        if key in categories_data and key != "_order":
                            final_config["categories"][key] = categories_data[key]
                else:
                    final_config["categories"] = OrderedDict(categories_data)
            else:
                final_config["categories"] = OrderedDict(old_config.get("categories", {}))
        else:
            final_config["categories"] = OrderedDict(old_config.get("categories", {}))
        
        if "mapping" in new_config:
            mapping_data = new_config["mapping"]
            
            if isinstance(mapping_data, dict):
                if "_order" in mapping_data and isinstance(mapping_data["_order"], list):
                    final_config["mapping"] = OrderedDict()
                    for key in mapping_data["_order"]:
                        if key in mapping_data and key != "_order":
                            final_config["mapping"][key] = mapping_data[key]
                else:
                    final_config["mapping"] = OrderedDict(mapping_data)
            else:
                final_config["mapping"] = OrderedDict(old_config.get("mapping", {}))
        else:
            final_config["mapping"] = OrderedDict(old_config.get("mapping", {}))
        
        if "third_party_urls" in new_config:
            urls_data = new_config["third_party_urls"]
            if isinstance(urls_data, dict):
                final_config["third_party_urls"] = OrderedDict(urls_data)
            else:
                final_config["third_party_urls"] = OrderedDict(old_config.get("third_party_urls", {}))
        else:
            final_config["third_party_urls"] = OrderedDict(old_config.get("third_party_urls", {}))
        
        if "settings" in new_config:
            is_valid, msg, validated_settings = validate_config_settings(
                new_config["settings"], 
                old_config.get("settings", {})
            )
            if not is_valid:
                return jsonify(standard_response(-1, msg))
            final_config["settings"] = validated_settings
        else:
            final_config["settings"] = OrderedDict(old_config.get("settings", {}))
        
        if iptv.save_config(final_config):
            response_data = {
                "categories": final_config["categories"],
                "mapping": final_config["mapping"],
                "third_party_urls": final_config["third_party_urls"],
                "settings": final_config["settings"]
            }
            
            if isinstance(response_data["categories"], OrderedDict):
                response_data["categories"]._order = list(response_data["categories"].keys())
            if isinstance(response_data["mapping"], OrderedDict):
                response_data["mapping"]._order = list(response_data["mapping"].keys())
            
            return jsonify(standard_response(200, "配置更新成功", response_data))
        else:
            return jsonify(standard_response(-1, "配置保存失败"))
            
    except Exception as e:
        return jsonify(standard_response(-1, f"更新配置失败: {str(e)}"))

@app.route("/api/ip/files", methods=["GET"])
def list_ip_files():
    try:
        if not os.path.exists(iptv.IP_DIR):
            os.makedirs(iptv.IP_DIR, exist_ok=True)
            default_file = os.path.join(iptv.IP_DIR, "default_ip.txt")
            if not os.path.exists(default_file):
                with open(default_file, 'w', encoding='utf-8') as f:
                    f.write("# IP地址列表\n# 格式: IP:端口\n192.168.1.100:8080\n")
        
        files = [f for f in os.listdir(iptv.IP_DIR) 
                if f.endswith(".txt") and os.path.isfile(os.path.join(iptv.IP_DIR, f))]
        files.sort()
        
        return jsonify(standard_response(200, "获取IP文件列表成功", files))
        
    except Exception as e:
        return jsonify(standard_response(-1, f"获取IP文件列表失败: {str(e)}"))

@app.route("/api/ip/file/<filename>", methods=["GET"])
def read_ip_file(filename):
    try:
        filename = urllib.parse.unquote(filename)
        file_path = safe_join(iptv.IP_DIR, filename)

        if not os.path.exists(file_path):
            return jsonify(standard_response(-1, "IP文件不存在"))

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        return jsonify(standard_response(200, "读取IP文件成功", content))
        
    except ValueError as e:
        return jsonify(standard_response(-1, str(e)))
    except Exception as e:
        return jsonify(standard_response(-1, f"读取IP文件失败: {str(e)}"))

@app.route("/api/ip/file/<filename>", methods=["POST"])
def save_ip_file(filename):
    try:
        filename = urllib.parse.unquote(filename)
        if not validate_filename(filename):
            return jsonify(standard_response(-1, "文件名格式无效，必须以.txt结尾且仅包含字母数字下划线中文"))
        
        content = request.json.get("content", "")
        file_path = safe_join(iptv.IP_DIR, filename)
        
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
            
        return jsonify(standard_response(200, "IP文件保存成功"))
        
    except ValueError as e:
        return jsonify(standard_response(-1, str(e)))
    except Exception as e:
        return jsonify(standard_response(-1, f"保存IP文件失败: {str(e)}"))

@app.route("/api/ip/file/<filename>", methods=["DELETE"])
def delete_ip_file(filename):
    try:
        filename = urllib.parse.unquote(filename)
        file_path = safe_join(iptv.IP_DIR, filename)

        if not os.path.exists(file_path):
            return jsonify(standard_response(-1, "IP文件不存在"))
            
        os.remove(file_path)
        return jsonify(standard_response(200, "IP文件删除成功"))
        
    except ValueError as e:
        return jsonify(standard_response(-1, str(e)))
    except Exception as e:
        return jsonify(standard_response(-1, f"删除IP文件失败: {str(e)}"))

@app.route("/api/rtp/files", methods=["GET"])
def get_rtp_files():
    try:
        if not os.path.exists(iptv.RTP_DIR):
            os.makedirs(iptv.RTP_DIR, exist_ok=True)
            default_file = os.path.join(iptv.RTP_DIR, "default_rtp.txt")
            if not os.path.exists(default_file):
                with open(default_file, 'w', encoding='utf-8') as f:
                    f.write("# RTP频道列表\n# 格式: 频道名,RTP/UDP地址\nCCTV1,rtp://239.1.1.1:1234\n")
        
        files = [f for f in os.listdir(iptv.RTP_DIR) 
                if f.endswith(".txt") and os.path.isfile(os.path.join(iptv.RTP_DIR, f))]
        files.sort()
        
        return jsonify(standard_response(200, "获取RTP文件列表成功", files))
        
    except Exception as e:
        return jsonify(standard_response(-1, f"获取RTP文件列表失败: {str(e)}"))

@app.route("/api/rtp/file/<filename>", methods=["GET"])
def read_rtp_file(filename):
    try:
        filename = urllib.parse.unquote(filename)
        file_path = safe_join(iptv.RTP_DIR, filename)

        if not os.path.exists(file_path):
            return jsonify(standard_response(-1, "RTP文件不存在"))
            
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        return jsonify(standard_response(200, "读取RTP文件成功", content))
        
    except ValueError as e:
        return jsonify(standard_response(-1, str(e)))
    except Exception as e:
        return jsonify(standard_response(-1, f"读取RTP文件失败: {str(e)}"))

@app.route("/api/rtp/file/<filename>", methods=["POST"])
def save_rtp_file(filename):
    try:
        filename = urllib.parse.unquote(filename)
        if not validate_filename(filename):
            return jsonify(standard_response(-1, "文件名格式无效，必须以.txt结尾且仅包含字母数字下划线中文"))

        content = request.json.get("content", "")
        file_path = safe_join(iptv.RTP_DIR, filename)
        
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        
        return jsonify(standard_response(200, "RTP文件保存成功"))
        
    except ValueError as e:
        return jsonify(standard_response(-1, str(e)))
    except Exception as e:
        return jsonify(standard_response(-1, f"保存RTP文件失败: {str(e)}"))

@app.route("/api/rtp/file/<filename>", methods=["DELETE"])
def delete_rtp_file(filename):
    try:
        filename = urllib.parse.unquote(filename)
        if not validate_filename(filename):
            return jsonify(standard_response(-1, "文件名格式无效，必须以.txt结尾且仅包含字母数字下划线中文"))

        file_path = safe_join(iptv.RTP_DIR, filename)
        if not os.path.exists(file_path):
            return jsonify(standard_response(-1, "RTP文件不存在"))
        
        os.remove(file_path)
        return jsonify(standard_response(200, "RTP文件删除成功"))
        
    except ValueError as e:
        return jsonify(standard_response(-1, str(e)))
    except Exception as e:
        return jsonify(standard_response(-1, f"删除RTP文件失败: {str(e)}"))

@app.route("/api/third-party/urls", methods=["GET"])
def get_third_party_urls():
    try:
        config = iptv.load_config()
        third_party_urls = config.get("third_party_urls", iptv.DEFAULT_THIRD_PARTY_URLS)
        url_list = [{"url": url, "filename": filename} for url, filename in third_party_urls.items()]
        return jsonify(standard_response(200, "获取第三方URL成功", url_list))
    except Exception as e:
        default_url_list = [{"url": url, "filename": filename} for url, filename in iptv.DEFAULT_THIRD_PARTY_URLS.items()]
        return jsonify(standard_response(200, f"获取第三方URL失败，使用默认值", default_url_list))

@app.route("/api/third-party/url", methods=["POST"])
def add_third_party_url():
    try:
        data = request.json
        url = data.get("url", "").strip()
        filename = data.get("filename", "").strip()

        if not url or not filename:
            return jsonify(standard_response(-1, "URL和文件名不能为空"))
        if not validate_filename(filename):
            return jsonify(standard_response(-1, "文件名格式无效，必须以.txt结尾且仅包含字母数字下划线中文"))

        config = iptv.load_config()
        third_party_urls = OrderedDict(config.get("third_party_urls", iptv.DEFAULT_THIRD_PARTY_URLS))
        third_party_urls[url] = filename

        config["third_party_urls"] = third_party_urls
        if iptv.save_config(config):
            url_list = [{"url": u, "filename": f} for u, f in third_party_urls.items()]
            return jsonify(standard_response(200, "添加第三方URL成功", url_list))
        else:
            return jsonify(standard_response(-1, "保存配置失败"))
    except Exception as e:
        return jsonify(standard_response(-1, f"添加第三方URL失败: {str(e)}"))

@app.route("/api/third-party/url", methods=["DELETE"])
def delete_third_party_url():
    try:
        data = request.json
        url = data.get("url", "").strip()
        if not url:
            return jsonify(standard_response(-1, "URL不能为空"))

        config = iptv.load_config()
        third_party_urls = config.get("third_party_urls", OrderedDict())
        if url in third_party_urls:
            del third_party_urls[url]
            config["third_party_urls"] = third_party_urls
            if iptv.save_config(config):
                return jsonify(standard_response(200, "删除第三方URL成功"))
            else:
                return jsonify(standard_response(-1, "保存配置失败"))
        else:
            return jsonify(standard_response(-1, "URL不存在"))
    except Exception as e:
        return jsonify(standard_response(-1, f"删除第三方URL失败: {str(e)}"))

@app.route("/api/third-party/urls", methods=["PUT"])
def update_third_party_urls():
    try:
        data = request.json
        url_list = data.get("urls", [])

        new_urls = OrderedDict()
        for item in url_list:
            url = item.get("url", "").strip()
            filename = item.get("filename", "").strip()
            if not url or not filename:
                continue
            if validate_filename(filename):
                new_urls[url] = filename

        config = iptv.load_config()
        config["third_party_urls"] = new_urls
        if iptv.save_config(config):
            return jsonify(standard_response(200, "更新第三方URL成功"))
        else:
            return jsonify(standard_response(-1, "保存配置失败"))
    except Exception as e:
        return jsonify(standard_response(-1, f"更新第三方URL失败: {str(e)}"))

@app.route("/api/iptv/update", methods=["POST"])
def manual_update_iptv():
    try:
        from threading import Thread
        
        def run_update():
            try:
                result = subprocess.run(
                    ["python", "iptv.py", "--manual"],
                    cwd=BASE_DIR,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
                
                time.sleep(2)
                new_data = load_iptv()
                
                service_manager = ServiceManager()
                manager = service_manager.get_manager()
                if manager:
                    manager.reload(new_data)
                
            except subprocess.TimeoutExpired:
                pass
            except Exception as e:
                pass
        
        Thread(target=run_update, daemon=True).start()
        
        return jsonify(standard_response(200, "手动更新任务已启动"))
        
    except Exception as e:
        return jsonify(standard_response(-1, f"启动手动更新失败: {str(e)}"))

@app.route("/api/schedules", methods=["GET"])
def get_schedules():
    """获取定时任务配置"""
    try:
        config = iptv.load_config()
        settings = config.get("settings", {})
        
        schedules = settings.get("schedules", [
            {"id": 1, "time": "04:00", "enabled": True},
            {"id": 2, "time": "12:00", "enabled": True},
            {"id": 3, "time": "20:00", "enabled": True}
        ])
        
        return jsonify(standard_response(200, "获取定时任务配置成功", schedules))
        
    except Exception as e:
        return jsonify(standard_response(-1, f"获取定时任务配置失败: {str(e)}"))

@app.route("/api/schedules", methods=["POST"])
def save_schedules():
    """保存定时任务配置"""
    try:
        data = request.json
        if not data or not isinstance(data, list):
            return jsonify(standard_response(-1, "请求数据格式错误，应为数组"))
        
        validated_schedules = []
        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                continue
                
            schedule_item = {
                "id": item.get("id", idx + 1),
                "time": item.get("time", "00:00"),
                "enabled": item.get("enabled", True)
            }
            
            if not re.match(r'^([01][0-9]|2[0-3]):[0-5][0-9]$', schedule_item["time"]):
                return jsonify(standard_response(-1, f"时间格式错误: {schedule_item['time']}，应使用HH:MM格式"))
            
            validated_schedules.append(schedule_item)
        
        if not validated_schedules:
            return jsonify(standard_response(-1, "至少需要配置一个定时任务"))
        
        config = iptv.load_config()
        
        if "settings" not in config:
            config["settings"] = OrderedDict()
        
        config["settings"]["schedules"] = validated_schedules
        
        if iptv.save_config(config):
            
            return jsonify(standard_response(200, "定时任务配置保存成功", validated_schedules))
        else:
            return jsonify(standard_response(-1, "配置保存失败"))
            
    except Exception as e:
        return jsonify(standard_response(-1, f"保存定时任务配置失败: {str(e)}"))

@app.route("/api/schedules/reload", methods=["POST"])
def reload_schedules():
    """重新加载定时任务"""
    return jsonify(standard_response(200, "北京时间调度器无需重载"))

@app.route("/api/health", methods=["GET"])
def health_check():
    try:
        service_manager = ServiceManager()
        manager = service_manager.get_manager()
        
        status = {
            "status": "healthy",
            "timestamp": time.time(),
            "service": "IPTV HLS Server",
            "channels": len(manager.channels) if manager else 0,
            "hls_dir": {
                "exists": os.path.exists(HLS_DIR),
                "size": sum(os.path.getsize(f) for f in glob.glob(os.path.join(HLS_DIR, "**/*"), recursive=True) if os.path.isfile(f))
            },
            "memory": {
                "thread_count": threading.active_count()
            }
        }
        
        return jsonify(standard_response(200, "服务运行正常", status))
    except Exception as e:
        return jsonify(standard_response(-1, f"健康检查失败: {str(e)}"))

@app.route("/api/channels/status", methods=["GET"])
def get_all_channels_status():
    try:
        service_manager = ServiceManager()
        manager = service_manager.get_manager()
        
        if not manager:
            return jsonify(standard_response(-1, "管理器未初始化"))
        
        with manager.lock:
            status_list = []
            for name, ch in manager.channels.items():
                active_ips = {}
                if manager.ip_activity_manager:
                    active_ips = manager.ip_activity_manager.get_active_ips(name)
                
                status = {
                    'name': name,
                    'state': ch.state,
                    'has_checker': ch.active_checker is not None,
                    'sources_count': len(ch.sources) if hasattr(ch, 'sources') else 0,
                    'active_clients': len(active_ips),
                    'hls_ready': ch.hls_ready if hasattr(ch, 'hls_ready') else False,
                    'start_time': ch.start_time if hasattr(ch, 'start_time') else 0,
                    'last_active_ts': ch.last_active_ts if hasattr(ch, 'last_active_ts') else 0
                }
                status_list.append(status)
            
            status_list.sort(key=lambda x: (0 if x['state'] == 'RUNNING' else 1, x['name']))
            
            return jsonify(standard_response(200, "获取频道状态成功", status_list))
            
    except Exception as e:
        return jsonify(standard_response(-1, f"获取频道状态失败: {str(e)}"))

def kill_orphan_ffmpeg():
    """启动时清理残留的 ffmpeg 进程"""
    if sys.platform.startswith('linux') or sys.platform == 'darwin':
        try:
            subprocess.run(["pkill", "-9", "ffmpeg"], capture_output=True)
        except:
            pass
    elif sys.platform.startswith('win'):
        try:
            subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], capture_output=True)
        except:
            pass

def init_services():
    kill_orphan_ffmpeg()
    try:
        required_dirs = [HLS_DIR, WEB_DIR, LOG_DIR]
        for dir_path in required_dirs:
            if not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)
        
        if not os.path.exists(IPTV_FILE):
            with open(IPTV_FILE, 'w', encoding='utf-8') as f:
                f.write("# IPTV 播放列表\n# 频道名,直播源URL\n")
        
        service_manager = ServiceManager()
        success = service_manager.initialize()
        
        if not success:
            return False

        start_beijing_scheduler()
        
        return True

    except Exception as e:
        return False

def main():
    if not init_services():
        return
    
    try:
        import atexit
        atexit.register(lambda: ServiceManager().cleanup())
        
        app.run(
            host='0.0.0.0', 
            port=PORT, 
            debug=False, 
            threaded=True,
            use_reloader=False
        )
    except KeyboardInterrupt:
        ServiceManager().cleanup()
    except Exception as e:
        ServiceManager().cleanup()

if __name__ == "__main__":
    main()
