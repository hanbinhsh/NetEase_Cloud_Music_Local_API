import os
import json
import sqlite3
import pymem
import pymem.process
import time
import requests
import re
import threading
import ctypes
from flask import Flask, Response
from flask_cors import CORS
from urllib.parse import quote
import uiautomation as auto

# ===========================
# 全局状态存储
# ===========================
API_STATE = {
    "playing": False,
    "process_active": False,
    "basic_info": {
        "id": 0,
        "name": "",
        "artist": "",
        "album": "",
        "cover_url": "",
        "duration": 0
    },
    "playback": {
        "current_sec": 0.0,
        "total_sec": 0.0,
        "percentage": 0.0,
        "formatted_current": "00:00",
        "formatted_total": "00:00"
    },
    "lyrics": {
        "current_line": "", 
        "current_trans": "",
        "all_lyrics": []     
    },
    "db_info": {}            
}

state_lock = threading.Lock()

# ===========================
# 0. 辅助工具：内存读取与搜索
# ===========================
class MemoryUtils:
    @staticmethod
    def read_pointer_chain_string(pm, base_addr, static_offset, offsets):
        """
        读取多级指针链 (最终目标是 String 类型的 ID)
        """
        try:
            # 1. 读取基址入口 (64位)
            # pm.read_longlong 读取 8 字节地址
            addr = pm.read_longlong(base_addr + static_offset)
            
            # 2. 遍历中间偏移 (64位)
            for offset in offsets[:-1]:
                if addr == 0: return None
                addr = pm.read_longlong(addr + offset)
            
            if addr == 0: return None
            
            # 3. 计算最终数据的内存地址
            final_addr = addr + offsets[-1]
            
            # 4. 读取字符串数据
            # 我们读 64 字节，足够覆盖 ID_TIMESTAMP 这种格式
            raw_bytes = pm.read_bytes(final_addr, 64)
            
            # 解码并清洗 (处理 C 风格字符串截断)
            try:
                # 找到 \x00 截断
                null_idx = raw_bytes.find(b'\x00')
                if null_idx != -1:
                    raw_bytes = raw_bytes[:null_idx]
                
                text = raw_bytes.decode('utf-8', errors='ignore')
                
                # 5. 解析格式 "ID_XXXXXX"
                if '_' in text:
                    id_str = text.split('_')[0]
                    # 确保提取出来的是数字
                    if id_str.isdigit():
                        return int(id_str)
                elif text.isdigit():
                    # 只有纯数字的情况
                    return int(text)
                    
            except:
                pass

            return None
            
        except Exception:
            return None

class WindowUtils:
    @staticmethod
    def get_netease_window_title():
        """
        遍历所有窗口，找到真正的网易云音乐播放标题。
        """
        results = []
        
        def enum_window_callback(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            length = 256
            buff = ctypes.create_unicode_buffer(length)
            ctypes.windll.user32.GetClassNameW(hwnd, buff, length)
            class_name = buff.value
            if "OrpheusBrowserHost" in class_name:
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value
                    if title:
                        results.append(title)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        try:
            ctypes.windll.user32.EnumWindows(WNDENUMPROC(enum_window_callback), 0)
        except: pass

        best_title = None
        for t in results:
            if t in ["网易云音乐", "桌面歌词", "精简模式", "Mini模式"]: continue
            if " - " in t: return t.strip()
            best_title = t
        return best_title

class SearchService:
    @staticmethod
    def search_song_by_title(title_str, duration_sec):
        if not title_str: return None
        
        target_song_name = ""
        target_artists = []
        
        clean_title_str = title_str.replace(" - 网易云音乐", "").strip()
        search_keyword = ""
        
        if " - " in clean_title_str:
            parts = clean_title_str.rsplit(" - ", 1)
            target_song_name = parts[0].strip()
            artist_part_str = parts[1].strip()
            target_artists = [a.strip().lower() for a in artist_part_str.split("/")]
            # 优化策略：只搜第一位歌手
            primary_artist = artist_part_str.split("/")[0].strip()
            search_keyword = f"{target_song_name} {primary_artist}"
        else:
            target_song_name = clean_title_str
            target_artists = []
            search_keyword = clean_title_str

        target_ms = duration_sec * 1000
        
        print(f"\n[DEBUG] 🔎 搜索: [{search_keyword}]") 
        
        try:
            url = "http://music.163.com/api/cloudsearch/pc"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': 'http://music.163.com/',
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            data = {'s': search_keyword, 'type': 1, 'offset': 0, 'limit': 10, 'total': 'true'}
            
            resp = requests.post(url, data=data, headers=headers, timeout=5)
            if resp.status_code != 200: return None
            
            try: resp_json = resp.json()
            except: return None
            
            songs = resp_json.get('result', {}).get('songs', [])
            if not songs: return None
            
            best_match = None
            min_duration_diff = 99999999 
            
            for i, song in enumerate(songs):
                s_name = song.get('name', '')
                s_dt = song.get('dt') or song.get('duration', 0)
                s_artists_list = song.get('ar') or song.get('artists', [])
                s_artist_names = [a.get('name', '').lower() for a in s_artists_list]
                
                diff = abs(s_dt - target_ms)
                
                is_artist_match = False
                if not target_artists:
                    is_artist_match = True 
                else:
                    for ta in target_artists:
                        for sa in s_artist_names:
                            if ta in sa or sa in ta: 
                                is_artist_match = True
                                break
                        if is_artist_match: break
                
                is_name_match = (target_song_name.lower() in s_name.lower()) or (s_name.lower() in target_song_name.lower())

                if is_artist_match and is_name_match and diff < 3000:
                    if diff < min_duration_diff:
                        min_duration_diff = diff
                        best_match = song
            
            if best_match:
                print(f"[DEBUG] ✅ 搜索匹配成功: {best_match['name']}")
                return SearchService._format_song(best_match)
            else:
                return None

        except Exception as e:
            print(f"[DEBUG] Search Error: {e}")
            return None

    @staticmethod
    def _format_song(song_data):
        return {
            "id": song_data['id'],
            "name": song_data['name'],
            "duration": song_data.get('dt') or song_data.get('duration'),
            "artists": song_data.get('ar') or song_data.get('artists', []),
            "album": song_data.get('al') or song_data.get('album', {})
        }
    
class PlayModeService:
    def __init__(self):
        self.window = None
        self.control_bar = None
        self.mode_map = {
            "loop": "list",
            "singleloop": "single",
            "shuffle": "random",
            "order": "order"
        }
        self.current_mode = "list"

    def _get_handles(self):
        """重新连接网易云窗口并定位控制栏锚点"""
        try:
            self.window = auto.WindowControl(searchDepth=1, ClassName="OrpheusBrowserHost")
            if not self.window.Exists(0): return False
            for ui_name in self.mode_map.keys():
                target_btn = self.window.ButtonControl(searchDepth=15, Name=ui_name)
                if target_btn.Exists(0.1):
                    self.control_bar = target_btn.GetParentControl()
                    return True
        except: pass
        return False

    def get_mode(self):
        """获取当前模式，包含自动重连逻辑"""
        try:
            if not self.window or not self.window.Exists(0):
                self._get_handles()
                return self.current_mode

            found_key = None
            if self.control_bar and self.control_bar.Exists(0):
                for child in self.control_bar.GetChildren():
                    if child.Name in self.mode_map:
                        found_key = child.Name
                        break
            
            if not found_key: # 缓存失效重试
                if self._get_handles(): return self.get_mode()
            
            if found_key: self.current_mode = self.mode_map[found_key]
        except Exception: # 捕获 UI 重绘导致的句柄失效
            self.window = None
            self.control_bar = None
        return self.current_mode

# ===========================
# 1. 数据库服务
# ===========================
class NeteaseV3Service:
    def __init__(self):
        self.user_home = os.path.expanduser("~")
        self.db_path = os.path.join(self.user_home, r"AppData\Local\NetEase\CloudMusic\Library\webdb.dat")
        self.last_db_playtime = 0
        self.last_file_mtime = 0
        self.current_full_data = None 

    def check_db_update(self):
        """检查数据库文件是否有更新 (同时检查 .dat 和 .dat-wal)"""
        try:
            if not os.path.exists(self.db_path): return False
            
            current_mtime = os.path.getmtime(self.db_path)
            wal_path = self.db_path + "-wal"
            if os.path.exists(wal_path):
                wal_mtime = os.path.getmtime(wal_path)
                current_mtime = max(current_mtime, wal_mtime)

            if current_mtime != self.last_file_mtime:
                self.last_file_mtime = current_mtime
                return True
        except: pass
        return False

    def _create_ro_connection(self):
        """辅助方法：创建只读数据库连接"""
        if not os.path.exists(self.db_path): return None
        try:
            # 构造只读 URI
            safe_path = quote(self.db_path.replace('\\', '/'))
            db_uri = f"file:{safe_path}?mode=ro"
            return sqlite3.connect(db_uri, uri=True, timeout=1)
        except:
            return None

    def _read_db_query(self, sql, params=()):
        """内部通用查询方法 (基础版，不带JSON解析)"""
        result = []
        conn = None
        try:
            conn = self._create_ro_connection()
            if not conn: return []
            
            cursor = conn.cursor()
            cursor.execute(sql, params)
            result = cursor.fetchall()
            
        except sqlite3.OperationalError:
            pass # 忽略锁错误
        except Exception as e:
            print(f"[DB Error] {e}")
        finally:
            if conn: conn.close()
        return result

    def _get_all_raw_data(self, table_name, limit=None, order_by=None):
        """
        通用获取数据方法 (优化版)
        1. 使用只读连接 (不复制文件)
        2. 自动遍历字段，解析 JSON 字符串
        """
        result_list = []
        conn = None
        try:
            conn = self._create_ro_connection()
            if not conn: return []
            
            # 设置 row_factory 以便能像字典一样访问列名
            conn.row_factory = sqlite3.Row 
            cursor = conn.cursor()
            
            # 动态构建 SQL
            sql = f"SELECT * FROM {table_name}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            if limit:
                sql += f" LIMIT {limit}"
                
            cursor.execute(sql)
            rows = cursor.fetchall()
            
            for row in rows:
                # 将 sqlite3.Row 对象转为标准 Python 字典
                row_dict = dict(row)
                
                # === 自动 JSON 解析逻辑 (保留原逻辑) ===
                # 遍历字典，寻找看起来像 JSON 的字符串
                for key, val in list(row_dict.items()):
                    if isinstance(val, str) and len(val) > 1 and (val.startswith('{') or val.startswith('[')):
                        try:
                            parsed_data = json.loads(val)
                            # 存入新字段，后缀 _parsed
                            row_dict[f"{key}_parsed"] = parsed_data
                        except:
                            pass
                
                result_list.append(row_dict)
                
        except Exception as e:
            print(f"[DB Error {table_name}] {e}")
        finally:
            if conn: conn.close()
            
        return result_list

    def get_latest_track(self):
        """获取最后一条播放记录"""
        rows = self._read_db_query("SELECT jsonStr FROM historyTracks ORDER BY playtime DESC LIMIT 1")
        if rows:
            try:
                data = json.loads(rows[0][0])
                self.current_full_data = data
                return data
            except: pass
        return self.current_full_data

    def search_db_for_id(self, target_id):
        """遍历本地数据库查找指定 ID"""
        rows = self._read_db_query("SELECT jsonStr FROM historyTracks ORDER BY playtime DESC LIMIT 100")
        for row in rows:
            try:
                data = json.loads(row[0])
                if int(data.get('id', 0)) == int(target_id):
                    return data
            except: continue
        return None

    def get_track_hybrid(self, memory_duration):
        """
        混合获取：当内存指针失效时使用
        尝试 数据库 -> 失败则 -> 窗口标题搜索
        """
        # 1. 尝试读数据库 (只读直连)
        rows = self._read_db_query("SELECT jsonStr FROM historyTracks ORDER BY playtime DESC LIMIT 1")
        if rows:
            try:
                data = json.loads(rows[0][0])
                # 简单校验：如果数据库里的时长和内存里的时长差距 < 3秒，认为是同一首
                # 注意：数据库里的 duration 是毫秒
                db_duration_ms = data.get('duration', 0)
                mem_duration_ms = memory_duration * 1000
                
                if abs(db_duration_ms - mem_duration_ms) < 3000:
                    print(" -> [降级] 数据库命中 (时长匹配)")
                    return data
            except: pass

        # 2. 数据库没命中（可能是切歌了但文件还没写），尝试搜索
        # 需要获取窗口标题
        title = WindowUtils.get_netease_window_title()
        if title and title != "网易云音乐":
            print(f"[降级搜索] 内存指针失效且DB未更。标题: {title}")
            search_data = SearchService.search_song_by_title(title, memory_duration)
            if search_data:
                return search_data

        return None

    def get_song_detail_by_id(self, song_id):
        """API 获取详情"""
        try:
            url = f"http://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': 'http://music.163.com/'
            }
            resp = requests.get(url, headers=headers, timeout=3)
            data = resp.json()
            songs = data.get('songs', [])
            if songs:
                song = songs[0]
                return {
                    "id": song['id'],
                    "name": song['name'],
                    "duration": song['duration'],
                    "artists": song['artists'], 
                    "album": song['album']     
                }
        except Exception as e:
            print(f"[API] Get Detail Error: {e}")
        return None

    def get_history_list(self, limit=20):
        """获取最近播放历史 (自动解析 JSON)"""
        return self._get_all_raw_data("historyTracks", limit=limit, order_by="playtime DESC")

    def get_playlist_list(self):
        """获取用户歌单列表 (自动解析 JSON)"""
        return self._get_all_raw_data("web_user_playlist")
    
    def get_raw_playing_list(self):
        """
        获取原始的播放列表数据 (不做任何字段过滤)
        来源: webdata/file/playingList
        """
        # 1. 定位文件路径
        # 使用 os.environ['LOCALAPPDATA'] 能更准确地定位到 C:\Users\ASUS\AppData\Local
        file_path = os.path.join(
            os.environ['LOCALAPPDATA'], 
            r"Netease\CloudMusic\webdata\file\playingList"
        )
        
        if not os.path.exists(file_path):
            return []

        try:
            # 2. 读取文件
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: return []
                
                # 3. 解析 JSON
                root_data = json.loads(content)
                
                # 4. 直接返回 list 键对应的内容，不做任何处理
                if isinstance(root_data, dict) and 'list' in root_data:
                    return root_data['list']
                elif isinstance(root_data, list):
                    # 某些极老版本可能直接存的是 list，兼容一下
                    return root_data
                
                return []
                
        except Exception as e:
            print(f"[PlayingList Error] 读取失败: {e}")
            return []
        
    def get_playback_neighbors(self, current_id, mode):
        """根据当前模式和 playingList 文件预测上下曲"""
        raw_list = self.get_raw_playing_list()
        if not raw_list or not current_id: return {}, {}

        try:
            # 1. 处理单曲循环
            if mode == "single":
                curr_item = next((item for item in raw_list if str(item.get('id')) == str(current_id)), None)
                if curr_item:
                    song = self._format_neighbor(curr_item)
                    return song, song

            # 2. 确定排序逻辑：随机模式用 randomOrder，其余用 displayOrder
            sort_key = 'randomOrder' if mode == 'random' else 'displayOrder'
            sorted_list = sorted(raw_list, key=lambda x: x.get(sort_key, 0))

            # 3. 定位当前歌曲索引
            idx = -1
            for i, item in enumerate(sorted_list):
                if str(item.get('id')) == str(current_id):
                    idx = i
                    break
            
            if idx == -1: return {}, {}

            # 4. 计算循环索引
            l = len(sorted_list)
            prev_s = self._format_neighbor(sorted_list[(idx - 1) % l])
            next_s = self._format_neighbor(sorted_list[(idx + 1) % l])
            return prev_s, next_s
        except:
            return {}, {}

    def _format_neighbor(self, item):
        """内部辅助：格式化邻居歌曲信息"""
        t = item.get('track', {})
        ar = t.get('artists') or t.get('ar', [])
        al = t.get('album') or t.get('al', {})
        return {
            "id": t.get('id'),
            "name": t.get('name'),
            "artist": " / ".join([a.get('name') for a in ar]),
            "cover": al.get('picUrl', "")
        }

# ===========================
# 2. 歌词服务
# ===========================
class LyricService:
    def __init__(self):
        self.current_id = None
        self.is_loading = False
        self.parsed_list = [] 
        self.lyric_packet = {
            "id": 0, "hasLyric": False, "hasTrans": False, "hasRoma": False, "hasYrc": False,
            "lrc": "", "tlyric": "", "romalrc": "", "yrc": ""
        }

    def load_lyrics(self, song_id):
        if song_id == self.current_id: return
        self.current_id = song_id
        self.parsed_list = []
        self.lyric_packet = {k: (False if "has" in k else "") for k in self.lyric_packet}
        self.lyric_packet["id"] = song_id
        threading.Thread(target=self._fetch_lyrics, args=(song_id,), daemon=True).start()

    def _parse_lrc_text(self, lrc_content):
        """解析标准 LRC 用于内部计时 (兼容 [mm:ss:xx] 格式)"""
        res = {}
        if not lrc_content: return res
        
        # 【关键修改】正则兼容冒号和点号作为毫秒分隔符
        # 匹配: [00:00] 或 [00:00.00] 或 [00:00:00]
        pattern = re.compile(r'\[(\d{2}):(\d{2})(?:[\.:](\d+))?\](.*)')
        
        for line in lrc_content.split('\n'):
            match = pattern.search(line)
            if match:
                min_str = match.group(1)
                sec_str = match.group(2)
                ms_str = match.group(3) if match.group(3) else "0"
                content = match.group(4).strip()
                
                # 计算秒数
                # 注意：有些非标lrc的毫秒可能是2位也可能是3位，这里做简单处理
                # 如果 ms_str 是 "61"，它代表 0.61s
                if len(ms_str) == 2:
                    ms_val = int(ms_str) / 100.0
                elif len(ms_str) == 3:
                    ms_val = int(ms_str) / 1000.0
                else:
                    ms_val = 0.0
                
                t = int(min_str) * 60 + int(sec_str) + ms_val
                
                if content: res[t] = content
        return res

    def _fetch_lyrics(self, target_song_id):
        self.is_loading = True
        try:
            url = (
                f"http://music.163.com/api/song/lyric?id={target_song_id}"
                f"&cp=false&lv=0&kv=0&tv=0&rv=0&yv=0&ytv=0&yrv=0"
            )
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': 'http://music.163.com/'
            }
            resp = requests.get(url, headers=headers, timeout=5).json()
            if target_song_id != self.current_id: return

            raw_lrc = resp.get('lrc', {}).get('lyric', "")
            raw_trans = resp.get('tlyric', {}).get('lyric', "")
            raw_roma = resp.get('romalrc', {}).get('lyric', "")
            raw_yrc = resp.get('yrc', {}).get('lyric', "")
            if not raw_yrc: raw_yrc = resp.get('klyric', {}).get('lyric', "")

            with state_lock:
                self.lyric_packet = {
                    "id": target_song_id,
                    "hasLyric": bool(raw_lrc), "hasTrans": bool(raw_trans), "hasRoma": bool(raw_roma), "hasYrc": bool(raw_yrc),
                    "lrc": raw_lrc, "tlyric": raw_trans, "romalrc": raw_roma, "yrc": raw_yrc
                }

            ori_dict = self._parse_lrc_text(raw_lrc)
            trans_dict = self._parse_lrc_text(raw_trans)
            merged = []
            for t in sorted(ori_dict.keys()):
                merged.append({
                    "time": t, "text": ori_dict[t], "trans": trans_dict.get(t, "")
                })
            self.parsed_list = merged
                
        except Exception as e: print(f"Lyric Error: {e}")
        finally:
            if target_song_id == self.current_id: self.is_loading = False

    def get_current_line(self, current_time):
        if not self.parsed_list: return "", ""
        curr_ori, curr_trans = "", ""
        for item in self.parsed_list:
            if current_time >= item['time']:
                curr_ori = item['text']
                curr_trans = item['trans']
            else: break
        return curr_ori, curr_trans

    def get_full_packet(self):
        return self.lyric_packet
    
    def clear(self):
        self.current_id = None
        self.parsed_list = []
        self.lyric_packet = {k: (False if "has" in k else "") for k in self.lyric_packet}

# ===========================
# 3. 后台监控线程
# ===========================
def format_t(s):
    return f"{int(s//60):02}:{int(s%60):02}"

def monitor_loop(v3, lrc_svc):
    with auto.UIAutomationInitializerInThread():
        # === 内存指针配置 ===
        PTR_STATIC_OFFSET = 0x01DDE250
        PTR_OFFSETS = [0x10, 0, 0x10, 0x68, 0]
        
        OFF_CURR = 0x1D7E8F8
        OFF_TOTAL = 0x1DDEF58

        mode_svc = PlayModeService()
        
        pm = None
        base = None
        
        last_ct = -1.0
        last_tt = 0.0
        
        # 兼容旧逻辑变量
        last_switch_time = 0      
        is_waiting_stable = False 
        current_song_title_cache = ""
        last_title_check_time = 0
        
        # 内存ID记录
        last_memory_id = None

        print("启动后台监控线程...")

        while True:
            try:
                # 1. 进程连接
                if pm is None:
                    try:
                        pm = pymem.Pymem("cloudmusic.exe")
                        mod = pymem.process.module_from_name(pm.process_handle, "cloudmusic.dll")
                        base = mod.lpBaseOfDll
                        print("已连接到网易云音乐进程")
                        with state_lock: API_STATE['process_active'] = True
                    except:
                        with state_lock: 
                            API_STATE['process_active'] = False
                            API_STATE['playing'] = False
                        time.sleep(2)
                        continue

                # 2. 读取基础时间
                try:
                    ct = pm.read_double(base + OFF_CURR)
                    tt = pm.read_double(base + OFF_TOTAL)
                except:
                    pm = None
                    continue

                is_moving = (ct != last_ct)
                last_ct = ct
                
                # ==========================================
                # 4. 【核心优先】尝试直接读取内存 ID
                # ==========================================
                memory_id = MemoryUtils.read_pointer_chain_string(pm, base, PTR_STATIC_OFFSET, PTR_OFFSETS)
                
                current_track_full = None
                
                # === 分支 A: 内存读取成功 (高精度模式) ===
                if memory_id:
                    # 重置旧逻辑的状态，防止混合干扰
                    is_waiting_stable = False

                    # 只有当 ID 发生变化时，才去执行昂贵的查询操作
                    if memory_id != last_memory_id:
                        print(f"\n[内存] 检测到 ID 变更: {last_memory_id} -> {memory_id}")
                        last_memory_id = memory_id
                        
                        # 立即清空旧歌词
                        lrc_svc.clear()
                        with state_lock:
                            API_STATE['lyrics']['all_lyrics'] = []
                            API_STATE['lyrics']['current_line'] = "Loading..."
                        
                        # --- 策略：内存 -> 数据库 -> API ---
                        
                        # 1. 先查本地数据库 (最安全，0 网络请求)
                        print(f"[查询] 正在检索本地数据库 (ID={memory_id})...")
                        db_track = v3.search_db_for_id(memory_id)
                        
                        if db_track:
                            current_track_full = db_track
                            print(f" -> [命中] 本地数据库: {db_track['name']}")
                        else:
                            # 2. 数据库没有，才调用 API (最后手段)
                            print(f" -> [未命中] 本地无缓存，调用 API...")
                            api_track = v3.get_song_detail_by_id(memory_id)
                            
                            if api_track:
                                current_track_full = api_track
                                print(f" -> [成功] API 获取: {api_track['name']}")
                            else:
                                print(f" -> [失败] 无法获取歌曲详情")
                    
                    # 如果 ID 没变，但全局为空 (刚启动时)，补一次查询
                    elif API_STATE['basic_info']['id'] != memory_id:
                        db_track = v3.search_db_for_id(memory_id)
                        if db_track:
                            current_track_full = db_track
                        else:
                            current_track_full = v3.get_song_detail_by_id(memory_id)

                # === 分支 B: 内存读取失败 (降级模式) ===
                else:
                    # 如果 tt 无效，直接跳过
                    if tt < 1.0:
                        time.sleep(0.1)
                        continue
                    
                    is_switching = False
                    
                    # Trigger 1: 时长突变
                    if abs(tt - last_tt) > 1.0:
                        is_switching = True
                        print(f"[触发] 时长突变: {last_tt:.1f} -> {tt:.1f}")
                    # Trigger 2: 进度回跳
                    elif last_ct > 2.0 and ct < 1.0:
                        pass 
                    # Trigger 3: 主动标题轮询
                    if time.time() - last_title_check_time > 0.5:
                        last_title_check_time = time.time()
                        win_title = WindowUtils.get_netease_window_title()
                        if win_title:
                            clean_win_title = win_title.replace(" - 网易云音乐", "").strip()
                            if current_song_title_cache and clean_win_title != current_song_title_cache and not is_waiting_stable:
                                if " - " in clean_win_title:
                                    is_switching = True
                                    print(f"[触发] 标题变更: '{current_song_title_cache}' -> '{clean_win_title}'")
                    
                    if is_switching:
                        last_tt = tt
                        last_switch_time = time.time()
                        is_waiting_stable = True
                        lrc_svc.clear() 
                        with state_lock:
                            API_STATE['lyrics']['all_lyrics'] = []
                            API_STATE['lyrics']['current_line'] = "Loading..."

                    if is_waiting_stable:
                        time_diff = time.time() - last_switch_time
                        if time_diff > 1.2:
                            print(f"[防抖] 状态已稳定，同步新歌数据...")
                            is_waiting_stable = False 
                            
                            # 降级模式：尝试获取数据
                            # 1. 尝试读最新的库
                            # 2. 尝试搜标题
                            track = v3.get_track_hybrid(tt)
                            if track:
                                current_track_full = track
                                print(f" -> [降级成功] {track['name']}")
                            else:
                                print(" -> [降级失败] 无法同步数据")

                # ==========================================
                # 5. 更新全局状态
                # ==========================================
                if current_track_full:
                    song_name = current_track_full.get('name')
                    artists = current_track_full.get('artists') or current_track_full.get('ar', [])
                    all_artist_names = [a.get('name') for a in artists]
                    artist_display_str = " / ".join(all_artist_names) if all_artist_names else "未知歌手"

                    # 获取当前模式
                    current_mode = mode_svc.get_mode()

                    # 计算邻居歌曲
                    curr_id = API_STATE['basic_info']['id']
                    prev_track, next_track = {}, {}
                    if curr_id:
                    # 只有当 ID 存在且有效时才计算
                        prev_track, next_track = v3.get_playback_neighbors(curr_id, current_mode)
                    
                    # 更新缓存标题，防止降级逻辑死循环
                    current_song_title_cache = f"{song_name} - {'/'.join(all_artist_names)}"
                    
                    song_id = current_track_full.get("id")
                    
                    # 加载歌词
                    lrc_svc.load_lyrics(song_id)
                    
                    al_data = current_track_full.get("album") or current_track_full.get("al", {})
                    cover_url = al_data.get("picUrl", "")

                    with state_lock:
                        API_STATE['basic_info'] = {
                            "id": song_id,
                            "name": song_name,
                            "artist": artist_display_str,
                            "album": al_data.get("name", ""),
                            "cover_url": cover_url,
                            "duration": current_track_full.get("duration", 0)
                        }
                        API_STATE['db_info'] = current_track_full

                # 实时更新进度和歌词
                cur_txt, cur_trans = lrc_svc.get_current_line(ct)
                
                with state_lock:
                    API_STATE['playing'] = is_moving
                    API_STATE['playback'] = {
                        "current_sec": ct,
                        "total_sec": tt,
                        "percentage": (ct / tt * 100) if tt > 0 else 0,
                        "formatted_current": format_t(ct),
                        "formatted_total": format_t(tt),
                        "play_mode": current_mode,
                        "prev_song": prev_track,
                        "next_song": next_track
                    }
                    API_STATE['lyrics']['current_line'] = cur_txt
                    API_STATE['lyrics']['current_trans'] = cur_trans
                time.sleep(0.1)

            except Exception as e:
                print(f"Monitor Loop Error: {e}")
                time.sleep(1)

# ===========================
# 4. Flask Web Server
# ===========================
app = Flask(__name__)
CORS(app)

# 初始化服务实例
v3 = NeteaseV3Service()
lrc_svc = LyricService() # 注意：这里需要改为全局单例，或者在 monitor_loop 里引用同一个实例

# 【关键修改】为了让 Flask 和 monitor_loop 共享同一个 LyricService 实例
# 我们需要把 monitor_loop 里的 lrc_svc 提出来变成全局变量，或者像下面这样：
# 建议直接在文件最上方定义全局 lrc_service，然后在 monitor_loop 里使用 global lrc_service

@app.route('/info', methods=['GET'])
def get_info():
    with state_lock:
        # 这里不需要返回 all_lyrics 了，前端去调 /lyrics 接口拿
        # 我们只返回 playback, basic_info 和 lyrics.current_line
        
        # 构造一个轻量级的响应副本
        lite_state = {
            "playing": API_STATE["playing"],
            "process_active": API_STATE["process_active"],
            "basic_info": API_STATE["basic_info"],
            "playback": API_STATE["playback"],
            "lyrics": {
                "current_line": API_STATE["lyrics"]["current_line"],
                "current_trans": API_STATE["lyrics"]["current_trans"]
            }
        }
        return Response(json.dumps(lite_state, ensure_ascii=False), mimetype='application/json')

@app.route('/lyrics', methods=['GET'])
def get_lyrics():
    """【新增】专门获取歌词的接口"""
    # 直接从 lrc_service 获取最新的包
    data = lrc_svc.get_full_packet()
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')
    
@app.route('/history', methods=['GET'])
def get_history():
    data = v3.get_history_list(limit=20) 
    return Response(json.dumps({"code": 200, "data": data}, ensure_ascii=False), mimetype='application/json')

@app.route('/playlist', methods=['GET'])
def get_playlist():
    data = v3.get_playlist_list()
    return Response(json.dumps({"code": 200, "data": data}, ensure_ascii=False), mimetype='application/json')

@app.route('/queue', methods=['GET'])
def get_queue():
    """获取当前播放列表（原始数据）"""
    v3 = NeteaseV3Service()
    
    # 获取原始列表
    raw_data = v3.get_raw_playing_list()
    
    # 直接包装返回
    return Response(
        json.dumps({
            "code": 200, 
            "count": len(raw_data), 
            "data": raw_data  # 这里包含所有的 id, track, privilege, referInfo 等字段
        }, ensure_ascii=False), 
        mimetype='application/json'
    )

if __name__ == "__main__":
    # 在这里把全局的 service 传给 monitor
    t = threading.Thread(target=monitor_loop, args=(v3, lrc_svc), daemon=True)
    t.start()
    print(f"API 服务已启动: http://127.0.0.1:18726/info")
    app.run(host='0.0.0.0', port=18726, debug=False)