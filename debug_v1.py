import os
import re
import time
import json
import threading
import requests
import pymem
import win32gui
from flask import Flask, Response
from flask_cors import CORS

# ===========================
# 全局状态
# ===========================
API_STATE = {
    "playing": False,
    "basic_info": {"id": 0, "name": "", "artist": "", "album": "", "cover_url": "", "duration": 0},
    "playback": {"current_sec": 0.0, "total_sec": 0.0, "percentage": 0},
    "lyrics": {"current_line": "", "current_trans": "", "all_lyrics": []}
}
state_lock = threading.Lock()

# ===========================
# 1. 实时日志分析工具 (获取 ID 的救星)
# ===========================
class NeteaseLogService:
    def __init__(self):
        self.log_path = os.path.join(os.environ['LOCALAPPDATA'], r"NetEase\CloudMusic\info.log")

    def get_latest_id_from_log(self):
        """从日志文件中提取最后一次播放的 ID"""
        if not os.path.exists(self.log_path):
            return None
        
        try:
            # 以非锁定模式读取日志最后几百行
            with open(self.log_path, "r", encoding="utf-8", errors="ignore") as f:
                # 移动到末尾，读取最后 2000 字节
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4000))
                content = f.read()

            # 匹配日志中的 ID 模式。网易云日志中通常有 "Update info: id=xxx" 或 "playId:xxx"
            # 这里的正则根据版本不同可能微调，通常如下：
            ids = re.findall(r"id=(\d+)", content)
            if not ids:
                ids = re.findall(r"playId:(\d+)", content)
                
            if ids:
                return int(ids[-1]) # 返回最后一个找到的 ID
        except Exception as e:
            print(f"Log Read Error: {e}")
        return None

# ===========================
# 2. 官方 API 详情查询
# ===========================
class NeteaseApiService:
    def get_song_detail(self, song_id):
        """通过 ID 获取歌曲详情和封面"""
        if not song_id: return None
        url = f"https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        try:
            res = requests.get(url, timeout=2).json()
            song = res.get("songs", [])[0]
            return {
                "id": song["id"],
                "name": song["name"],
                "artist": "/".join([a["name"] for a in song["artists"]]),
                "album": song["album"]["name"],
                "cover_url": song["album"]["picUrl"],
                "duration": song["duration"]
            }
        except: return None

    def get_lyrics(self, song_id):
        """通过 ID 获取歌词"""
        url = f"https://music.163.com/api/song/lyric?id={song_id}&lv=1&tv=-1"
        try:
            res = requests.get(url, timeout=2).json()
            return self._parse_lrc(res.get("lrc", {}).get("lyric", ""), 
                                  res.get("tlyric", {}).get("lyric", ""))
        except: return []

    def _parse_lrc(self, ori, trans):
        import re
        def parse_text(t):
            d = {}
            for line in t.split('\n'):
                m = re.search(r'\[(\d{2}):(\d{2}(?:\.\d+)?)\](.*)', line)
                if m: d[int(m.group(1))*60 + float(m.group(2))] = m.group(3).strip()
            return d
        o_d, t_d = parse_text(ori), parse_text(trans)
        return [{"time": t, "text": o_d[t], "trans": t_d.get(t, "")} for t in sorted(o_d.keys())]

# ===========================
# 3. 监控主逻辑
# ===========================
def monitor_loop():
    log_svc = NeteaseLogService()
    api_svc = NeteaseApiService()
    
    # 请确认你的内存偏移
    OFF_CURR = 0x1D7E8F8
    OFF_TOTAL = 0x1DDEF58
    
    pm = None
    base = None
    last_id = 0
    last_title = ""

    while True:
        try:
            # --- 内存连接 ---
            if pm is None:
                try:
                    pm = pymem.Pymem("cloudmusic.exe")
                    mod = pymem.process.module_from_name(pm.process_handle, "cloudmusic.dll")
                    base = mod.lpBaseOfDll
                except: time.sleep(2); continue

            # --- 获取标题和内存时长 ---
            hwnd = win32gui.FindWindow("OrpheusBrowserHost", None)
            current_title = win32gui.GetWindowText(hwnd) if hwnd else ""
            
            try:
                ct = pm.read_double(base + OFF_CURR)
                tt = pm.read_double(base + OFF_TOTAL)
            except: pm = None; continue

            # --- 切歌判定 ---
            # 只要标题变了，且标题包含 " - "，就说明切歌了
            if current_title != last_title and " - " in current_title:
                print(f"检测到切歌，正在通过日志追溯 ID...")
                
                # 1. 尝试从日志获取实时 ID
                # 刚切歌时日志可能还没写完，重试几次
                current_id = None
                for _ in range(5):
                    current_id = log_svc.get_latest_id_from_log()
                    if current_id and current_id != last_id:
                        break
                    time.sleep(0.3)

                # 2. 如果日志拿到了 ID
                if current_id and current_id != last_id:
                    detail = api_svc.get_song_detail(current_id)
                    if detail:
                        lyrics = api_svc.get_lyrics(current_id)
                        with state_lock:
                            API_STATE["basic_info"] = detail
                            API_STATE["lyrics"]["all_lyrics"] = lyrics
                        last_id = current_id
                        last_title = current_title
                        print(f"成功更新: {detail['name']} (ID: {current_id})")

            # --- 实时进度刷新 ---
            with state_lock:
                API_STATE["playing"] = (ct != API_STATE["playback"]["current_sec"])
                API_STATE["playback"].update({
                    "current_sec": ct,
                    "total_sec": tt,
                    "percentage": (ct / tt * 100) if tt > 0 else 0
                })
                # 实时歌词匹配
                curr_lrc, curr_trans = "", ""
                for item in API_STATE["lyrics"]["all_lyrics"]:
                    if ct >= item["time"]:
                        curr_lrc, curr_trans = item["text"], item["trans"]
                    else: break
                API_STATE["lyrics"]["current_line"] = curr_lrc
                API_STATE["lyrics"]["current_trans"] = curr_trans

            time.sleep(0.1)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(1)

# ===========================
# 4. Flask
# ===========================
app = Flask(__name__)
CORS(app)

@app.route('/info')
def get_info():
    with state_lock:
        return Response(json.dumps(API_STATE, ensure_ascii=False), mimetype='application/json')

if __name__ == "__main__":
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=18726, debug=False)