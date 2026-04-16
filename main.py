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
import struct
import math
import sys
from flask import Flask, Response, request, send_file, send_from_directory
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
    "db_info": {},
    "memory_locator": {
        "status": "bootstrap",
        "source": "bootstrap",
        "fingerprint": "",
        "details": ""
    }
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

    @staticmethod
    def read_double_safe(pm, address):
        try:
            value = pm.read_double(address)
            if math.isfinite(value):
                return value
        except Exception:
            pass
        return None

class CloudMusicOffsetResolver:
    POINTER_OFFSETS = [0x10, 0, 0x10, 0x68, 0]
    TOTAL_PTR_DELTA = 0xD08
    TOTAL_CURR_DELTAS = [0x60760, 0x60868]
    CURRENT_DELTA_TOLERANCE = 0x280
    SCAN_RADIUS = 0x500000
    KNOWN_LAYOUTS = [
        {
            "ptr_static_offset": 0x01DF3490,
            "off_curr": 0x1D93930,
            "off_total": 0x1DF4198
        },
        {
            "ptr_static_offset": 0x01DDE250,
            "off_curr": 0x1D7E8F8,
            "off_total": 0x1DDEF58
        }
    ]

    def __init__(self, cache_path=None):
        self.cache_path = cache_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "offset_cache.json"
        )
        self._lock = threading.Lock()
        self.current_layout = None
        self.status = {
            "status": "bootstrap",
            "source": "bootstrap",
            "fingerprint": "",
            "details": ""
        }
        self.cache = self._load_cache()

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_path):
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception as e:
            print(f"[Locator] 读取缓存失败: {e}")
        return {"entries": {}}

    def _save_cache(self):
        try:
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Locator] 写入缓存失败: {e}")

    def _set_status(self, status, source, fingerprint="", details=""):
        with self._lock:
            self.status = {
                "status": status,
                "source": source,
                "fingerprint": fingerprint,
                "details": details
            }

    def get_status(self):
        with self._lock:
            result = dict(self.status)
            if self.current_layout:
                result["layout"] = {
                    "ptr_static_offset": self.current_layout.get("ptr_static_offset"),
                    "off_curr": self.current_layout.get("off_curr"),
                    "off_total": self.current_layout.get("off_total")
                }
            return result

    def build_fingerprint(self, module):
        module_path = ""
        try:
            module_path = module.filename
        except Exception:
            module_path = "cloudmusic.dll"

        size = int(getattr(module, "SizeOfImage", 0) or 0)
        stat_size = 0
        stat_mtime = 0
        if module_path and os.path.exists(module_path):
            try:
                stat = os.stat(module_path)
                stat_size = int(stat.st_size)
                stat_mtime = int(stat.st_mtime)
            except OSError:
                pass

        base_name = os.path.basename(module_path).lower() if module_path else "cloudmusic.dll"
        return f"{base_name}|img={size}|file={stat_size}|mtime={stat_mtime}"

    def _normalize_layout(self, layout, source, fingerprint):
        off_total = int(layout["off_total"])
        ptr_static = int(layout.get("ptr_static_offset", off_total - self.TOTAL_PTR_DELTA))
        return {
            "ptr_static_offset": ptr_static,
            "ptr_offsets": list(self.POINTER_OFFSETS),
            "off_curr": int(layout["off_curr"]),
            "off_total": off_total,
            "source": source,
            "fingerprint": fingerprint,
            "validated_at": int(time.time())
        }

    def _cache_entry(self, fingerprint):
        return self.cache.get("entries", {}).get(fingerprint)

    def _manual_entry(self):
        manual = self.cache.get("manual_override")
        if isinstance(manual, dict) and "off_curr" in manual and "off_total" in manual:
            return manual
        return None

    def _store_layout(self, fingerprint, layout):
        if not fingerprint:
            return

        normalized = self._normalize_layout(layout, layout.get("source", "resolved"), fingerprint)
        self.cache.setdefault("entries", {})[fingerprint] = normalized
        self._save_cache()

    def apply_manual_layout(self, off_curr, off_total, ptr_static_offset=None, fingerprint="manual-entry"):
        off_curr = int(off_curr)
        off_total = int(off_total)
        if ptr_static_offset is None:
            ptr_static_offset = off_total - self.TOTAL_PTR_DELTA

        layout = self._normalize_layout(
            {
                "ptr_static_offset": int(ptr_static_offset),
                "off_curr": off_curr,
                "off_total": off_total
            },
            "manual_override",
            fingerprint
        )
        self.cache["manual_override"] = layout
        if fingerprint:
            self.cache.setdefault("entries", {})[fingerprint] = dict(layout)
        self.current_layout = layout
        self._save_cache()
        self._set_status("ready", "manual_override", fingerprint, "已应用手工录入偏移")
        return layout

    def get_cache_path(self):
        return self.cache_path

    def _sample_progress(self, pm, base_addr, off_curr, off_total, sample_count=4, interval=0.05):
        current_values = []
        total_values = []

        for idx in range(sample_count):
            ct = MemoryUtils.read_double_safe(pm, base_addr + off_curr)
            tt = MemoryUtils.read_double_safe(pm, base_addr + off_total)
            if ct is None or tt is None:
                return None
            current_values.append(ct)
            total_values.append(tt)
            if idx != sample_count - 1:
                time.sleep(interval)

        tt_min = min(total_values)
        tt_max = max(total_values)
        ct_min = min(current_values)
        ct_max = max(current_values)
        total_span = tt_max - tt_min
        current_delta = current_values[-1] - current_values[0]

        valid = (
            tt_max > 0.5 and
            ct_min >= -0.5 and
            total_span < 0.75 and
            current_values[-1] <= total_values[-1] + 3.0
        )

        if not valid:
            return None

        score = 100.0
        score -= min(total_span, 1.0) * 25.0
        if current_delta >= 0:
            score += min(current_delta, 0.5) * 20.0
        else:
            score -= min(abs(current_delta), 0.5) * 40.0
        if ct_max > tt_max + 1.0:
            score -= 40.0

        return {
            "current": current_values[-1],
            "total": total_values[-1],
            "score": score
        }

    def is_runtime_progress_valid(self, current_sec, total_sec):
        if current_sec is None or total_sec is None:
            return False
        if not (math.isfinite(current_sec) and math.isfinite(total_sec)):
            return False
        if total_sec <= 0.5:
            return False
        if current_sec < -0.5:
            return False
        if current_sec > total_sec + 3.0:
            return False
        return True

    def _validate_pointer(self, pm, base_addr, ptr_static_offset):
        song_id = MemoryUtils.read_pointer_chain_string(
            pm,
            base_addr,
            ptr_static_offset,
            self.POINTER_OFFSETS
        )
        if song_id and song_id > 1000:
            return song_id
        return None

    def _validate_layout(self, pm, base_addr, layout):
        ptr_static_offset = int(layout.get("ptr_static_offset", int(layout["off_total"]) - self.TOTAL_PTR_DELTA))
        pointer_id = self._validate_pointer(pm, base_addr, ptr_static_offset)
        progress = self._sample_progress(pm, base_addr, int(layout["off_curr"]), int(layout["off_total"]))
        if not progress and not pointer_id:
            return None

        return {
            "layout": self._normalize_layout(
                {
                    "ptr_static_offset": ptr_static_offset,
                    "off_curr": int(layout["off_curr"]),
                    "off_total": int(layout["off_total"])
                },
                layout.get("source", "candidate"),
                layout.get("fingerprint", "")
            ),
            "pointer_id": pointer_id,
            "progress": progress,
            "strong": bool(pointer_id and progress),
            "score": (progress["score"] if progress else 0) + (60 if pointer_id else 0)
        }

    def _iter_candidate_layouts(self, fingerprint):
        seen = set()
        raw_candidates = []

        if self.current_layout:
            raw_candidates.append(dict(self.current_layout))

        manual = self._manual_entry()
        if manual:
            raw_candidates.append(dict(manual))

        cached = self._cache_entry(fingerprint)
        if cached:
            raw_candidates.append(dict(cached))

        raw_candidates.extend(self.KNOWN_LAYOUTS)

        for item in raw_candidates:
            key = (int(item["off_curr"]), int(item["off_total"]))
            if key in seen:
                continue
            seen.add(key)
            normalized = self._normalize_layout(item, item.get("source", "known"), fingerprint)
            yield normalized

    def _window_bounds(self, center, image_size):
        start = max(0, int(center) - self.SCAN_RADIUS)
        end = min(int(image_size), int(center) + self.SCAN_RADIUS)
        if end - start < 8:
            return None
        return start, end

    def _locate_curr_near_total(self, pm, base_addr, total_rva, total_value):
        best_match = None
        base_deltas = sorted(set(self.TOTAL_CURR_DELTAS))

        for base_delta in base_deltas:
            for delta_adjust in range(-self.CURRENT_DELTA_TOLERANCE, self.CURRENT_DELTA_TOLERANCE + 1, 8):
                curr_rva = int(total_rva - (base_delta + delta_adjust))
                if curr_rva <= 0:
                    continue

                current_value = MemoryUtils.read_double_safe(pm, base_addr + curr_rva)
                if current_value is None:
                    continue
                if current_value < -0.5 or current_value > total_value + 3.0:
                    continue

                sample = self._sample_progress(pm, base_addr, curr_rva, total_rva, sample_count=3, interval=0.04)
                if not sample:
                    continue

                score = sample["score"] - abs(delta_adjust) / 16.0
                if best_match is None or score > best_match["score"]:
                    best_match = {
                        "off_curr": curr_rva,
                        "score": score
                    }

        return best_match

    def _scan_for_layout(self, pm, module, fingerprint):
        base_addr = module.lpBaseOfDll
        image_size = int(getattr(module, "SizeOfImage", 0) or 0)
        if image_size <= 0:
            return None

        search_centers = [layout["off_total"] for layout in self.KNOWN_LAYOUTS]
        cached = self._cache_entry(fingerprint)
        if cached:
            search_centers.insert(0, int(cached["off_total"]))
        if self.current_layout:
            search_centers.insert(0, int(self.current_layout["off_total"]))

        best_result = None

        for center in search_centers:
            bounds = self._window_bounds(center, image_size)
            if not bounds:
                continue

            start, end = bounds
            try:
                block = pm.read_bytes(base_addr + start, end - start)
            except Exception:
                continue

            for local_offset in range(0, len(block) - 8, 8):
                total_rva = start + local_offset
                total_value = struct.unpack_from("<d", block, local_offset)[0]
                if not math.isfinite(total_value):
                    continue
                if total_value < 1.0 or total_value > 7200.0:
                    continue

                ptr_static_offset = total_rva - self.TOTAL_PTR_DELTA
                if ptr_static_offset <= 0 or ptr_static_offset >= image_size:
                    continue

                pointer_id = self._validate_pointer(pm, base_addr, ptr_static_offset)
                if not pointer_id:
                    continue

                curr_match = self._locate_curr_near_total(pm, base_addr, total_rva, total_value)
                if not curr_match:
                    continue

                validated = self._validate_layout(
                    pm,
                    base_addr,
                    {
                        "ptr_static_offset": ptr_static_offset,
                        "off_curr": curr_match["off_curr"],
                        "off_total": total_rva,
                        "source": "relocated",
                        "fingerprint": fingerprint
                    }
                )
                if not validated:
                    continue

                if validated["strong"]:
                    return validated["layout"]

                if best_result is None or validated["score"] > best_result["score"]:
                    best_result = validated

        return best_result["layout"] if best_result else None

    def resolve(self, pm, module, force_rescan=False):
        fingerprint = self.build_fingerprint(module)
        base_addr = module.lpBaseOfDll
        best_weak = None

        self._set_status("relocating", "bootstrap", fingerprint, "正在校验内存偏移")

        if not force_rescan:
            for layout in self._iter_candidate_layouts(fingerprint):
                validated = self._validate_layout(pm, base_addr, layout)
                if not validated:
                    continue
                if validated["strong"]:
                    self.current_layout = validated["layout"]
                    self._store_layout(fingerprint, self.current_layout)
                    self._set_status("ready", validated["layout"]["source"], fingerprint, "已命中缓存/已知偏移")
                    return self.current_layout
                if best_weak is None or validated["score"] > best_weak["score"]:
                    best_weak = validated

        relocated = self._scan_for_layout(pm, module, fingerprint)
        if relocated:
            self.current_layout = self._normalize_layout(relocated, relocated.get("source", "relocated"), fingerprint)
            self._store_layout(fingerprint, self.current_layout)
            self._set_status("ready", self.current_layout["source"], fingerprint, "已自动重定位")
            return self.current_layout

        if best_weak:
            self.current_layout = best_weak["layout"]
            self._set_status("degraded", self.current_layout["source"], fingerprint, "仅部分校验通过，继续降级运行")
            return self.current_layout

        fallback = self._normalize_layout(self.KNOWN_LAYOUTS[0], "hardcoded_fallback", fingerprint)
        self.current_layout = fallback
        self._set_status("failed", "hardcoded_fallback", fingerprint, "自动定位失败，退回硬编码候选")
        return fallback

class GuidedOffsetScanner:
    def __init__(self, locator):
        self.locator = locator
        self.pm = None
        self.module = None
        self.base_addr = None
        self.image_size = 0
        self.current_candidates = []
        self.total_candidates = []
        self.id_candidates = []

    def attach(self):
        self.pm = pymem.Pymem("cloudmusic.exe")
        self.module = pymem.process.module_from_name(self.pm.process_handle, "cloudmusic.dll")
        self.base_addr = int(self.module.lpBaseOfDll)
        self.image_size = int(getattr(self.module, "SizeOfImage", 0) or 0)
        return {
            "base_addr": self.base_addr,
            "image_size": self.image_size,
            "fingerprint": self.locator.build_fingerprint(self.module)
        }

    def ensure_attached(self):
        if self.pm is None or self.module is None:
            return self.attach()
        return {
            "base_addr": self.base_addr,
            "image_size": self.image_size,
            "fingerprint": self.locator.build_fingerprint(self.module)
        }

    def _scan_block_for_range(self, min_value, max_value):
        self.ensure_attached()
        block = self.pm.read_bytes(self.base_addr, self.image_size)
        results = []

        for offset in range(0, len(block) - 8, 8):
            value = struct.unpack_from("<d", block, offset)[0]
            if not math.isfinite(value):
                continue
            if value < min_value or value > max_value:
                continue
            results.append({
                "address": self.base_addr + offset,
                "rva": offset,
                "value": value
            })

        return results

    def _refresh_candidates(self, candidates):
        refreshed = []
        for item in candidates:
            value = MemoryUtils.read_double_safe(self.pm, item["address"])
            if value is None:
                continue
            clone = dict(item)
            clone["value"] = value
            refreshed.append(clone)
        return refreshed

    def _filter_candidates(self, candidates, min_value, max_value):
        self.ensure_attached()
        refreshed = self._refresh_candidates(candidates)
        return [item for item in refreshed if min_value <= item["value"] <= max_value]

    def scan_current(self, min_value, max_value):
        self.current_candidates = self._scan_block_for_range(min_value, max_value)
        return list(self.current_candidates)

    def rescan_current(self, min_value, max_value):
        self.current_candidates = self._filter_candidates(self.current_candidates, min_value, max_value)
        return list(self.current_candidates)

    def scan_total(self, min_value, max_value):
        self.total_candidates = self._scan_block_for_range(min_value, max_value)
        return list(self.total_candidates)

    def rescan_total(self, min_value, max_value):
        self.total_candidates = self._filter_candidates(self.total_candidates, min_value, max_value)
        return list(self.total_candidates)

    def refresh_live_values(self):
        self.ensure_attached()
        self.current_candidates = self._refresh_candidates(self.current_candidates)
        self.total_candidates = self._refresh_candidates(self.total_candidates)
        self.id_candidates = self._refresh_id_candidates(self.id_candidates)
        return {
            "current": list(self.current_candidates),
            "total": list(self.total_candidates),
            "id": list(self.id_candidates)
        }

    def describe_candidate(self, candidate):
        self.ensure_attached()
        ptr_static = int(candidate["rva"]) - self.locator.TOTAL_PTR_DELTA
        song_id = None
        if ptr_static > 0:
            song_id = MemoryUtils.read_pointer_chain_string(
                self.pm,
                self.base_addr,
                ptr_static,
                self.locator.POINTER_OFFSETS
            )
        return {
            "ptr_static_offset": ptr_static,
            "song_id": song_id
        }

    def _read_song_id_from_ptr(self, ptr_static_offset):
        self.ensure_attached()
        if ptr_static_offset is None or int(ptr_static_offset) <= 0:
            return None
        return MemoryUtils.read_pointer_chain_string(
            self.pm,
            self.base_addr,
            int(ptr_static_offset),
            self.locator.POINTER_OFFSETS
        )

    def _refresh_id_candidates(self, candidates):
        refreshed = []
        for item in candidates:
            song_id = self._read_song_id_from_ptr(item["rva"])
            if not song_id:
                continue
            clone = dict(item)
            clone["song_id"] = song_id
            refreshed.append(clone)
        return refreshed

    def _scan_pointer_offsets(self, target_song_id, center_rva, radius, step):
        self.ensure_attached()
        target_song_id = int(target_song_id)
        center_rva = int(center_rva)
        radius = max(int(radius), step)
        step = max(int(step), 1)
        start = max(0, center_rva - radius)
        end = min(self.image_size, center_rva + radius)
        results = []

        for rva in range(start, end, step):
            song_id = self._read_song_id_from_ptr(rva)
            if song_id != target_song_id:
                continue
            results.append({
                "address": self.base_addr + rva,
                "rva": rva,
                "song_id": song_id
            })

        return results

    def _guess_pointer_centers(self, total_rva=None):
        centers = []
        if total_rva:
            centers.append(int(total_rva) - self.locator.TOTAL_PTR_DELTA)
        if self.locator.current_layout:
            centers.append(int(self.locator.current_layout.get("ptr_static_offset", 0)))
        manual = self.locator._manual_entry()
        if manual:
            centers.append(int(manual.get("ptr_static_offset", 0)))
        for item in self.locator.KNOWN_LAYOUTS:
            centers.append(int(item.get("ptr_static_offset", 0)))

        seen = set()
        result = []
        for center in centers:
            if center <= 0 or center in seen:
                continue
            seen.add(center)
            result.append(center)
        return result

    def scan_id(self, target_song_id, center_rva=None, radius=0x40000, step=8, total_rva=None):
        self.ensure_attached()
        all_results = []
        seen = set()

        centers = [int(center_rva)] if center_rva is not None else self._guess_pointer_centers(total_rva)
        for center in centers:
            for item in self._scan_pointer_offsets(target_song_id, center, radius, step):
                if item["rva"] in seen:
                    continue
                seen.add(item["rva"])
                all_results.append(item)

        self.id_candidates = sorted(all_results, key=lambda x: x["rva"])
        return list(self.id_candidates)

    def rescan_id(self, target_song_id):
        self.ensure_attached()
        target_song_id = int(target_song_id)
        refreshed = self._refresh_id_candidates(self.id_candidates)
        self.id_candidates = [item for item in refreshed if item["song_id"] == target_song_id]
        return list(self.id_candidates)

    def fingerprint(self):
        self.ensure_attached()
        return self.locator.build_fingerprint(self.module)

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
# 0.5 按键模拟工具 (触发全局快捷键)
# ===========================
class KeyboardHelper:
    VK_CTRL = 0x11
    VK_ALT = 0x12
    VK_LEFT = 0x25
    VK_RIGHT = 0x27
    VK_P = 0x50

    @staticmethod
    def press_shortcut(keys):
        """模拟组合键按下与抬起"""
        # 依次按下按键
        for k in keys:
            ctypes.windll.user32.keybd_event(k, 0, 0, 0)
        time.sleep(0.05)
        # 逆序松开按键 (2 代表 KEYEVENTF_KEYUP)
        for k in reversed(keys):
            ctypes.windll.user32.keybd_event(k, 0, 2, 0)

# ===========================
# 1. 数据库服务
# ===========================
class NeteaseV3Service:
    def __init__(self):
        self.user_home = os.path.expanduser("~")
        self.db_path = os.path.join(
            self.user_home,
            r"AppData\Local\NetEase\CloudMusic\Library\webdb.dat"
        )

        self.last_db_playtime = 0
        self.last_file_mtime = 0
        self.current_full_data = None

        # 播放列表缓存
        self.playing_list_cache = []
        self.playing_list_mtime = 0

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
        获取原始的播放列表数据 (带缓存优化)
        """
        file_path = os.path.join(
            os.environ['LOCALAPPDATA'], 
            r"Netease\CloudMusic\webdata\file\playingList"
        )
        
        if not os.path.exists(file_path):
            return []

        try:
            # 检查文件修改时间
            current_mtime = os.path.getmtime(file_path)
            # 如果文件没变，直接返回缓存，不再读盘
            if current_mtime == self.playing_list_mtime and self.playing_list_cache:
                return self.playing_list_cache

            # 文件变了，重新读取
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                if not content: return []
                
                root_data = json.loads(content)
                result = []
                
                if isinstance(root_data, dict) and 'list' in root_data:
                    result = root_data['list']
                elif isinstance(root_data, list):
                    result = root_data
                
                # 更新缓存
                self.playing_list_cache = result
                self.playing_list_mtime = current_mtime
                # print(f"[列表更新] 检测到播放列表文件变更，已刷新缓存。数量: {len(result)}")
                return result
                
        except Exception as e:
            print(f"[PlayingList Error] 读取失败: {e}")
            return self.playing_list_cache # 出错时返回旧缓存
                
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

def parse_offset_value(raw_value):
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return int(raw_value)

    text = str(raw_value).strip()
    if not text:
        return None
    if text.lower().startswith("cloudmusic.dll+"):
        text = text.split("+", 1)[1]
    if text.lower().startswith("0x"):
        return int(text, 16)
    if re.fullmatch(r"[0-9a-fA-F]+", text):
        return int(text, 16)
    return int(text)

def launch_locator_gui(locator):
    import tkinter as tk
    from tkinter import messagebox, ttk

    scanner = GuidedOffsetScanner(locator)
    db_service = NeteaseV3Service()
    current_layout = locator.current_layout or locator._manual_entry() or locator.KNOWN_LAYOUTS[0]
    song_meta_cache = {}

    def fmt(value):
        return f"0x{int(value):08X}"

    def update_status(text):
        status_var.set(text)
        root.update_idletasks()

    def parse_seconds(entry_value, tolerance_value):
        center = float(entry_value.strip())
        tolerance = float(tolerance_value.strip() or "0.5")
        if tolerance < 0:
            raise ValueError("容差不能为负数")
        return center - tolerance, center + tolerance

    def attach_process():
        try:
            meta = scanner.attach()
            update_status(
                f"已附加 cloudmusic.exe | base=0x{meta['base_addr']:X} | image=0x{meta['image_size']:X}"
            )
            refresh_preview()
        except Exception as exc:
            messagebox.showerror("附加失败", str(exc))

    def fill_tree(tree, candidates, kind):
        tree.delete(*tree.get_children())
        for idx, item in enumerate(candidates[:500]):
            extra = ""
            value = ""
            if kind == "id":
                value = str(item.get("song_id", ""))
            else:
                value = f"{item['value']:.6f}"
            tree.insert(
                "",
                "end",
                iid=f"{kind}:{idx}",
                values=(
                    idx + 1,
                    f"0x{item['address']:X}",
                    f"0x{item['rva']:08X}",
                    value,
                    extra
                )
            )

    def selected_candidate(kind):
        tree_map = {
            "current": current_tree,
            "total": total_tree,
            "id": id_tree
        }
        item_map = {
            "current": scanner.current_candidates,
            "total": scanner.total_candidates,
            "id": scanner.id_candidates
        }
        tree = tree_map[kind]
        items = item_map[kind]
        selection = tree.selection()
        if not selection:
            return None
        _, idx_text = selection[0].split(":")
        idx = int(idx_text)
        if idx >= len(items):
            return None
        return items[idx]

    def sync_selection_to_entries(*_):
        current_item = selected_candidate("current")
        total_item = selected_candidate("total")
        id_item = selected_candidate("id")

        if current_item:
            curr_var.set(fmt(current_item["rva"]))

        if total_item:
            total_var.set(fmt(total_item["rva"]))
            total_info = scanner.describe_candidate(total_item)
            ptr_guess = total_info["ptr_static_offset"]
            if ptr_guess > 0:
                ptr_var.set(fmt(ptr_guess))
            if total_info.get("song_id"):
                update_status(
                    f"已选总时长候选 0x{total_item['rva']:08X}，推导 PTR_STATIC_OFFSET={fmt(ptr_guess)}，歌曲ID={total_info['song_id']}"
                )
        if id_item:
            ptr_var.set(fmt(id_item["rva"]))
            update_status(f"已选歌曲ID候选 0x{id_item['rva']:08X} -> {id_item.get('song_id')}")

        refresh_preview()

    def run_scan(kind, rescan=False):
        try:
            scanner.ensure_attached()
            if kind == "current":
                min_value, max_value = parse_seconds(curr_seconds_var.get(), curr_tol_var.get())
                candidates = scanner.rescan_current(min_value, max_value) if rescan else scanner.scan_current(min_value, max_value)
                fill_tree(current_tree, candidates, "current")
            elif kind == "total":
                min_value, max_value = parse_seconds(total_seconds_var.get(), total_tol_var.get())
                candidates = scanner.rescan_total(min_value, max_value) if rescan else scanner.scan_total(min_value, max_value)
                fill_tree(total_tree, candidates, "total")
            else:
                target_song_id = int(id_target_var.get().strip())
                if rescan:
                    candidates = scanner.rescan_id(target_song_id)
                else:
                    center_text = id_center_var.get().strip()
                    center_rva = parse_offset_value(center_text) if center_text else None
                    radius = parse_offset_value(id_radius_var.get()) if id_radius_var.get().strip() else 0x40000
                    step = parse_offset_value(id_step_var.get()) if id_step_var.get().strip() else 8
                    total_hint = parse_offset_value(total_var.get()) if total_var.get().strip() else None
                    candidates = scanner.scan_id(
                        target_song_id,
                        center_rva=center_rva,
                        radius=radius,
                        step=step,
                        total_rva=total_hint
                    )
                fill_tree(id_tree, candidates, "id")

            action = "继续筛选" if rescan else "首次扫描"
            update_status(f"{action}完成：{kind} 候选 {len(candidates)} 个")
            refresh_preview()
        except Exception as exc:
            messagebox.showerror("扫描失败", str(exc))

    def refresh_live():
        try:
            data = scanner.refresh_live_values()
            fill_tree(current_tree, data["current"], "current")
            fill_tree(total_tree, data["total"], "total")
            fill_tree(id_tree, data["id"], "id")
            sync_selection_to_entries()
            update_status("已刷新候选当前值")
        except Exception as exc:
            messagebox.showerror("刷新失败", str(exc))

    def song_meta(song_id):
        if not song_id:
            return None
        song_id = int(song_id)
        if song_id in song_meta_cache:
            return song_meta_cache[song_id]

        data = db_service.search_db_for_id(song_id)
        if not data:
            try:
                data = db_service.get_song_detail_by_id(song_id)
            except Exception:
                data = None
        if not data:
            song_meta_cache[song_id] = None
            return None

        artists = data.get('artists') or data.get('ar', [])
        artist_names = [a.get('name') for a in artists if a.get('name')]
        meta = {
            "id": song_id,
            "name": data.get("name", ""),
            "artist": " / ".join(artist_names),
            "album": (data.get("album") or data.get("al", {})).get("name", "")
        }
        song_meta_cache[song_id] = meta
        return meta

    def refresh_preview():
        try:
            scanner.ensure_attached()
        except Exception:
            preview_curr_var.set("未附加进程")
            preview_total_var.set("未附加进程")
            preview_ptr_var.set("未附加进程")
            preview_song_var.set("")
            return

        try:
            curr_rva = parse_offset_value(curr_var.get()) if curr_var.get().strip() else None
            total_rva = parse_offset_value(total_var.get()) if total_var.get().strip() else None
            ptr_rva = parse_offset_value(ptr_var.get()) if ptr_var.get().strip() else None

            curr_value = MemoryUtils.read_double_safe(scanner.pm, scanner.base_addr + curr_rva) if curr_rva is not None else None
            total_value = MemoryUtils.read_double_safe(scanner.pm, scanner.base_addr + total_rva) if total_rva is not None else None
            song_id = scanner._read_song_id_from_ptr(ptr_rva) if ptr_rva is not None else None

            preview_curr_var.set(f"{curr_value:.6f}" if curr_value is not None else "无效")
            preview_total_var.set(f"{total_value:.6f}" if total_value is not None else "无效")
            preview_ptr_var.set(str(song_id) if song_id else "无效")

            meta = song_meta(song_id) if song_id else None
            if meta:
                preview_song_var.set(
                    f"{meta['name']} | {meta['artist'] or '未知歌手'} | {meta['album'] or '未知专辑'}"
                )
            else:
                preview_song_var.set("")

            if ptr_rva is not None:
                if not id_target_var.get().strip() and song_id:
                    id_target_var.set(str(song_id))
                id_center_var.set(fmt(ptr_rva))
        except Exception:
            preview_curr_var.set("无效")
            preview_total_var.set("无效")
            preview_ptr_var.set("无效")
            preview_song_var.set("")

    def schedule_preview():
        refresh_preview()
        root.after(700, schedule_preview)

    def save_layout():
        try:
            off_curr = parse_offset_value(curr_var.get())
            off_total = parse_offset_value(total_var.get())
            ptr_text = ptr_var.get().strip()
            ptr_static = parse_offset_value(ptr_text) if ptr_text else None
            fingerprint = ""
            try:
                fingerprint = scanner.fingerprint()
            except Exception:
                fingerprint = "manual-entry"
            saved_layout = locator.apply_manual_layout(off_curr, off_total, ptr_static, fingerprint=fingerprint)
            update_status(f"已保存到 {locator.get_cache_path()} | fingerprint={fingerprint}")
            current_saved_var.set(
                f"OFF_CURR={fmt(saved_layout['off_curr'])} | OFF_TOTAL={fmt(saved_layout['off_total'])} | PTR_STATIC_OFFSET={fmt(saved_layout['ptr_static_offset'])}"
            )
            messagebox.showinfo(
                "保存成功",
                f"已写入 {locator.get_cache_path()}\n下次启动会优先使用这组偏移。"
            )
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    root = tk.Tk()
    root.title("CloudMusic 引导式偏移定位")
    root.geometry("1200x1000")
    root.minsize(1000, 860)

    status_var = tk.StringVar(value="准备就绪")
    curr_seconds_var = tk.StringVar(value="75")
    curr_tol_var = tk.StringVar(value="0.6")
    total_seconds_var = tk.StringVar(value="240")
    total_tol_var = tk.StringVar(value="0.8")
    id_target_var = tk.StringVar(value="")
    id_radius_var = tk.StringVar(value="0x40000")
    id_step_var = tk.StringVar(value="0x8")
    id_center_var = tk.StringVar(value=fmt(current_layout["ptr_static_offset"]))
    curr_var = tk.StringVar(value=fmt(current_layout["off_curr"]))
    total_var = tk.StringVar(value=fmt(current_layout["off_total"]))
    ptr_var = tk.StringVar(value=fmt(current_layout["ptr_static_offset"]))
    preview_curr_var = tk.StringVar(value="")
    preview_total_var = tk.StringVar(value="")
    preview_ptr_var = tk.StringVar(value="")
    preview_song_var = tk.StringVar(value="")
    current_saved_var = tk.StringVar(value="")
    initial_manual = locator._manual_entry()
    if initial_manual:
        current_saved_var.set(
            f"OFF_CURR={fmt(initial_manual['off_curr'])} | OFF_TOTAL={fmt(initial_manual['off_total'])} | PTR_STATIC_OFFSET={fmt(initial_manual['ptr_static_offset'])}"
        )

    header = tk.Label(
        root,
        text=(
            "建议流程：先扫描当前进度，再扫描总时长；如果歌曲ID不稳定，再切到歌曲ID页按歌曲链接里的数字筛选；"
            "下方会实时预览当前填入的地址对应的值和歌曲信息。"
        ),
        wraplength=1120,
        justify="left"
    )
    header.pack(fill="x", padx=16, pady=(16, 10))

    top_bar = tk.Frame(root)
    top_bar.pack(fill="x", padx=16, pady=(0, 10))
    tk.Button(top_bar, text="附加网易云进程", command=attach_process, width=18).pack(side="left")
    tk.Button(top_bar, text="刷新候选当前值", command=refresh_live, width=16).pack(side="left", padx=8)
    tk.Button(top_bar, text="立即刷新预览", command=refresh_preview, width=14).pack(side="left", padx=8)
    tk.Label(top_bar, textvariable=status_var, anchor="w").pack(side="left", padx=12)

    scan_frame = tk.Frame(root)
    scan_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
    scan_frame.columnconfigure(0, weight=1)
    scan_frame.rowconfigure(0, weight=1)

    notebook = ttk.Notebook(scan_frame)
    notebook.grid(row=0, column=0, sticky="nsew")

    current_box = ttk.Frame(notebook)
    total_box = ttk.Frame(notebook)
    id_box = ttk.Frame(notebook)
    notebook.add(current_box, text="当前时长")
    notebook.add(total_box, text="总时长")
    notebook.add(id_box, text="歌曲ID")

    for frame, seconds_var, tol_var, scan_kind in [
        (current_box, curr_seconds_var, curr_tol_var, "current"),
        (total_box, total_seconds_var, total_tol_var, "total")
    ]:
        control = tk.Frame(frame)
        control.pack(fill="x", padx=12, pady=10)
        tk.Label(control, text="目标秒数").pack(side="left")
        tk.Entry(control, textvariable=seconds_var, width=10).pack(side="left", padx=(8, 12))
        tk.Label(control, text="容差").pack(side="left")
        tk.Entry(control, textvariable=tol_var, width=8).pack(side="left", padx=(8, 12))
        tk.Button(control, text="首次扫描", command=lambda kind=scan_kind: run_scan(kind, False), width=12).pack(side="left")
        tk.Button(control, text="继续筛选", command=lambda kind=scan_kind: run_scan(kind, True), width=12).pack(side="left", padx=8)

    id_control = tk.Frame(id_box)
    id_control.pack(fill="x", padx=12, pady=10)
    tk.Label(id_control, text="目标歌曲ID").pack(side="left")
    tk.Entry(id_control, textvariable=id_target_var, width=16).pack(side="left", padx=(8, 12))
    tk.Label(id_control, text="中心偏移").pack(side="left")
    tk.Entry(id_control, textvariable=id_center_var, width=14).pack(side="left", padx=(8, 12))
    tk.Label(id_control, text="半径").pack(side="left")
    tk.Entry(id_control, textvariable=id_radius_var, width=12).pack(side="left", padx=(8, 12))
    tk.Label(id_control, text="步长").pack(side="left")
    tk.Entry(id_control, textvariable=id_step_var, width=8).pack(side="left", padx=(8, 12))
    tk.Button(id_control, text="首次扫描", command=lambda: run_scan("id", False), width=12).pack(side="left")
    tk.Button(id_control, text="继续筛选", command=lambda: run_scan("id", True), width=12).pack(side="left", padx=8)

    tree_columns = ("idx", "addr", "rva", "value", "extra")
    current_tree = ttk.Treeview(current_box, columns=tree_columns, show="headings", height=20)
    total_tree = ttk.Treeview(total_box, columns=tree_columns, show="headings", height=20)
    id_tree = ttk.Treeview(id_box, columns=tree_columns, show="headings", height=20)

    for tree in (current_tree, total_tree, id_tree):
        tree.heading("idx", text="#")
        tree.heading("addr", text="绝对地址")
        tree.heading("rva", text="cloudmusic.dll+偏移")
        tree.heading("value", text="当前值/歌曲ID")
        tree.heading("extra", text="附加信息")
        tree.column("idx", width=50, anchor="center")
        tree.column("addr", width=180, anchor="center")
        tree.column("rva", width=150, anchor="center")
        tree.column("value", width=100, anchor="center")
        tree.column("extra", width=120, anchor="center")

    current_scroll = ttk.Scrollbar(current_box, orient="vertical", command=current_tree.yview)
    current_tree.configure(yscrollcommand=current_scroll.set)
    total_scroll = ttk.Scrollbar(total_box, orient="vertical", command=total_tree.yview)
    total_tree.configure(yscrollcommand=total_scroll.set)
    id_scroll = ttk.Scrollbar(id_box, orient="vertical", command=id_tree.yview)
    id_tree.configure(yscrollcommand=id_scroll.set)

    current_tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
    current_scroll.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))
    total_tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
    total_scroll.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))
    id_tree.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
    id_scroll.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))

    current_tree.bind("<<TreeviewSelect>>", sync_selection_to_entries)
    total_tree.bind("<<TreeviewSelect>>", sync_selection_to_entries)
    id_tree.bind("<<TreeviewSelect>>", sync_selection_to_entries)

    bottom = ttk.LabelFrame(root, text="保存与实时预览")
    bottom.pack(fill="x", padx=16, pady=(0, 16))
    bottom.columnconfigure(1, weight=1)

    row_specs = [
        ("OFF_CURR", curr_var, preview_curr_var),
        ("OFF_TOTAL", total_var, preview_total_var),
        ("PTR_STATIC_OFFSET", ptr_var, preview_ptr_var)
    ]
    for row_idx, (label, variable, live_var) in enumerate(row_specs):
        tk.Label(bottom, text=label, width=18, anchor="w").grid(row=row_idx, column=0, padx=(12, 8), pady=8, sticky="w")
        tk.Entry(bottom, textvariable=variable).grid(row=row_idx, column=1, padx=(0, 8), pady=8, sticky="ew")
        tk.Label(bottom, textvariable=live_var, width=18, anchor="w").grid(row=row_idx, column=2, padx=(0, 12), pady=8, sticky="w")

    tk.Label(bottom, text="歌曲预览", width=18, anchor="w").grid(row=3, column=0, padx=(12, 8), pady=8, sticky="w")
    tk.Label(bottom, textvariable=preview_song_var, anchor="w", justify="left").grid(row=3, column=1, columnspan=2, padx=(0, 12), pady=8, sticky="w")

    tk.Label(bottom, text="最近保存", width=18, anchor="w").grid(row=4, column=0, padx=(12, 8), pady=8, sticky="w")
    tk.Label(bottom, textvariable=current_saved_var, anchor="w", justify="left").grid(row=4, column=1, columnspan=2, padx=(0, 12), pady=8, sticky="w")

    help_text = tk.Label(
        bottom,
        text=(
            "提示：如果当前进度为 00:00，很可能你在 0 秒附近扫描了太多无效 0 值。"
            "请把歌曲拖到 30 秒以上并暂停，再做首次扫描。歌曲ID页建议先复制网易云歌曲链接里的 id，再切歌后做继续筛选。"
        ),
        justify="left",
        wraplength=980
    )
    help_text.grid(row=5, column=0, columnspan=3, padx=12, pady=(0, 8), sticky="w")

    button_bar = tk.Frame(bottom)
    button_bar.grid(row=6, column=0, columnspan=3, padx=12, pady=(0, 12), sticky="w")
    tk.Button(button_bar, text="保存选中结果", command=save_layout, width=14).pack(side="left")
    tk.Button(button_bar, text="关闭", command=root.destroy, width=10).pack(side="left", padx=8)

    schedule_preview()
    root.mainloop()

def monitor_loop(v3, lrc_svc, locator):
    with auto.UIAutomationInitializerInThread():
        mode_svc = PlayModeService()
        
        pm = None
        mod = None
        base = None
        layout = None
        
        last_ct = -1.0
        last_tt = 0.0
        
        # 兼容旧逻辑变量
        last_switch_time = 0      
        is_waiting_stable = False 
        current_song_title_cache = ""
        last_title_check_time = 0
        
        # 内存ID记录
        last_memory_id = None
        invalid_progress_reads = 0
        invalid_pointer_reads = 0

        print("启动后台监控线程...")

        while True:
            try:
                # 1. 进程连接
                if pm is None:
                    try:
                        pm = pymem.Pymem("cloudmusic.exe")
                        mod = pymem.process.module_from_name(pm.process_handle, "cloudmusic.dll")
                        base = mod.lpBaseOfDll
                        layout = locator.resolve(pm, mod)
                        invalid_progress_reads = 0
                        invalid_pointer_reads = 0
                        last_memory_id = None
                        print(f"已连接到网易云音乐进程，偏移来源: {layout['source']}")
                        with state_lock:
                            API_STATE['process_active'] = True
                            API_STATE['memory_locator'] = locator.get_status()
                    except Exception as e:
                        print(f"[Locator] 连接进程失败: {e}")
                        with state_lock: 
                            API_STATE['process_active'] = False
                            API_STATE['playing'] = False
                            API_STATE['memory_locator'] = locator.get_status()
                        time.sleep(2)
                        continue

                # 2. 读取基础时间
                if layout is None:
                    layout = locator.resolve(pm, mod)
                    with state_lock:
                        API_STATE['memory_locator'] = locator.get_status()

                ct = MemoryUtils.read_double_safe(pm, base + layout["off_curr"])
                tt = MemoryUtils.read_double_safe(pm, base + layout["off_total"])
                if not locator.is_runtime_progress_valid(ct, tt):
                    invalid_progress_reads += 1
                    if invalid_progress_reads >= 3:
                        print("[Locator] 进度地址疑似失效，尝试自动重定位...")
                        layout = locator.resolve(pm, mod, force_rescan=True)
                        invalid_progress_reads = 0
                        with state_lock:
                            API_STATE['memory_locator'] = locator.get_status()
                        ct = MemoryUtils.read_double_safe(pm, base + layout["off_curr"])
                        tt = MemoryUtils.read_double_safe(pm, base + layout["off_total"])

                if not locator.is_runtime_progress_valid(ct, tt):
                    ct = last_ct if last_ct >= 0 else 0.0
                    tt = last_tt if last_tt > 0 else 0.0
                else:
                    invalid_progress_reads = 0

                is_moving = (ct != last_ct)
                last_ct = ct
                if tt > 0:
                    last_tt = tt

                current_mode = mode_svc.get_mode()

                # ==========================================
                # 3. ID 检测与元数据更新 (Metadata)
                # ==========================================
                song_id = API_STATE['basic_info'].get('id', 0)
                memory_id = MemoryUtils.read_pointer_chain_string(
                    pm,
                    base,
                    layout["ptr_static_offset"],
                    layout.get("ptr_offsets", locator.POINTER_OFFSETS)
                )
                if not memory_id:
                    invalid_pointer_reads += 1
                    if invalid_pointer_reads >= 5:
                        print("[Locator] 歌曲 ID 指针疑似失效，尝试自动重定位...")
                        layout = locator.resolve(pm, mod, force_rescan=True)
                        invalid_pointer_reads = 0
                        with state_lock:
                            API_STATE['memory_locator'] = locator.get_status()
                        memory_id = MemoryUtils.read_pointer_chain_string(
                            pm,
                            base,
                            layout["ptr_static_offset"],
                            layout.get("ptr_offsets", locator.POINTER_OFFSETS)
                        )
                else:
                    invalid_pointer_reads = 0

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
                # 4. 写入静态数据 (仅在切歌时执行)
                # ==========================================
                if current_track_full:
                    song_id = current_track_full.get("id") or song_id

                    song_name = current_track_full.get('name')
                    artists = current_track_full.get('artists') or current_track_full.get('ar', [])
                    all_artist_names = [a.get('name') for a in artists]
                    artist_display_str = " / ".join(all_artist_names) if all_artist_names else "未知歌手"
                    current_song_title_cache = f"{song_name} - {'/'.join(all_artist_names)}"
                    
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
                        API_STATE['memory_locator'] = locator.get_status()
                elif memory_id:
                    song_id = memory_id

                # 只要 current_mode 变了，next_song 就会立刻变
                prev_track, next_track = {}, {}
                
                if song_id:
                    # 使用最新的 ID 和 最新的 Mode 计算
                    prev_track, next_track = v3.get_playback_neighbors(song_id, current_mode)

                # ==========================================
                # 5. 写入动态数据 (进度/歌词/模式/邻居)
                # ==========================================
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
                pm = None
                mod = None
                base = None
                layout = None
                time.sleep(1)

# ===========================
# 4. Flask Web Server
# ===========================
app = Flask(__name__)
CORS(app)

# 初始化服务实例
v3 = NeteaseV3Service()
lrc_svc = LyricService() # 注意：这里需要改为全局单例，或者在 monitor_loop 里引用同一个实例
offset_resolver = CloudMusicOffsetResolver()

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
            "memory_locator": API_STATE["memory_locator"],
            "lyrics": {
                "current_line": API_STATE["lyrics"]["current_line"],
                "current_trans": API_STATE["lyrics"]["current_trans"]
            }
        }
        return Response(json.dumps(lite_state, ensure_ascii=False), mimetype='application/json')

@app.route('/debug/locator', methods=['GET'])
def get_locator_debug():
    return Response(
        json.dumps({
            "code": 200,
            "data": offset_resolver.get_status(),
            "cache_path": offset_resolver.get_cache_path()
        }, ensure_ascii=False),
        mimetype='application/json'
    )

@app.route('/debug/locator/manual', methods=['POST'])
def set_locator_manual():
    try:
        payload = request.get_json(silent=True) or {}
        off_curr = parse_offset_value(payload.get("off_curr"))
        off_total = parse_offset_value(payload.get("off_total"))
        ptr_static_offset = parse_offset_value(payload.get("ptr_static_offset")) if payload.get("ptr_static_offset") not in (None, "") else None

        if off_curr is None or off_total is None:
            return Response(
                json.dumps({"code": 400, "msg": "off_curr/off_total 不能为空"}, ensure_ascii=False),
                mimetype='application/json'
            )

        layout = offset_resolver.apply_manual_layout(off_curr, off_total, ptr_static_offset)
        with state_lock:
            API_STATE["memory_locator"] = offset_resolver.get_status()

        return Response(
            json.dumps({"code": 200, "data": layout}, ensure_ascii=False),
            mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({"code": 500, "msg": str(e)}, ensure_ascii=False),
            mimetype='application/json'
        )

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

@app.route('/control/<action>', methods=['POST'])
def control_player(action):
    """播放控制接口"""
    try:
        if action == 'prev':
            KeyboardHelper.press_shortcut([KeyboardHelper.VK_CTRL, KeyboardHelper.VK_ALT, KeyboardHelper.VK_LEFT])
        elif action == 'next':
            KeyboardHelper.press_shortcut([KeyboardHelper.VK_CTRL, KeyboardHelper.VK_ALT, KeyboardHelper.VK_RIGHT])
        elif action == 'playpause':
            KeyboardHelper.press_shortcut([KeyboardHelper.VK_CTRL, KeyboardHelper.VK_ALT, KeyboardHelper.VK_P])
        else:
            return Response(json.dumps({"code": 400, "msg": "Unknown action"}), mimetype='application/json')
            
        return Response(json.dumps({"code": 200, "msg": "success"}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"code": 500, "msg": str(e)}), mimetype='application/json')
    
@app.route('/', methods=['GET'])
@app.route('/player', methods=['GET'])
def serve_player():
    """托管前端 HTML 页面"""
    # 假设 player.html 和 main.py 在同一个文件夹下
    current_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(current_dir, 'player/player.html')
    
    if os.path.exists(html_path):
        return send_file(html_path)
    else:
        return Response("找不到 player.html，请确保它和 main.py 在同一目录", status=404)

@app.route('/wallpaper', methods=['GET'])
def serve_wallpaper():
    """托管 Wallpaper Engine 预览页面。"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    html_path = os.path.join(current_dir, 'wallpaper', 'index.html')

    if os.path.exists(html_path):
        return send_file(html_path)
    return Response("找不到 wallpaper/index.html", status=404)

@app.route('/<path:filename>')
def serve_static_files(filename):
    """托管仓库内的静态文件，便于浏览器预览和本地调试。"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    return send_from_directory(current_dir, filename)

@app.after_request
def add_header(response):
    response.cache_control.no_store = True
    return response

if __name__ == "__main__":
    if "--locator-gui" in sys.argv:
        launch_locator_gui(offset_resolver)
    else:
        # 在这里把全局的 service 传给 monitor
        t = threading.Thread(target=monitor_loop, args=(v3, lrc_svc, offset_resolver), daemon=True)
        t.start()
        print(f"API 服务已启动: http://127.0.0.1:18726/info")
        app.run(host='0.0.0.0', port=18726, debug=False)
