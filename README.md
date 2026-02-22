## NetEase Cloud Music Local API + PySide6 FluentWindow

项目包含两部分：

1. `main.py`：原始本地 API 服务（Flask + 网易云进程数据监控）
2. `fluent_app.py`：新的 PySide6 + qfluentwidgets 现代化桌面界面

### 新界面功能

`fluent_app.py` 提供三个页面：

- 主界面：启动服务按钮
- 设置界面：
  - 是否显示下一首/上一首封面
  - 是否显示音频频谱（当前网页播放器暂无频谱元素，保留设置）
  - 是否显示歌词原文/译文/罗马音
  - 是否优先使用逐字歌词
  - 是否显示播放模式
  - 快捷键设置（上一曲/下一曲/播放暂停）
- 网页播放器界面：内嵌 `player/player.html`

设置会立即作用到网页播放器（通过 URL 参数实时重载）。

### 运行

```bash
pip install PySide6 PySide6-WebEngine PyQt-Fluent-Widgets flask flask-cors pymem requests uiautomation
python fluent_app.py
```

### API 端点

- `/info`
- `/lyrics`
- `/playlist`
- `/history`
- `/queue`
