import os
import json
import sqlite3
import pymem
import pymem.process
import time
import shutil
import requests
import re
from threading import Thread

# ===========================
# 1. 数据库与元数据服务 (保持不变)
# ===========================
class NeteaseV3Service:
    def __init__(self):
        self.user_home = os.path.expanduser("~")
        self.db_path = os.path.join(self.user_home, r"AppData\Local\NetEase\CloudMusic\Library\webdb.dat")
        self.last_db_playtime = 0
        self.last_file_mtime = 0
        self.current_track = None

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
                if os.path.exists(f): os.remove(f)
            if row:
                playtime, json_str = row
                if playtime == self.last_db_playtime and self.current_track:
                    return self.current_track
                self.last_db_playtime = playtime
                data = json.loads(json_str)
                artists = [a.get("name") for a in data.get("artists", [])]
                self.current_track = {
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "artist": " / ".join(artists),
                    "total_ms": data.get("duration", 0)
                }
                return self.current_track
        except: pass
        return self.current_track

# ===========================
# 2. 歌词处理系统 (新增翻译支持)
# ===========================
class LyricService:
    def __init__(self):
        self.lyrics_map = [] # 格式: [(sec, original, translation), ...]
        self.current_id = None
        self.is_loading = False

    def load_lyrics(self, song_id):
        if song_id == self.current_id: return
        self.current_id = song_id
        self.lyrics_map = []
        Thread(target=self._fetch_lyrics, args=(song_id,), daemon=True).start()

    def _parse_lrc_text(self, lrc_content):
        """解析单段 LRC 文本为字典 {time: text}"""
        res = {}
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
            
            # 获取原词和翻译
            raw_ori = resp.get('lrc', {}).get('lyric', "")
            raw_trans = resp.get('tlyric', {}).get('lyric', "")
            
            ori_dict = self._parse_lrc_text(raw_ori)
            trans_dict = self._parse_lrc_text(raw_trans)
            
            # 合并逻辑
            merged = []
            for t in sorted(ori_dict.keys()):
                original = ori_dict[t]
                translation = trans_dict.get(t, "") # 如果没翻译，留空
                merged.append((t, original, translation))
            
            self.lyrics_map = merged
        except:
            pass
        finally:
            self.is_loading = False

    def get_lyric_at(self, current_time):
        if self.is_loading: return "Loading...", ""
        if not self.lyrics_map: return "No lyrics.", ""
        
        curr_ori, curr_trans = "...", ""
        for t, ori, trans in self.lyrics_map:
            if current_time >= t:
                curr_ori = ori
                curr_trans = trans
            else:
                break
        return curr_ori, curr_trans

# ===========================
# 3. 主逻辑
# ===========================
def format_t(s):
    return f"{int(s//60):02}:{int(s%60):02}"

def main():
    v3 = NeteaseV3Service()
    lrc_svc = LyricService()
    
    try:
        pm = pymem.Pymem("cloudmusic.exe")
        mod = pymem.process.module_from_name(pm.process_handle, "cloudmusic.dll")
        base = mod.lpBaseOfDll
        OFF_CURR = 0x1D7E8F8
        OFF_TOTAL = 0x1DDEF58
    except:
        print("未检测到网易云运行")
        return

    os.system('') # 开启颜色支持

    track = None
    last_total_t = 0

    while True:
        try:
            ct = pm.read_double(base + OFF_CURR)
            tt = pm.read_double(base + OFF_TOTAL)

            # 更新检测
            if abs(tt - last_total_t) > 0.5 or v3.check_db_update():
                new_track = v3.get_latest_track()
                if new_track:
                    track = new_track
                    last_total_t = tt
                    lrc_svc.load_lyrics(track['id'])

            if track:
                print("\033[H", end="") # 光标回行首
                print("="*60)
                print(f" 🎵  歌曲: {track['name']:<40}")
                print(f" 👤  歌手: {track['artist']:<40}")
                print("-" * 60)
                
                # 获取原词和翻译
                ori_lrc, trans_lrc = lrc_svc.get_lyric_at(ct)
                
                # 显示原词 (亮绿色)
                print(f"\n   \033[1;32m{ori_lrc:<50}\033[0m")
                # 显示翻译 (淡灰色/淡蓝色)，如果有的话
                if trans_lrc:
                    print(f"   \033[1;34m({trans_lrc})\033[0m{' '*20}")
                else:
                    print(f"{' '*60}") # 清理上一首歌残留的翻译行
                print("\n" + "-" * 60)
                
                # 进度条
                p = ct / tt if tt > 0 else 0
                bar = "█" * int(p * 30) + "░" * (30 - int(p * 30))
                print(f" ⏳  {format_t(ct)} [{bar}] {format_t(tt)}    ")
                print("="*60)

            time.sleep(0.05)
        except KeyboardInterrupt: break
        except: time.sleep(0.5)

if __name__ == "__main__":
    main()