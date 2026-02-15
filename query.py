import os
import sqlite3
import json
import shutil

def query_database_info():
    # 1. 数据库路径
    user_home = os.path.expanduser("~")
    db_path = os.path.join(user_home, r"AppData\Local\NetEase\CloudMusic\Library\webdb.dat")
    
    if not os.path.exists(db_path):
        print(f"找不到数据库文件: {db_path}")
        return

    # 2. 复制临时文件防止被锁定
    temp_db = "discovery_webdb.dat"
    try:
        shutil.copy2(db_path, temp_db)
        if os.path.exists(db_path + "-wal"):
            shutil.copy2(db_path + "-wal", temp_db + "-wal")
        
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        
        print("\n" + "="*60)
        print(" TASK 1: 数据库表概览")
        print("="*60)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        for i, table in enumerate(tables):
            print(f"[{i+1}] 表名: {table[0]}")

        print("\n" + "="*60)
        print(" TASK 2: 核心表 [historyTracks] 字段结构")
        print("="*60)
        cursor.execute("PRAGMA table_info(historyTracks);")
        columns = cursor.fetchall()
        for col in columns:
            print(f"字段ID: {col[0]} | 名称: {col[1]:<12} | 类型: {col[2]}")

        print("\n" + "="*60)
        print(" TASK 3: 最近播放歌曲的完整 JSON 信息 (已解析)")
        print("="*60)
        # 按照 playtime 降序取最新一条
        cursor.execute("SELECT playtime, jsonStr FROM historyTracks ORDER BY playtime DESC LIMIT 1")
        row = cursor.fetchone()
        
        if row:
            playtime, json_str = row
            print(f"入库时间 (Playtime戳): {playtime}")
            
            # 格式化打印 JSON
            data = json.loads(json_str)
            print("\n--- 完整 JSON 详情 ---")
            print(json.dumps(data, indent=4, ensure_ascii=False))
            
            # 提取一些有趣但我们之前没用的字段
            print("\n--- 潜在可用信息挖掘 ---")
            print(f"歌单来源ID (sourceId): {data.get('sourceId', 'N/A')}")
            print(f"歌曲音质信息 (h/m/l/sq): {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
            if 'privilege' in data:
                print(f"播放权限 (Privilege): {data['privilege'].get('playMaxbr')} (最大码率)")
            if 'mv' in data and data['mv'] != 0:
                print(f"关联 MV ID: {data['mv']}")
        else:
            print("historyTracks 表中没有数据。")

        conn.close()
    except Exception as e:
        print(f"查询出错: {e}")
    finally:
        # 清理临时文件
        for f in [temp_db, temp_db + "-wal", temp_db + "-shm"]:
            if os.path.exists(f): os.remove(f)

if __name__ == "__main__":
    query_database_info()