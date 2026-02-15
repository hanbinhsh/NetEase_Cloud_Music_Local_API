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
from flask import Flask, Response  # 引入 Response
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

    def get_latest_track(self):
        if not os.path.exists(self.db_path): return None
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
            
            for f in [temp_db, temp_db + "-wal", temp_db + "-shm"]:
                if os.path.exists(f): 
                    try: os.remove(f)
                    except: pass

            if row:
                playtime, json_str = row
                if playtime == self.last_db_playtime and self.current_full_data:
                    return self.current_full_data
                
                self.last_db_playtime = playtime
                data = json.loads(json_str)
                self.current_full_data = data 
                return data
        except Exception as e:
            print(f"DB Error: {e}")
        return self.current_full_data

# ===========================
# 2. 歌词服务
# ===========================
class LyricService:
    def __init__(self):
        self.lyrics_list = [] 
        self.current_id = None
        self.is_loading = False

    def load_lyrics(self, song_id):
        if song_id == self.current_id: return
        self.current_id = song_id
        self.lyrics_list = []
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

    def _fetch_lyrics(self, song_id):
        self.is_loading = True
        try:
            url = f"http://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
            resp = requests.get(url, timeout=5).json()
            
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
            
            with state_lock:
                self.lyrics_list = merged
        except: pass
        finally:
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
    
    last_ct = -1.0    #用于判断暂停
    last_tt = 0.0     #【新增】用于判断切歌（总时长变化）
    
    print("启动后台监控线程...")

    while True:
        try:
            # 1. 进程连接逻辑
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

            # 2. 读取内存数据
            try:
                ct = pm.read_double(base + OFF_CURR)
                tt = pm.read_double(base + OFF_TOTAL)
            except pymem.exception.MemoryReadError:
                pm = None
                continue

            # 3. 判断播放/暂停状态
            is_moving = (ct != last_ct)
            last_ct = ct
            if tt <= 0: is_moving = False

            # ==========================================
            # 4. 【核心修复】双重检测更新机制
            # ==========================================
            # 条件A: 数据库文件本身更新了 (check_db_update)
            # 条件B: 内存中的歌曲总时长突变了 (abs(tt - last_tt) > 1.0) -> 说明切歌了
            
            db_updated = v3.check_db_update()
            song_changed = abs(tt - last_tt) > 1.0 
            
            current_track_full = None

            if db_updated or song_changed:
                # 触发读取新数据
                new_data = v3.get_latest_track()
                if new_data:
                    current_track_full = new_data
                    last_tt = tt # 更新记录的总时长
                    # 只有切歌时才重新加载歌词，防止频繁请求
                    lrc_svc.load_lyrics(current_track_full.get("id"))
                else:
                    # 如果读不到新数据，保持旧数据
                    current_track_full = v3.current_full_data
            else:
                # 既没切歌也没更新文件，直接用缓存
                current_track_full = v3.current_full_data

            # ==========================================
            # 5. 更新全局状态 (API_STATE)
            # ==========================================
            if current_track_full:
                song_id = current_track_full.get("id")
                
                # 获取当前进度的歌词
                cur_txt, cur_trans = lrc_svc.get_current_line(ct)
                artists_list = [a.get("name") for a in current_track_full.get("artists", [])]

                with state_lock:
                    API_STATE['playing'] = is_moving
                    API_STATE['basic_info'] = {
                        "id": song_id,
                        "name": current_track_full.get("name"),
                        "artist": " / ".join(artists_list),
                        "album": current_track_full.get("album", {}).get("name"),
                        "cover_url": current_track_full.get("album", {}).get("picUrl"),
                        "duration": current_track_full.get("duration", 0)
                    }
                    API_STATE['playback'] = {
                        "current_sec": ct,
                        "total_sec": tt,
                        "percentage": (ct / tt * 100) if tt > 0 else 0,
                        "formatted_current": format_t(ct),
                        "formatted_total": format_t(tt)
                    }
                    API_STATE['lyrics']['current_line'] = cur_txt
                    API_STATE['lyrics']['current_trans'] = cur_trans
                    API_STATE['lyrics']['all_lyrics'] = lrc_svc.get_all_lyrics()
                    API_STATE['db_info'] = current_track_full

            time.sleep(0.1) 

        except Exception as e:
            print(f"Monitor Loop Error: {e}")
            time.sleep(1)
# ===========================
# 4. Flask Web Server (强制中文版)
# ===========================
app = Flask(__name__)
CORS(app)

@app.route('/info', methods=['GET'])
def get_info():
    with state_lock:
        # 1. 使用 Python 原生 json.dumps 
        # 2. ensure_ascii=False 强制不转义中文
        # 3. 手动构建 Response 对象
        json_str = json.dumps(API_STATE, ensure_ascii=False)
        return Response(json_str, mimetype='application/json')

@app.route('/', methods=['GET'])
def index():
    return "Netease Local API Running."

if __name__ == "__main__":
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    print(f"API 服务已启动: http://127.0.0.1:18726/info")
    app.run(host='0.0.0.0', port=18726, debug=False)