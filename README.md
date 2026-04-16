# CloudMusic Local API

读取本地网易云音乐客户端的播放状态，并通过 Flask 提供本机 HTTP API。

项目支持：
- 当前歌曲信息、播放进度、歌词、播放列表、历史记录
- 浏览器播放器页面 `/player`
- Wallpaper Engine 页面 `/wallpaper`
- 内存偏移自动校验与缓存
- 引导式 GUI 偏移定位器

## 功能概览

- 高精度读取：
  通过 `cloudmusic.dll` 内存读取当前播放进度、总时长、歌曲 ID。
- 多层兜底：
  当内存读取失效时，回退到本地数据库、窗口标题和网易云接口补全信息。
- 歌词支持：
  支持原文、翻译、罗马音、逐字歌词。
- 本地页面：
  提供原有浏览器播放器页面与 Wallpaper Engine 预览页面。
- 偏移定位器：
  提供带 GUI 的手动定位工具，适合网易云版本更新后重新定位 `OFF_CURR`、`OFF_TOTAL`、`PTR_STATIC_OFFSET`。

## 环境要求

- Windows
- Python 3.10+
- 已安装并可运行网易云音乐桌面客户端

## 依赖安装

建议先创建虚拟环境，然后安装依赖：

```bash
pip install flask flask-cors pymem requests uiautomation
```

如果你还有其他本地依赖，请按你的实际环境补充。

## 启动方式

启动 API：

```bash
python main.py
```

默认地址：

- `http://127.0.0.1:18726/info`
- `http://127.0.0.1:18726/player`
- `http://127.0.0.1:18726/wallpaper`

启动偏移定位 GUI：

```bash
python main.py --locator-gui
```

## 偏移定位器用法

当网易云版本更新、旧偏移失效时，可以用 GUI 重新定位。

定位器目前包含 3 个标签页：

- `当前时长`
- `总时长`
- `歌曲ID`

推荐流程：

1. 打开网易云并开始播放歌曲。
2. 在“当前时长”页把进度拖到一个非零秒数后暂停，执行首次扫描。
3. 再拖到另一个秒数，执行继续筛选。
4. 在“总时长”页切到一首总时长明显不同的歌，再做首次扫描和继续筛选。
5. 如果歌曲 ID 不稳定，在“歌曲ID”页填入歌曲链接中的 `id`，执行扫描并在切歌后继续筛选。
6. 选中候选后，底部会实时预览：
   - 当前地址对应的当前秒数
   - 当前地址对应的总时长
   - 当前 `PTR_STATIC_OFFSET` 对应的歌曲 ID
   - 歌曲名称 / 歌手 / 专辑
7. 确认无误后点击“保存选中结果”。

保存后的结果会写入：

- `offset_cache.json` 的 `manual_override`
- 当前网易云版本指纹对应的 `entries[...]`

这样下次启动时会优先使用保存过的结果。

## 接口说明

- `GET /info`
  返回当前播放状态、歌曲信息、播放进度、歌词当前行、内存定位状态。
- `GET /lyrics`
  返回完整歌词包。
- `GET /history`
  返回最近播放历史。
- `GET /playlist`
  返回本地歌单信息。
- `GET /queue`
  返回当前播放队列。
- `POST /control/prev`
  上一首。
- `POST /control/next`
  下一首。
- `POST /control/playpause`
  播放 / 暂停。
- `GET /debug/locator`
  返回当前偏移定位状态和缓存路径。
- `POST /debug/locator/manual`
  手动提交偏移，适合外部脚本或自定义工具调用。

## 页面说明

- `/player`
  原有网页播放器。
- `/wallpaper`
  Wallpaper Engine 预览页。

更完整的 Wallpaper Engine 使用说明见 [wallpaper/README.md](wallpaper/README.md)。

## 偏移机制说明

项目当前采用两层策略：

- 自动策略：
  启动时优先读取缓存或已知候选偏移，并对结果做运行时校验。
- 手动策略：
  自动定位不可靠时，使用 GUI 定位器进行人工筛选并保存。

注意：

- 网易云更新后，`PTR_STATIC_OFFSET`、`OFF_CURR`、`OFF_TOTAL` 可能变化。
- 多级指针偏移 `PTR_OFFSETS = [0x10, 0, 0x10, 0x68, 0]` 通常更稳定，但也不保证绝对不变。
- 如果网页播放器一直显示 `00:00`，通常意味着当前进度地址定位错误，建议重新运行定位器。

## 目录结构

```text
main.py                 Flask API 与监控主程序
offset_cache.json       偏移缓存
player/                 浏览器播放器页面
wallpaper/              Wallpaper Engine 页面
ce/                     与偏移定位相关的辅助资料
old/                    历史文件
```

## 发布建议

如果你准备公开发布，建议补充：

- `requirements.txt`
- 截图或 GIF
- 已测试的网易云版本号
- 常见问题，例如：
  - GUI 能打开但扫不到值
  - `/info` 一直是 `00:00`
  - 歌词或歌曲信息读取失败

## 参考

参考并借鉴了：

- `https://github.com/Widdit/now-playing-service`

并在此基础上补充了：

- 进度条拖动后的纠偏
- 多层兜底逻辑
- Wallpaper Engine 页面
- GUI 偏移定位器
