import pymem
import pymem.process
import time

# =================配置区域=================
PROCESS_NAME = "cloudmusic.exe"
MODULE_NAME = "cloudmusic.dll"
STATIC_OFFSET = 0x01DDE250

# 【重要】请填入你刚才测试成功的那个顺序！
# 如果你刚才是反转后成功的，请用这个：
OFFSETS = [0x10, 0, 0x10, 0x68, 0]
# =========================================

def read_string_from_memory(pm, addr, max_length=50):
    """
    从内存读取字符串，遇到 0x00 结束，或者达到最大长度
    """
    try:
        # 读取 bytes
        data = pm.read_bytes(addr, max_length)
        # 找到 null terminator 的位置 (C字符串以 \0 结尾)
        null_index = data.find(b'\x00')
        if null_index != -1:
            data = data[:null_index]
        # 解码
        return data.decode('utf-8', errors='ignore')
    except:
        return ""

def debug_pointer_chain(pm, module_base):
    print(f"\n--- 读取指针链 (String模式) ---")
    
    # 1. 基址
    entry_addr = module_base + STATIC_OFFSET
    
    try:
        # 64位指针读取
        current_ptr = pm.read_longlong(entry_addr)
    except:
        print("❌ 入口读取失败")
        return

    # 2. 遍历中间层
    for i, offset in enumerate(OFFSETS[:-1]):
        if current_ptr == 0: return
        next_addr = current_ptr + offset
        try:
            current_ptr = pm.read_longlong(next_addr)
        except:
            return

    # 3. 计算最终数据地址
    final_data_addr = current_ptr + OFFSETS[-1]
    print(f"Step Final: 数据地址 = {hex(final_data_addr)}")
    
    try:
        # 【核心修改】读取原始字符串
        raw_str = read_string_from_memory(pm, final_data_addr)
        print(f"        原始字符串: '{raw_str}'")
        
        # 【核心修改】清洗数据：按 "_" 分割
        if '_' in raw_str:
            clean_id = raw_str.split('_')[0]
            print(f"✅ 提取 ID: {clean_id}")
            return int(clean_id)
        elif raw_str.isdigit():
             print(f"✅ 提取 ID (纯数字): {raw_str}")
             return int(raw_str)
        else:
            print(f"❓ 格式无法识别: {raw_str}")
            return None
            
    except Exception as e:
        print(f"❌ 解析失败: {e}")

def main():
    try:
        pm = pymem.Pymem(PROCESS_NAME)
        mod = pymem.process.module_from_name(pm.process_handle, MODULE_NAME)
        base = mod.lpBaseOfDll
        
        while True:
            debug_pointer_chain(pm, base)
            time.sleep(1)
            
    except Exception as e:
        print(e)

if __name__ == "__main__":
    main()