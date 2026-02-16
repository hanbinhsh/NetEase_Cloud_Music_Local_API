import uiautomation as auto
import time

def monitor_netease_play_mode_optimized():
    # 模式名称映射
    mode_map = {
        "loop": "列表循环 (List Loop)",
        "singleloop": "单曲循环 (Single Loop)",
        "shuffle": "随机播放 (Shuffle)",
        "order": "顺序播放 (Sequential)"
    }

    print("==================================================")
    print("   📡 网易云播放模式实时监控 (高性能优化版)")
    print("==================================================")
    
    # 1. 锁定网易云主窗口
    window = auto.WindowControl(searchDepth=1, ClassName="OrpheusBrowserHost")
    
    if not window.Exists(0):
        print("❌ 未找到网易云音乐窗口，请确保程序已启动。")
        return

    print("✅ 已连接主窗口，正在定位底部控制栏...")

    # ================= 核心优化：定位并缓存控制栏 =================
    control_bar = None
    
    # 尝试寻找任意一个模式按钮，以此来定位它们的“父级容器”
    # 我们给予稍微长一点的超时时间(3秒)，确保第一次能找到
    for ui_name in mode_map.keys():
        print(f"🔍 正在尝试定位 UI 锚点: {ui_name} ...")
        # 第一次寻找必须深搜，但只做一次
        target_btn = window.ButtonControl(searchDepth=15, Name=ui_name)
        if target_btn.Exists(0.5): # 快速探测
            # 找到按钮后，获取它的父控件（通常是底部的控制条容器）
            control_bar = target_btn.GetParentControl()
            print(f"✅ 成功定位控制栏！父级对象: {control_bar.Name} ({control_bar.ControlType})")
            break
    
    if not control_bar:
        print("⚠️ 无法定位到底部控制栏，可能是当前模式按钮被隐藏或界面版本不兼容。")
        print("   建议：手动切换一次播放模式，让按钮刷新出来后再运行脚本。")
        return

    print("--------------------------------------------------")
    print("🚀 监控已启动 (优化模式: 仅扫描控制栏区域)")

    last_mode = None

    try:
        while True:
            found_mode_key = None
            
            # ================= 优化后的检测循环 =================
            # 直接在 control_bar 下寻找，Depth 设为 1 即可，极快
            
            # 方法 A: 遍历子元素 (最高效，不需要 4 次搜索)
            # 获取控制栏下的所有一级子元素，检查名字是否在映射表中
            children = control_bar.GetChildren()
            for child in children:
                if child.Name in mode_map:
                    found_mode_key = child.Name
                    break 
            
            # 方法 B (备用): 如果方法 A 找不到，可以用 searchDepth=2 再搜一次特定的按钮
            if not found_mode_key:
                for ui_name in mode_map.keys():
                    if control_bar.ButtonControl(searchDepth=2, Name=ui_name).Exists(0):
                        found_mode_key = ui_name
                        break

            # 3. 状态处理
            if found_mode_key != last_mode:
                if found_mode_key:
                    mode_text = mode_map[found_mode_key]
                    print(f"[{time.strftime('%H:%M:%S')}] 🎵 模式切换 -> {mode_text}")
                else:
                    # 如果突然找不到，可能是窗口最小化导致不渲染，或者是UI刷新了
                    # 可以在这里加一个逻辑：如果连续多次找不到，重新触发“寻找父级容器”的逻辑
                    print(f"[{time.strftime('%H:%M:%S')}] ⏳ 暂无数据 (窗口最小化或遮挡)")
                
                last_mode = found_mode_key

            # 4. 频率控制
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n监控已停止。")
    except Exception as e:
        print(f"\n❌ 发生意外错误 (可能是UI重绘导致句柄失效): {e}")

if __name__ == "__main__":
    monitor_netease_play_mode_optimized()