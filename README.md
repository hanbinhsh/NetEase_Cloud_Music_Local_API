使用 CE 查找网易云音乐播放器的播放时间基址和偏移量，通过多级指针访问内存中存储的歌曲 ID 并比对数据库。网易云版本不对时，相关地址可能需要调整。

内含多种兜底逻辑，支持罗马音/逐字歌词显示。

## 接口与页面
- `/` 或 `/player`：原有浏览器播放器页面
- `/wallpaper`：新的 Wallpaper Engine 预览页
- `/info`：基础信息
- `/lyrics`：歌词
- `/playlist`：歌单列表
- `/history`：播放历史
- `/queue`：当前播放队列

## Wallpaper Engine 用法
新的壁纸页面位于 [wallpaper/index.html](wallpaper/index.html)，设计为“本机 Flask API + Wallpaper Engine Web 壁纸”模式：

1. 先启动 `main.py`
2. 在浏览器里访问 `http://127.0.0.1:18726/wallpaper` 预览效果
3. 在 Wallpaper Engine 中选择“创建壁纸” -> “网页” -> 指向 `wallpaper/index.html`
4. 在壁纸属性里添加或映射这些用户属性键：
   - `apibase`：API 地址，默认 `http://127.0.0.1:18726`
   - `standbybg`：待机背景图
   - `compactlayout`：切换到参考图风格的卡片布局
   - `showclock`：显示时间
   - `showlyrics`：显示歌词
   - `showtranslation`：显示歌词翻译
   - `showroma`：显示罗马音
   - `showwordbyword`：显示逐字歌词
   - `showspectrum`：显示频谱
   - `enablemouse`：鼠标追踪
   - `lowperf`：低性能模式
   - `blurstrength`：毛玻璃强度
   - `mousestrength`：鼠标位移强度
   - `themecolor`：主题色

更完整的导入步骤和属性建议见 [wallpaper/README.md](wallpaper/README.md)。

## 参考
参考了 `https://github.com/Widdit/now-playing-service` 的代码，并完善了手动拖动进度条导致的进度不匹配问题。
