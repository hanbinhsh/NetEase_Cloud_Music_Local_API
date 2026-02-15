import os
import json
import sqlite3
import pymem
import pymem.process
import time
import shutil
import requests
import re
import threading
import ctypes
from flask import Flask, Response
from flask_cors import CORS

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
# 0. 辅助工具：窗口标题读取与搜索
# ===========================
class WindowUtils:
    @staticmethod
    def get_netease_window_title():
        """
        遍历所有窗口，找到真正的网易云音乐播放标题。
        解决 FindWindow 只返回第一个(可能是未更新的)窗口的问题。
        """
        results = []
        
        # 定义回调函数，用于收集所有窗口
        def enum_window_callback(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            
            # 1. 获取类名
            length = 256
            buff = ctypes.create_unicode_buffer(length)
            ctypes.windll.user32.GetClassNameW(hwnd, buff, length)
            class_name = buff.value
            
            # 网易云的类名通常是 OrpheusBrowserHost
            if "OrpheusBrowserHost" in class_name:
                # 2. 获取标题
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value
                    if title:
                        results.append(title)
            return True

        # 定义 C 函数原型
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        EnumWindows = ctypes.windll.user32.EnumWindows
        
        # 开始遍历
        try:
            EnumWindows(WNDENUMPROC(enum_window_callback), 0)
        except Exception as e:
            # 极少数情况下 ctypes 回调可能会报错，忽略
            pass

        # === 筛选逻辑 (核心) ===
        # 我们收集到了一堆标题，比如 ["网易云音乐", "桌面歌词", "晴天 - 周杰伦"]
        # 我们需要挑出那个真正的歌名
        
        best_title = None
        
        for t in results:
            # 黑名单过滤
            if t in ["网易云音乐", "桌面歌词", "精简模式", "Mini模式"]:
                continue
            
            # 只要包含 " - "，通常就是歌名 (例如 "晴天 - 周杰伦")
            if " - " in t:
                return t.strip()
            
            # 如果没有连字符，但不是黑名单里的词，暂存起来作为备选
            best_title = t
            
        # 如果遍历完了没找到带 "-" 的，就返回备选，实在不行返回 None
        return best_title

class SearchService:
    @staticmethod
    def search_song_by_title(title_str, duration_sec):
        if not title_str: return None
        
        full_title = title_str.replace(" - 网易云音乐", "").strip()
        # print(f"[DEBUG] 搜索: [{full_title}] | 目标时长: {duration_sec}s")
        
        try:
            url = "http://music.163.com/api/cloudsearch/pc"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': 'http://music.163.com/',
                'Content-Type': 'application/x-www-form-urlencoded'
            }
            data = {'s': full_title, 'type': 1, 'offset': 0, 'limit': 5, 'total': 'true'}
            
            resp = requests.post(url, data=data, headers=headers, timeout=3)
            if resp.status_code != 200: return None
            
            try: resp_json = resp.json()
            except: return None
            
            songs = resp_json.get('result', {}).get('songs', [])
            if not songs: return None
            
            # === 严格匹配逻辑 ===
            target_ms = duration_sec * 1000
            
            # 允许误差：3秒 (内存读取和API数据可能有微小差异)
            ALLOWED_DIFF = 3000 
            
            best_match = None
            min_diff = 99999999
            
            for song in songs:
                s_duration = song.get('dt') or song.get('duration', 0)
                diff = abs(s_duration - target_ms)
                
                # print(f"  > 候选: {song['name']} | 误差: {diff}ms")

                if diff < min_diff:
                    min_diff = diff
                    best_match = song
            
            # === 关键修改：只有误差在允许范围内才返回 ===
            # 如果最小误差都超过了 3秒，说明窗口标题搜出来的歌，根本不是当前播放的歌（时长对不上）
            # 这时候返回 None，让主循环去重试
            if best_match and min_diff < ALLOWED_DIFF:
                print(f"[DEBUG] ✅ 校验通过: {best_match['name']}")
                return SearchService._format_song(best_match)
            
            print(f"[DEBUG] ❌ 校验失败 (最小误差 {min_diff}ms > {ALLOWED_DIFF}ms)，标题可能滞后")
            return None
                
        except Exception:
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
        try:
            mtime = os.path.getmtime(self.db_path)
            if mtime != self.last_file_mtime:
                self.last_file_mtime = mtime
                return True
        except: pass
        return False

    def _read_db(self):
        """内部方法：底层读取数据库"""
        if not os.path.exists(self.db_path): return None, 0
        temp_db = "temp_webdb.dat"
        try:
            shutil.copy2(self.db_path, temp_db)
            if os.path.exists(self.db_path + "-wal"):
                shutil.copy2(self.db_path + "-wal", temp_db + "-wal")
            
            conn = sqlite3.connect(temp_db)
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("SELECT playtime, jsonStr FROM historyTracks ORDER BY playtime DESC LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            
            # 清理临时文件
            for f in [temp_db, temp_db + "-wal", temp_db + "-shm"]:
                if os.path.exists(f): 
                    try: os.remove(f)
                    except: pass
            
            if row:
                return json.loads(row[1]), row[0]
        except: pass
        return None, 0

    def get_latest_track(self):
        """
        【补回这个方法】
        常规获取：当检测到文件更新时调用
        """
        data, playtime = self._read_db()
        if data and playtime > self.last_db_playtime:
            self.last_db_playtime = playtime
            self.current_full_data = data
            return data
        return self.current_full_data

    def get_track_hybrid(self, memory_duration):
        """
        混合获取：当检测到切歌但文件未更新时调用
        尝试 数据库 -> 失败则 -> 窗口标题搜索
        """
        # 1. 尝试读数据库
        db_data, playtime = self._read_db()
        
        # 如果数据库有更新 (playtime 变大)
        if db_data and playtime > self.last_db_playtime:
            self.last_db_playtime = playtime
            self.current_full_data = db_data
            print(" -> [数据源] 本地数据库 (WebDB)")
            return db_data

        # 2. 搜索逻辑
        need_search = False
        if not self.current_full_data:
            need_search = True
        else:
            cached_ms = self.current_full_data.get('duration', 0)
            # 如果内存时长和缓存时长偏差 > 3秒，说明切歌了但DB没更
            if abs(cached_ms - (memory_duration * 1000)) > 3000:
                need_search = True
        
        if need_search:
            # 获取窗口标题
            title = WindowUtils.get_netease_window_title()
            
            if title and title != "网易云音乐":
                print(f"[触发搜索] 内存变动，DB未更。标题: {title}")
                search_data = SearchService.search_song_by_title(title, memory_duration)
                
                # 只有当搜索结果有效（SearchService 内部已经做了时长校验）时才采用
                if search_data:
                    self.current_full_data = search_data
                    return search_data

        return None # 如果都没匹配上，返回 None，让 monitor_loop 继续重试
    
    def _get_all_raw_data(self, table_name, limit=None, order_by=None):
        """通用内部方法：获取某张表的所有原始数据 (修复字典迭代错误版)"""
        if not os.path.exists(self.db_path): return []
        
        # 使用时间戳防止文件名冲突
        temp_db = f"temp_{table_name}_{int(time.time())}.dat"
        result_list = []
        
        try:
            shutil.copy2(self.db_path, temp_db)
            if os.path.exists(self.db_path + "-wal"):
                try: shutil.copy2(self.db_path + "-wal", temp_db + "-wal")
                except: pass
            
            conn = sqlite3.connect(temp_db)
            # 设置 row_factory 可以让 cursor 直接返回类似字典的对象(sqlite3.Row)，
            # 但为了兼容性和后续修改，我们还是用 description 手动转字典比较稳妥
            conn.row_factory = sqlite3.Row 
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            
            # 动态构建查询
            sql = f"SELECT * FROM {table_name}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            if limit:
                sql += f" LIMIT {limit}"
                
            cursor.execute(sql)
            rows = cursor.fetchall()
            conn.close()
            
            for row in rows:
                # 将 sqlite3.Row 对象转为标准 Python 字典
                row_dict = dict(row)
                
                # === 修复点在这里 ===
                # 使用 list() 将 items() 转换为列表副本进行遍历
                # 这样就可以在循环内部安全地向 row_dict 添加新 Key 了
                for key, val in list(row_dict.items()):
                    # 检查是否为字符串且看起来像 JSON
                    if isinstance(val, str) and len(val) > 1 and (val.startswith('{') or val.startswith('[')):
                        try:
                            # 尝试解析 JSON
                            parsed_data = json.loads(val)
                            # 将解析后的数据存入新字段，例如 jsonStr -> jsonStr_parsed
                            row_dict[f"{key}_parsed"] = parsed_data
                        except:
                            # 解析失败则忽略，保持原样
                            pass
                
                result_list.append(row_dict)
                
        except Exception as e:
            print(f"[DB Error {table_name}] {e}")
        finally:
            # 清理临时文件
            for f in [temp_db, temp_db + "-wal", temp_db + "-shm"]:
                if os.path.exists(f): 
                    try: os.remove(f)
                    except: pass
                    
        return result_list

    def get_history_list(self, limit=20):
        """获取最近播放历史（返回所有原始字段）"""
        # historyTracks 表通常按 playtime 倒序
        return self._get_all_raw_data("historyTracks", limit=limit, order_by="playtime DESC")

    def get_playlist_list(self):
        """获取用户歌单列表（返回所有原始字段）"""
        # web_user_playlist 表
        return self._get_all_raw_data("web_user_playlist")

# ===========================
# 2. 歌词服务
# ===========================
class LyricService:
    def __init__(self):
        self.lyrics_list = [] 
        self.current_id = None
        self.is_loading = False

    def load_lyrics(self, song_id):
        # 1. 如果ID没变，不需要重新加载
        if song_id == self.current_id: 
            return
            
        # 2. 立即更新当前的目标ID
        self.current_id = song_id
        
        # 3. 立即清空旧歌词 (防止显示上一首)
        self.lyrics_list = []
        
        # 4. 启动线程，把 song_id 传进去作为“令牌”
        threading.Thread(target=self._fetch_lyrics, args=(song_id,), daemon=True).start()

    def _parse_lrc_text(self, lrc_content):
        res = {}
        if not lrc_content: return res
        pattern = re.compile(r'\[(\d{2}):(\d{2}(?:\.\d+)?)\](.*)')
        for line in lrc_content.split('\n'):
            match = pattern.search(line)
            if match:
                t = int(match.group(1)) * 60 + float(match.group(2))
                txt = match.group(3).strip()
                if txt: res[t] = txt
        return res

    def _fetch_lyrics(self, target_song_id):
        self.is_loading = True
        try:
            url = f"http://music.163.com/api/song/lyric?id={target_song_id}&lv=1&kv=1&tv=-1"
            resp = requests.get(url, timeout=5).json()
            
            # === 核心修复：写入前检查“令牌” ===
            # 如果下载完成时，全局的 current_id 已经变成了别的歌（说明用户又切歌了）
            # 那么这次下载的结果就是过期的，必须直接丢弃！
            if target_song_id != self.current_id:
                print(f"[歌词] 丢弃过期数据: {target_song_id}")
                return

            raw_ori = resp.get('lrc', {}).get('lyric', "")
            raw_trans = resp.get('tlyric', {}).get('lyric', "")
            
            ori_dict = self._parse_lrc_text(raw_ori)
            trans_dict = self._parse_lrc_text(raw_trans)
            
            merged = []
            for t in sorted(ori_dict.keys()):
                merged.append({
                    "time": t,
                    "text": ori_dict[t],
                    "trans": trans_dict.get(t, "")
                })
            
            # 二次检查（防止解析耗时期间切歌）
            if target_song_id != self.current_id:
                return

            with state_lock:
                self.lyrics_list = merged
                # print(f"[歌词] 加载完成 ID: {target_song_id}")
                
        except Exception as e:
            print(f"Lyric Error: {e}")
        finally:
            if target_song_id == self.current_id:
                self.is_loading = False

    def get_current_line(self, current_time):
        if not self.lyrics_list: return "", ""
        curr_ori, curr_trans = "", ""
        for item in self.lyrics_list:
            if current_time >= item['time']:
                curr_ori = item['text']
                curr_trans = item['trans']
            else:
                break
        return curr_ori, curr_trans

    def get_all_lyrics(self):
        return self.lyrics_list
    
    def clear(self):
        """新增：强制清空方法"""
        self.current_id = None
        self.lyrics_list = []

# ===========================
# 3. 后台监控线程
# ===========================
def format_t(s):
    return f"{int(s//60):02}:{int(s%60):02}"

def monitor_loop():
    v3 = NeteaseV3Service()
    lrc_svc = LyricService()
    
    OFF_CURR = 0x1D7E8F8
    OFF_TOTAL = 0x1DDEF58
    
    pm = None
    base = None
    
    last_ct = -1.0
    last_tt = 0.0
    
    last_switch_time = 0      
    is_waiting_stable = False 
    
    current_song_title_cache = ""
    last_title_check_time = 0

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

            # 2. 读取内存
            try:
                ct = pm.read_double(base + OFF_CURR)
                tt = pm.read_double(base + OFF_TOTAL)
            except:
                pm = None
                continue

            # 3. 基础状态
            is_moving = (ct != last_ct)
            
            if tt < 1.0:
                time.sleep(0.1)
                continue

            # ==========================================
            # 4. 多重切歌检测
            # ==========================================
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
                    # 清洗标题
                    clean_win_title = win_title.replace(" - 网易云音乐", "").strip()
                    
                    # 只有当 (标题不一致) 且 (当前没有在等待更新) 时，才触发
                    if current_song_title_cache and clean_win_title != current_song_title_cache and not is_waiting_stable:
                        
                        if " - " in clean_win_title:
                            is_switching = True
                            print(f"[触发] 标题变更: '{current_song_title_cache}' -> '{clean_win_title}'")
            
            last_ct = ct 

            # === 执行切歌流程 ===
            if is_switching:
                last_tt = tt
                last_switch_time = time.time()
                is_waiting_stable = True
                
                lrc_svc.clear() 
                with state_lock:
                    API_STATE['lyrics']['all_lyrics'] = []
                    API_STATE['lyrics']['current_line'] = "Loading..."
                    API_STATE['lyrics']['current_trans'] = ""

            # 等待稳定
            if is_waiting_stable:
                time_diff = time.time() - last_switch_time
                
                if time_diff > 1.2:
                    print(f"[防抖] 状态已稳定，同步新歌数据...")
                    is_waiting_stable = False 
                    
                    current_track_full = None
                    retry_count = 0
                    max_retries = 6 
                    
                    while retry_count < max_retries:
                        # 优先读库
                        if v3.check_db_update():
                            track = v3.get_latest_track()
                            if track and abs(track.get('duration', 0) - tt*1000) < 3000:
                                current_track_full = track
                                print(" -> 数据库命中")
                                break
                        
                        # 混合搜索
                        track = v3.get_track_hybrid(tt)
                        if track:
                            diff = abs(track.get('duration', 0) - tt*1000)
                            if diff < 3000:
                                current_track_full = track
                                print(f" -> 标题匹配成功")
                                break
                            if retry_count >= 4:
                                current_track_full = track
                                print(f" -> ⚠️ 强制接受版本差异")
                                break
                        
                        time.sleep(0.5)
                        retry_count += 1
                    
                    if current_track_full:
                        # === 【核心修复】缓存更新逻辑 ===
                        # 必须把所有歌手用 "/" 连起来，才能匹配网易云的窗口标题格式
                        song_name = current_track_full.get('name')
                        artists = current_track_full.get('artists') or current_track_full.get('ar', [])
                        
                        # 提取所有歌手名
                        all_artist_names = [a.get('name') for a in artists]
                        # 用斜杠拼接: "AVTechNO!/初音ミク"
                        artist_concat_str = "/".join(all_artist_names) if all_artist_names else ""
                        
                        # 更新缓存: "Artery - AVTechNO!/初音ミク"
                        current_song_title_cache = f"{song_name} - {artist_concat_str}"
                        # ==========================================
                        
                        song_id = current_track_full.get("id")
                        lrc_svc.load_lyrics(song_id)
                        
                        # API 显示用的歌手字符串 (用 " / " 分隔更美观)
                        artist_display_str = " / ".join(all_artist_names) if all_artist_names else "未知歌手"
                        
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
                    else:
                        print(" -> 同步失败")

            # ==========================================
            # 5. 实时更新区
            # ==========================================
            
            cur_txt, cur_trans = lrc_svc.get_current_line(ct)
            full_lyrics = lrc_svc.get_all_lyrics()
            
            with state_lock:
                API_STATE['playing'] = is_moving
                API_STATE['playback'] = {
                    "current_sec": ct,
                    "total_sec": tt,
                    "percentage": (ct / tt * 100) if tt > 0 else 0,
                    "formatted_current": format_t(ct),
                    "formatted_total": format_t(tt)
                }
                
                if full_lyrics:
                    API_STATE['lyrics']['current_line'] = cur_txt
                    API_STATE['lyrics']['current_trans'] = cur_trans
                    API_STATE['lyrics']['all_lyrics'] = full_lyrics
                elif not API_STATE['basic_info']['id']:
                    API_STATE['lyrics']['all_lyrics'] = []

            time.sleep(0.1)

        except Exception as e:
            print(f"Monitor Loop Error: {e}")
            time.sleep(1)

# ===========================
# 4. Flask Web Server
# ===========================
app = Flask(__name__)
CORS(app)

@app.route('/info', methods=['GET'])
def get_info():
    with state_lock:
        json_str = json.dumps(API_STATE, ensure_ascii=False)
        return Response(json_str, mimetype='application/json')
    
@app.route('/history', methods=['GET'])
def get_history():
    """获取最近播放历史"""
    v3 = NeteaseV3Service()
    data = v3.get_history_list(limit=20) # 默认返回最近20首
    return Response(json.dumps({"code": 200, "data": data}, ensure_ascii=False), mimetype='application/json')

@app.route('/playlist', methods=['GET'])
def get_playlist():
    """获取用户歌单列表"""
    v3 = NeteaseV3Service()
    data = v3.get_playlist_list()
    return Response(json.dumps({"code": 200, "data": data}, ensure_ascii=False), mimetype='application/json')

if __name__ == "__main__":
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    print(f"API 服务已启动: http://127.0.0.1:18726/info")
    app.run(host='0.0.0.0', port=18726, debug=False)