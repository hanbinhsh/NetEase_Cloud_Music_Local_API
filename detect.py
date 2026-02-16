import uiautomation as auto
import time
import os

def ui_inspector():
    os.system('cls' if os.name == 'nt' else 'clear')
    print("==================================================")
    print("   🖱️ 鼠标 UI 嗅探器")
    print("==================================================")
    print("请将鼠标【悬停】在网易云的【播放模式按钮】上...")
    print("如果是 Electron 应用，请特别留意 'LegacyIAccessiblePattern' 字段")
    print("按 Ctrl+C 退出")
    print("--------------------------------------------------")

    last_output = ""

    while True:
        try:
            # 获取鼠标当前位置下的控件
            element = auto.ControlFromCursor()
            
            # 获取控件的基本属性
            name = element.Name
            class_name = element.ClassName
            automation_id = element.AutomationId
            
            # 尝试获取 LegacyIAccessiblePattern (旧版接口，Electron 常用这个暴露信息)
            legacy_name = ""
            legacy_value = ""
            legacy_desc = ""
            try:
                pattern = element.GetLegacyIAccessiblePattern()
                if pattern:
                    legacy_name = pattern.Name
                    legacy_value = pattern.Value
                    legacy_desc = pattern.Description
            except:
                pass

            # 格式化输出
            output = (
                f"ClassName:  {class_name}\n"
                f"Name:       {name}\n"
                f"AutoId:     {automation_id}\n"
                f"LegacyName: {legacy_name}\n"
                f"LegacyVal:  {legacy_value}\n"
                f"LegacyDesc: {legacy_desc}\n"
            )

            # 只有当内容变化时才打印，防止刷屏
            if output != last_output:
                print("\n--- 捕捉到新控件 ---")
                print(output)
                last_output = output
            
            time.sleep(0.5)

        except KeyboardInterrupt:
            break
        except Exception:
            pass

if __name__ == "__main__":
    ui_inspector()