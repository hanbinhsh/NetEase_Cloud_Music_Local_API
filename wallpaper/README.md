# CloudMusic Wallpaper 导入说明

这个目录是给 Wallpaper Engine 的网页壁纸入口，核心文件是 `index.html`。

## 先决条件
1. 安装并启动网易云音乐桌面版。
2. 在本机启动 `main.py`，确认 `http://127.0.0.1:18726/info` 可以访问。
3. 如果你想让壁纸待机时显示自己的背景图，先准备一张本地图或可访问的图片地址。

## 本地预览
先运行 Flask 服务，然后访问：

```text
http://127.0.0.1:18726/wallpaper
```

这个页面就是导入 Wallpaper Engine 后的主要效果。

## 导入到 Wallpaper Engine
1. 打开 Wallpaper Engine。
2. 选择“创建壁纸”。
3. 类型选择“网页”。
4. 入口文件选择当前项目下的 `wallpaper/index.html`。
5. 保存后在编辑器中预览。

如果编辑器要求设置项目标题、封面图、描述，按你自己的习惯填写即可。

## 推荐添加的用户属性
这个壁纸已经实现了 `window.wallpaperPropertyListener.applyUserProperties`，建议在 Wallpaper Engine 编辑器里添加这些属性键：

- `apibase`
  - 类型：文本
  - 默认值：`http://127.0.0.1:18726`
- `standbybg`
  - 类型：文件
  - 用途：API 不可用或网易云未播放时的待机背景
- `compactlayout`
  - 类型：布尔
  - 用途：切换到参考图那种卡片式布局
- `showclock`
  - 类型：布尔
  - 默认值：`true`
- `showlyrics`
  - 类型：布尔
  - 默认值：`true`
- `showtranslation`
  - 类型：布尔
  - 默认值：`true`
- `showroma`
  - 类型：布尔
  - 默认值：`false`
- `showwordbyword`
  - 类型：布尔
  - 默认值：`true`
- `showspectrum`
  - 类型：布尔
  - 默认值：`true`
- `enablemouse`
  - 类型：布尔
  - 默认值：`true`
- `lowperf`
  - 类型：布尔
  - 默认值：`false`
- `blurstrength`
  - 类型：滑块
  - 建议范围：`8-48`
- `mousestrength`
  - 类型：滑块
  - 建议范围：`0-36`
- `themecolor`
  - 类型：颜色
  - 用途：手动覆盖主题色

## 已实现的桌面特性
- 毛玻璃卡片与模糊背景
- 双布局切换
  - 默认是较完整的信息布局
  - 可切换为更接近示例图的卡片式桌面布局
- 鼠标轻量视差追踪
- 时间与日期显示
- 待机背景图
- 歌曲信息、进度与控制按钮
- 上一首/下一首封面缓存
- 背景切换淡入动画
- 当前歌词与滚动歌词列表
- Wallpaper Engine 音频监听频谱
- 低性能模式开关

## 使用建议
- 如果你只想在本机使用，`apibase` 保持 `http://127.0.0.1:18726` 就够了。
- 如果出现白屏，优先检查 Flask 服务是否启动、`/info` 是否能访问，以及 Wallpaper Engine 是否允许该网页访问本机 HTTP 接口。
- 如果机器性能一般，可以开启 `lowperf`，同时降低 `blurstrength` 和 `mousestrength`。
