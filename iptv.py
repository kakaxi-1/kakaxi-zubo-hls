import os
import re
import requests
import time
import concurrent.futures
import subprocess
from datetime import datetime
import socket
import json
from collections import OrderedDict
import sys
import pytz
import fcntl
import argparse
import urllib3
import shutil

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv("CONFIG_FILE")
if not CONFIG_FILE:
    CONFIG_FILE = os.path.join(BASE_DIR, "iptv_config.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "IPTV.txt")
DEFAULT_THIRD_PARTY_URLS = OrderedDict([
    ("https://raw.githubusercontent.com/kakaxi-1/IPTV/main/iptv.txt", "source1.txt"),
])

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0"
}

IP_DIR = os.path.join(BASE_DIR, "ip")
RTP_DIR = os.path.join(BASE_DIR, "rtp")
WEB_DIR = os.path.join(BASE_DIR, "web")

DEFAULT_CONFIG = OrderedDict([
    ("categories", OrderedDict([
        ("央视频道", ["CCTV1", "CCTV2", "CCTV3", "CCTV4", "CCTV4欧洲", "CCTV4美洲", "CCTV5", "CCTV5+", "CCTV6", "CCTV7", "CCTV8", "CCTV9", "CCTV10", "CCTV11", "CCTV12", "CCTV13", "CCTV14", "CCTV15", "CCTV16", "CCTV17", "CCTV4K", "CCTV8K", "兵器科技", "风云音乐", "风云足球", "风云剧场", "怀旧剧场", "第一剧场", "女性时尚", "世界地理", "央视台球", "高尔夫网球", "央视文化精品", "卫生健康", "电视指南"]),
        ("卫视频道", ["湖南卫视", "浙江卫视", "江苏卫视", "东方卫视", "深圳卫视", "北京卫视", "广东卫视", "广西卫视", "东南卫视", "海南卫视", "河北卫视", "河南卫视", "湖北卫视", "江西卫视", "四川卫视", "重庆卫视", "贵州卫视", "云南卫视", "天津卫视", "安徽卫视", "山东卫视", "辽宁卫视", "黑龙江卫视", "吉林卫视", "内蒙古卫视", "宁夏卫视", "山西卫视", "陕西卫视", "甘肃卫视", "青海卫视", "新疆卫视", "西藏卫视", "三沙卫视", "山东教育卫视", "中国教育1台", "中国教育2台", "中国教育3台", "中国教育4台", "早期教育"]),
        ("数字频道", ["CHC动作电影", "CHC家庭影院", "CHC影迷电影", "淘电影", "淘精彩", "淘剧场", "淘4K", "淘娱乐", "淘BABY", "淘萌宠", "重温经典", "星空卫视", "ChannelV", "凤凰卫视中文台", "凤凰卫视资讯台", "凤凰卫视香港台", "凤凰卫视电影台", "求索纪录", "求索科学", "求索生活", "求索动物", "纪实人文", "金鹰纪实", "纪实科教", "睛彩青少", "睛彩竞技", "睛彩篮球", "睛彩广场舞", "魅力足球", "五星体育", "乐游", "生活时尚", "都市剧场", "欢笑剧场", "游戏风云", "金色学堂", "动漫秀场", "新动漫", "卡酷少儿", "金鹰卡通", "优漫卡通", "哈哈炫动", "嘉佳卡通"])
    ])),
    ("mapping", OrderedDict([
        ("CCTV1", ["CCTV-1", "CCTV-1 HD", "CCTV1 HD", "CCTV-1综合"]),
        ("CCTV2", ["CCTV-2", "CCTV-2 HD", "CCTV2 HD", "CCTV-2财经"]),
        ("CCTV3", ["CCTV-3", "CCTV-3 HD", "CCTV3 HD", "CCTV-3综艺"]),
        ("CCTV4", ["CCTV-4", "CCTV-4 HD", "CCTV4 HD", "CCTV-4中文国际"]),
        ("CCTV4欧洲", ["CCTV-4欧洲", "CCTV-4欧洲", "CCTV4欧洲 HD", "CCTV-4 欧洲", "CCTV-4中文国际欧洲", "CCTV4中文欧洲"]),
        ("CCTV4美洲", ["CCTV-4美洲", "CCTV-4北美", "CCTV4美洲 HD", "CCTV-4 美洲", "CCTV-4中文国际美洲", "CCTV4中文美洲"]),
        ("CCTV5", ["CCTV-5", "CCTV-5 HD", "CCTV5 HD", "CCTV-5体育"]),
        ("CCTV5+", ["CCTV-5+", "CCTV-5+ HD", "CCTV5+ HD", "CCTV-5+体育赛事"]),
        ("CCTV6", ["CCTV-6", "CCTV-6 HD", "CCTV6 HD", "CCTV-6电影"]),
        ("CCTV7", ["CCTV-7", "CCTV-7 HD", "CCTV7 HD", "CCTV-7国防军事"]),
        ("CCTV8", ["CCTV-8", "CCTV-8 HD", "CCTV8 HD", "CCTV-8电视剧"]),
        ("CCTV9", ["CCTV-9", "CCTV-9 HD", "CCTV9 HD", "CCTV-9纪录"]),
        ("CCTV10", ["CCTV-10", "CCTV-10 HD", "CCTV10 HD", "CCTV-10科教"]),
        ("CCTV11", ["CCTV-11", "CCTV-11 HD", "CCTV11 HD", "CCTV-11戏曲"]),
        ("CCTV12", ["CCTV-12", "CCTV-12 HD", "CCTV12 HD", "CCTV-12社会与法"]),
        ("CCTV13", ["CCTV-13", "CCTV-13 HD", "CCTV13 HD", "CCTV-13新闻"]),
        ("CCTV14", ["CCTV-14", "CCTV-14 HD", "CCTV14 HD", "CCTV-14少儿"]),
        ("CCTV15", ["CCTV-15", "CCTV-15 HD", "CCTV15 HD", "CCTV-15音乐"]),
        ("CCTV16", ["CCTV-16", "CCTV-16 HD", "CCTV-16奥林匹克"]),
        ("CCTV17", ["CCTV-17", "CCTV-17 HD", "CCTV17 HD", "CCTV-17农业农村"]),
        ("兵器科技", ["CCTV-兵器科技", "CCTV兵器科技", "CCTV兵器科技HD"]),
        ("风云音乐", ["CCTV-风云音乐", "CCTV风云音乐", "CCTV风云音乐HD"]),
        ("第一剧场", ["CCTV-第一剧场", "CCTV第一剧场", "CCTV第一剧场HD"]),
        ("风云足球", ["CCTV-风云足球", "CCTV风云足球", "CCTV风云足球HD"]),
        ("风云剧场", ["CCTV-风云剧场", "CCTV风云剧场", "CCTV风云剧场HD"]),
        ("怀旧剧场", ["CCTV-怀旧剧场", "CCTV怀旧剧场", "CCTV怀旧剧场HD"]),
        ("女性时尚", ["CCTV-女性时尚", "CCTV女性时尚", "CCTV女性时尚HD"]),
        ("世界地理", ["CCTV-世界地理", "CCTV世界地理", "CCTV世界地理HD"]),
        ("央视台球", ["CCTV-央视台球", "CCTV央视台球", "CCTV央视台球HD"]),
        ("高尔夫网球", ["CCTV-高尔夫网球", "CCTV高尔夫网球", "CCTV央视高网", "CCTV-高尔夫·网球", "央视高网"]),
        ("央视文化精品", ["CCTV-央视文化精品", "CCTV央视文化精品", "CCTV文化精品", "CCTV-文化精品", "文化精品"]),
        ("卫生健康", ["CCTV-卫生健康", "CCTV卫生健康"]),
        ("电视指南", ["CCTV-电视指南", "CCTV电视指南"]),
        ("山东教育卫视", ["山东教育"]),
        ("中国教育1台", ["CETV1", "中国教育一台", "中国教育1", "CETV", "CETV-1", "中国教育"]),
        ("中国教育2台", ["CETV2", "中国教育二台", "中国教育2", "CETV-2 空中课堂", "CETV-2"]),
        ("中国教育3台", ["CETV3", "中国教育三台", "中国教育3", "CETV-3 教育服务", "CETV-3"]),
        ("中国教育4台", ["CETV4", "中国教育四台", "中国教育4", "中国教育电视台第四频道", "CETV-4"]),
        ("早期教育", ["中国教育5台", "中国教育五台", "CETV早期教育", "华电早期教育", "CETV 早期教育"]),
        ("CHC动作电影", ["CHC动作电影高清", "动作电影", "动作电影高清"]),
        ("CHC家庭影院", ["CHC家庭电影高清", "家庭影院", "家庭影院高清"]),
        ("CHC影迷电影", ["CHC高清电影", "CHC-影迷电影", "影迷电影", "chc高清电影"]),
        ("淘电影", ["IPTV淘电影", "北京IPTV淘电影", "北京淘电影"]),
        ("淘精彩", ["IPTV淘精彩", "北京IPTV淘精彩", "北京淘精彩"]),
        ("淘剧场", ["IPTV淘剧场", "北京IPTV淘剧场", "北京淘剧场"]),
        ("淘4K", ["IPTV淘4K", "北京IPTV4K超清", "北京淘4K", "北京IPTV4K", "淘 4K"]),
        ("淘娱乐", ["IPTV淘娱乐", "北京IPTV淘娱乐", "北京淘娱乐"]),
        ("淘BABY", ["IPTV淘BABY", "北京IPTV淘BABY", "北京淘BABY", "IPTV淘baby", "北京IPTV淘baby", "北京淘baby"]),
        ("淘萌宠", ["IPTV淘萌宠", "北京IPTV萌宠TV", "北京淘萌宠"]),
        ("魅力足球", ["上海魅力足球"]),
        ("睛彩青少", ["睛彩羽毛球"]),
        ("星空卫视", ["星空衛視", "星空衛视", "星空衛視"]),
        ("ChannelV", ["CHANNEL-V", "Channel[V]"]),
        ("凤凰卫视中文台", ["凤凰中文", "凤凰中文台", "凤凰卫视中文", "凤凰卫视"]),
        ("凤凰卫视香港台", ["凤凰香港台", "凤凰卫视香港", "凤凰香港"]),
        ("凤凰卫视资讯台", ["凤凰资讯", "凤凰资讯台", "凤凰咨询", "凤凰咨询台", "凤凰卫视咨询台", "凤凰卫视资讯", "凤凰卫视咨询"]),
        ("凤凰卫视电影台", ["凤凰电影", "凤凰电影台", "凤凰卫视电影", "鳳凰衛視電影台", "凤凰电影"]),
        ("乐游", ["乐游频道", "上海乐游频道", "乐游纪实", "SiTV乐游频道", "乐游高清"]),
        ("欢笑剧场", ["上海欢笑剧场4K", "欢笑剧场 4K", "欢笑剧场高清", "上海欢笑剧场"]),
        ("生活时尚", ["生活时尚4K", "SiTV生活时尚", "上海生活时尚", "生活时尚高清"]),
        ("都市剧场", ["都市剧场4K", "SiTV都市剧场", "上海都市剧场", "都市剧场高清"]),
        ("游戏风云", ["游戏风云4K", "SiTV游戏风云", "上海游戏风云", "游戏风云高清"]),
        ("金色学堂", ["金色学堂4K", "SiTV金色学堂", "上海金色学堂", "金色学堂高清"]),
        ("动漫秀场", ["动漫秀场4K", "SiTV动漫秀场", "上海动漫秀场", "动漫秀场高清"]),
        ("卡酷少儿", ["北京KAKU少儿", "BRTV卡酷少儿", "北京卡酷少儿", "卡酷动画", "北京卡通", "北京少儿"]),
        ("哈哈炫动", ["炫动卡通", "上海哈哈炫动"]),
        ("优漫卡通", ["江苏优漫卡通", "优漫漫画"]),
        ("金鹰卡通", ["湖南金鹰卡通"]),
        ("嘉佳卡通", ["佳佳卡通"])
    ])),
    ("third_party_urls", DEFAULT_THIRD_PARTY_URLS),
    ("settings", OrderedDict([
        ("FFMPEG_MAX_DETECT_TIME", 40),
        ("FFMPEG_TEST_DURATION", 15),
        ("RESPONSE_TIME_THRESHOLD", 10),
        ("STREAM_STABLE_THRESHOLD", 0.5),
        ("schedules", [
            {"id": 1, "time": "09:00", "enabled": True},
            {"id": 2, "time": "12:00", "enabled": True},
            {"id": 3, "time": "18:00", "enabled": True}
        ])
    ]))
])


class UpdateLock:
    def __init__(self):
        self.lock_file = "/tmp/iptv_update.lock"
        self.timeout = 1800
        self.lock_fd = None
        
    def acquire(self, wait=True):
        try:
            self.lock_fd = open(self.lock_file, 'w')
            try:
                with open(self.lock_file, 'r') as f:
                    try:
                        lock_info = json.load(f)
                        lock_time = lock_info.get('time', 0)
                        if time.time() - lock_time > self.timeout:
                            self.lock_fd.close()
                            os.remove(self.lock_file)
                            self.lock_fd = open(self.lock_file, 'w')
                    except:
                        pass
            except:
                pass
            
            try:
                if wait:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
                else:
                    try:
                        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self.lock_fd.close()
                        return False
            except AttributeError:
                import errno
                try:
                    os.open(self.lock_file + '.pid', os.O_CREAT | os.O_EXCL | os.O_RDWR)
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        if wait:
                            time.sleep(5)
                            return self.acquire(wait)
                        else:
                            return False
                    raise
            
            lock_info = {
                'pid': os.getpid(),
                'time': time.time(),
                'hostname': socket.gethostname() if hasattr(socket, 'gethostname') else 'unknown'
            }
            self.lock_fd.seek(0)
            self.lock_fd.truncate()
            json.dump(lock_info, self.lock_fd)
            self.lock_fd.flush()
            return True
            
        except Exception as e:
            if self.lock_fd:
                try:
                    self.lock_fd.close()
                except:
                    pass
            return False
    
    def release(self):
        if self.lock_fd:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                self.lock_fd.close()
            except:
                try:
                    self.lock_fd.close()
                except:
                    pass
            finally:
                try:
                    os.remove(self.lock_file)
                except:
                    pass


class OrderedJSONEncoder(json.JSONEncoder):
    def encode(self, obj):
        if isinstance(obj, OrderedDict):
            processed_obj = OrderedDict()
            for key, value in obj.items():
                if isinstance(value, OrderedDict):
                    processed_obj[key] = OrderedDict(value)
                else:
                    processed_obj[key] = value
            return super().encode(dict(processed_obj))
        elif isinstance(obj, list):
            return super().encode([dict(item) if isinstance(item, OrderedDict) else item for item in obj])
        return super().encode(obj)
    
    def iterencode(self, o, _one_shot=False):
        return super().iterencode(o, _one_shot)


def ordered_json_loads(json_str):
    def object_pairs_hook(pairs):
        obj = OrderedDict()
        order = None
        regular_items = []
        for key, value in pairs:
            if key == "_order" and isinstance(value, list):
                order = value
            else:
                regular_items.append((key, value))
        if order:
            for key in order:
                for k, v in regular_items:
                    if k == key:
                        obj[key] = v
                        break
            for key, value in regular_items:
                if key not in obj:
                    obj[key] = value
        else:
            for key, value in regular_items:
                obj[key] = value
        for key, value in obj.items():
            if isinstance(value, dict) and "_order" in value:
                nested_order = value["_order"]
                nested_obj = OrderedDict()
                for nested_key in nested_order:
                    if nested_key in value and nested_key != "_order":
                        nested_obj[nested_key] = value[nested_key]
                obj[key] = nested_obj
        return obj
    try:
        return json.loads(json_str, object_pairs_hook=object_pairs_hook)
    except json.JSONDecodeError:
        return json.loads(json_str, object_pairs_hook=OrderedDict)


def load_config():
    config_dir = os.path.dirname(CONFIG_FILE)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content or content == "{}":
                save_config(DEFAULT_CONFIG)
                return DEFAULT_CONFIG.copy()
    except Exception:
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                save_config(DEFAULT_CONFIG)
                return DEFAULT_CONFIG.copy()
            config_data = ordered_json_loads(content)
        
        if not isinstance(config_data, OrderedDict):
            config_data = OrderedDict(config_data)

        need_save = False
        
        for key, value in DEFAULT_CONFIG.items():
            if key not in config_data or not config_data[key]:
                config_data[key] = value.copy() if hasattr(value, 'copy') else value
                need_save = True
        
        if "categories" not in config_data:
            config_data["categories"] = DEFAULT_CONFIG["categories"].copy()
            need_save = True
        elif not isinstance(config_data["categories"], OrderedDict):
            categories = config_data["categories"]
            new_categories = OrderedDict()
            default_order = list(DEFAULT_CONFIG["categories"].keys())
            for cat_name in default_order:
                if cat_name in categories:
                    new_categories[cat_name] = categories[cat_name]
            for cat_name, channels in categories.items():
                if cat_name not in new_categories:
                    new_categories[cat_name] = channels
            config_data["categories"] = new_categories
            need_save = True
        
        if "mapping" not in config_data:
            config_data["mapping"] = DEFAULT_CONFIG["mapping"].copy()
            need_save = True
        elif not isinstance(config_data["mapping"], OrderedDict):
            config_data["mapping"] = OrderedDict(config_data["mapping"])
            need_save = True
        
        if "third_party_urls" not in config_data or not config_data["third_party_urls"]:
            config_data["third_party_urls"] = DEFAULT_CONFIG["third_party_urls"].copy()
            need_save = True
        elif not isinstance(config_data["third_party_urls"], OrderedDict):
            config_data["third_party_urls"] = OrderedDict(config_data["third_party_urls"])
            need_save = True
        
        if "settings" not in config_data:
            config_data["settings"] = DEFAULT_CONFIG["settings"].copy()
            need_save = True
        elif not isinstance(config_data["settings"], OrderedDict):
            config_data["settings"] = OrderedDict(config_data["settings"])
            need_save = True
        else:
            settings = config_data["settings"]
            if "schedules" not in settings or not isinstance(settings.get("schedules"), list):
                settings["schedules"] = DEFAULT_CONFIG["settings"]["schedules"]
                need_save = True
            for key in ["FFMPEG_MAX_DETECT_TIME", "FFMPEG_TEST_DURATION", 
                      "RESPONSE_TIME_THRESHOLD", "STREAM_STABLE_THRESHOLD"]:
                if key not in settings:
                    settings[key] = DEFAULT_CONFIG["settings"][key]
                    need_save = True

        if need_save:
            save_config(config_data)

        return config_data
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(config):
    try:
        if not isinstance(config, OrderedDict):
            config = OrderedDict(config)
        
        def clean_json(obj):
            if isinstance(obj, dict):
                return {k: clean_json(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [clean_json(item) for item in obj]
            else:
                return obj
        
        cleaned_config = OrderedDict()
        for key in ["categories", "mapping", "third_party_urls", "settings"]:
            if key in config:
                cleaned_config[key] = clean_json(config[key])
            else:
                cleaned_config[key] = config.get(key, OrderedDict())
        
        config_dir = os.path.dirname(CONFIG_FILE)
        if config_dir and not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)
        
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(
                cleaned_config, 
                f, 
                cls=OrderedJSONEncoder, 
                indent=2, 
                ensure_ascii=False,
                separators=(',', ': ')
            )
        
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                content = f.read()
                json.loads(content)
        except json.JSONDecodeError:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f2:
                import json as simple_json
                simple_json.dump(
                    dict(config),
                    f2,
                    indent=2,
                    ensure_ascii=False
                )
        
        return True
    except Exception:
        return False


def get_isp_from_api(data):
    isp_raw = (data.get("isp") or "").lower()
    if any(keyword in isp_raw for keyword in ["telecom", "ct", "chinatelecom"]):
        return "电信"
    elif any(keyword in isp_raw for keyword in ["unicom", "cu", "chinaunicom"]):
        return "联通"
    elif any(keyword in isp_raw for keyword in ["mobile", "cm", "chinamobile"]):
        return "移动"
    return "未知"


def get_isp_by_regex(ip):
    telecom_segments = r"^(1[0-9]{2}|2[0-3]{2}|27|42|43|58|59|60|61|110|111|112|113|114|115|116|117|118|119|120|121|122|123|124|125|126|127|133|149|153|173|177|180|181|189|190|191|193|199)"
    unicom_segments = r"^(42|43|58|59|60|61|110|111|112|113|114|115|116|117|118|119|120|121|122|123|124|125|126|127|130|131|132|166|175|176|185|186|196)"
    mobile_segments = r"^((223|36|37|38|39|100|101|102|103|104|105|106|107|108|109|134|135|136|137|138|139|150|151|152|157|158|159|170|178|182|183|184|187|188|192|195|197|198))"

    if re.match(telecom_segments, ip):
        return "电信"
    elif re.match(unicom_segments, ip):
        return "联通"
    elif re.match(mobile_segments, ip):
        return "移动"
    return "未知"


def first_stage():
    os.makedirs(IP_DIR, exist_ok=True)
    all_ips = set()

    config = load_config()
    THIRD_PARTY_URLS = config.get("third_party_urls", DEFAULT_THIRD_PARTY_URLS)
    
    if not THIRD_PARTY_URLS or len(THIRD_PARTY_URLS) == 0:
        THIRD_PARTY_URLS = DEFAULT_THIRD_PARTY_URLS.copy()
        config["third_party_urls"] = THIRD_PARTY_URLS
        save_config(config)
    
    for url, filename in THIRD_PARTY_URLS.items():
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            pattern = r'http[s]?://(([^/:]+(:\d+)?|[0-9]{1,3}(\.[0-9]{1,3}){3}(:\d+)?))[^"\'\s<>]*'
            matches = re.findall(pattern, r.text)
            
            for match in matches:
                addr = match[0].strip()
                if addr:
                    all_ips.add(addr)
                
        except Exception:
            pass
        time.sleep(3)
    
    if len(all_ips) == 0:
        return

    province_isp_dict = {}
    
    for ip_port in all_ips:
        try:
            time.sleep(0.1)
            host = ip_port.split(":")[0]
            is_ip = re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host)
            if not is_ip:
                try:
                    resolved_ip = socket.gethostbyname(host)
                    ip = resolved_ip
                except Exception:
                    continue
            else:
                ip = host

            try:
                res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=10)
                if res.status_code != 200:
                    continue
                    
                data = res.json()
                if data.get("status") != "success":
                    continue

                province = data.get("regionName", "未知")
                province = province.replace("省", "").replace("市", "").replace("自治区", "")

                isp = get_isp_from_api(data)
                if isp == "未知":
                    isp = get_isp_by_regex(ip)

                if isp == "未知":
                    continue

                fname = f"{province}{isp}.txt"
                province_isp_dict.setdefault(fname, set()).add(ip_port)
                
            except Exception:
                continue

        except Exception:
            continue

    for filename, ip_set in province_isp_dict.items():
        path = os.path.join(IP_DIR, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                for ip_port in sorted(ip_set):
                    f.write(ip_port + "\n")
        except Exception:
            pass
    return


def second_stage_in_memory():
    combined_lines = []
    if not os.path.exists(IP_DIR) or not os.path.exists(RTP_DIR):
        return combined_lines

    config = load_config()
    mapping = config.get("mapping", {})
    alias_map = {}
    for main_name, aliases in mapping.items():
        for alias in aliases:
            alias_map[alias] = main_name

    for ip_file in os.listdir(IP_DIR):
        if not ip_file.endswith(".txt"):
            continue
        ip_path = os.path.join(IP_DIR, ip_file)
        rtp_path = os.path.join(RTP_DIR, ip_file)
        
        if not os.path.exists(rtp_path):
            continue

        try:
            with open(ip_path, encoding="utf-8") as f1, open(rtp_path, encoding="utf-8") as f2:
                ip_lines = [x.strip() for x in f1 if x.strip()]
                rtp_lines = [x.strip() for x in f2 if x.strip()]

            for ip_port in ip_lines:
                for rtp_line in rtp_lines:
                    if "," not in rtp_line:
                        continue
                    ch_name, rtp_url = rtp_line.strip().split(",", 1)

                    ch_main = alias_map.get(ch_name, ch_name)

                    if "rtp://" in rtp_url:
                        part = rtp_url.split("rtp://", 1)[1]
                        full_url = f"{ch_main},http://{ip_port}/rtp/{part}"
                    elif "udp://" in rtp_url:
                        part = rtp_url.split("udp://", 1)[1]
                        full_url = f"{ch_main},http://{ip_port}/udp/{part}"
                    else:
                        continue
                    combined_lines.append(full_url)

        except Exception:
            continue

    unique_lines = {}
    for line in combined_lines:
        if "," not in line:
            continue
        ch_main, url = line.split(",", 1)
        if url not in unique_lines:
            unique_lines[url] = line
    
    return list(unique_lines.values())


def check_stream_speed(url, timeout=5):
    try:
        start_time = time.time()
        
        with requests.get(url, stream=True, timeout=timeout, headers=HEADERS, verify=False) as r:
            if r.status_code != 200:
                return False, float('inf')
            
            first_chunk = next(r.iter_content(chunk_size=188), None)
            if not first_chunk or len(first_chunk) < 188:
                return False, float('inf')
            
            target_size = 512 * 1024 
            downloaded = len(first_chunk)
            
            for chunk in r.iter_content(chunk_size=4096):
                downloaded += len(chunk)
                if downloaded >= target_size:
                    break
                if time.time() - start_time > timeout:
                    return False, float('inf')

            duration = time.time() - start_time
            if duration <= 0: 
                duration = 0.01
            
            speed_kb = (downloaded / 1024) / duration
            
            if speed_kb < 300: 
                return False, float('inf')

            return True, int(duration * 1000)

    except Exception:
        return False, float('inf')


def detect_ip_channels(ip_port, entries):
    valid_channels = {}
    response_times = []
    
    priority_candidates = []
    other_candidates = []
    
    for ch_main, url in entries:
        valid_channels[url] = ch_main
        if "CCTV" in ch_main or "卫视" in ch_main:
            priority_candidates.append(url)
        else:
            other_candidates.append(url)
            
    test_urls = priority_candidates[:2]
    if len(test_urls) < 2 and other_candidates:
        test_urls.extend(other_candidates[:2-len(test_urls)])
    
    if not test_urls and entries:
        test_urls.append(entries[0][1])
    
    success_count = 0
    
    for url in test_urls:
        is_valid, duration = check_stream_speed(url, timeout=3)
        if is_valid:
            success_count += 1
            response_times.append(duration)
            if duration < 500:
                break
    
    if success_count > 0:
        avg_time = sum(response_times) / len(response_times)
        return ip_port, True, avg_time, valid_channels
    
    return ip_port, False, float('inf'), {}


def third_stage_enhanced():
    lines = second_stage_in_memory()
    
    if not lines:
        return
    
    config = load_config()
    categories_config = config.get("categories", {})
    
    groups = {}
    for line in lines:
        if "," not in line:
            continue
        ch_main, url = line.strip().split(",", 1)
        m = re.match(r"http://(\d+\.\d+\.\d+\.\d+:\d+)/", url)
        if m:
            ip_port = m.group(1)
            groups.setdefault(ip_port, []).append((ch_main, url))

    valid_results = []
    max_workers = 10
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(detect_ip_channels, ip, chs): ip for ip, chs in groups.items()}
        
        for future in concurrent.futures.as_completed(futures):
            try:
                ip_port, ip_is_valid, avg_time, valid_channels = future.result()
                if ip_is_valid:
                    valid_results.append({
                        'time': avg_time,
                        'channels': valid_channels
                    })
            except Exception:
                continue

    valid_results.sort(key=lambda x: x['time'])

    channel_urls = OrderedDict()
    
    for item in valid_results:
        for url, ch_main in item['channels'].items():
            if ch_main not in channel_urls:
                channel_urls[ch_main] = []
            channel_urls[ch_main].append(url)

    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            now_str = datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"更新时间,#genre#\n")
            f.write(f"{now_str},url\n")
            
            for category, channel_list in categories_config.items():
                f.write(f"\n{category},#genre#\n")
                for channel_name in channel_list:
                    if channel_name in channel_urls:
                        for url in channel_urls[channel_name]:
                            f.write(f"{channel_name},{url}\n")
            
        if CONFIG_FILE and os.path.dirname(CONFIG_FILE):
            config_output = os.path.join(os.path.dirname(CONFIG_FILE), "IPTV.txt")
            try:
                shutil.copy2(OUTPUT_FILE, config_output)
            except Exception:
                pass
                
    except Exception:
        pass


def init_environment():
    for dir_path in [IP_DIR, RTP_DIR, WEB_DIR]:
        os.makedirs(dir_path, exist_ok=True)
    
    config = load_config()
    
    if not config.get("third_party_urls"):
        config["third_party_urls"] = DEFAULT_THIRD_PARTY_URLS.copy()
        save_config(config)
    
    return config


def run_update(force=False, wait_for_lock=True):
    try:
        lock = UpdateLock()
        
        if not lock.acquire(wait=wait_for_lock):
            if wait_for_lock:
                time.sleep(10)
                return run_update(force, wait_for_lock)
            else:
                return 0
        
        try:
            start_time = time.time()
            
            config = init_environment()
            
            first_stage()
            second_stage_in_memory()
            third_stage_enhanced()
            
            save_config(config)
            
            end_time = time.time()
            
            return 0
            
        finally:
            lock.release()
            
    except Exception:
        return 1


def main():
    parser = argparse.ArgumentParser(description='IPTV采集工具')
    parser.add_argument('--manual', action='store_true',
                       help='手动更新模式（会等待锁）')
    parser.add_argument('--no-wait', action='store_true',
                       help='不等待锁（用于定时任务）')
    args = parser.parse_args()
    
    if args.manual:
        return run_update(force=True, wait_for_lock=not args.no_wait)
    else:
        return run_update(force=False, wait_for_lock=False)


if __name__ == "__main__":
    sys.exit(main())
