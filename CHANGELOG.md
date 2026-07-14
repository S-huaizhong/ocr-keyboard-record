# Changelog

本项目版本记录，遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格。

## [1.1.3] - 2026-07-14

托盘菜单崩溃 + 剪贴板自复制循环修复。

### Fixed
- **托盘右键菜单在 64-bit Python 下崩溃**：`AppendMenuW` / `TrackPopupMenu` / `DestroyMenu` /
  `SetForegroundWindow` / `CreatePopupMenu` 缺少 `ctypes` 的 `argtypes` 声明，`ctypes` 会把未声明
  参数当作 `c_int` 处理，导致子菜单场景下 64-bit `HMENU` 指针触发 `OverflowError: int too long`。
  给 `AppendMenuW` 的 `uIDNewItem` 用 `ctypes.c_void_p`（兼容命令 ID 与 `HMENU` 指针两种情况），
  其他菜单相关 API 也统一补齐 `argtypes`。
- **“全选复制”触发自复制循环**：`_copy_all` 把渲染 body 写入剪贴板并调用 `suppress_next` 登记 3 秒抑制，
  但 Windows 剪贴板往返时将 `\n` 规范化为 `\r\n`，字符串比较不等 → 抑制失效 → 内容被记回 log →
  下次全选复制时又把这条膨胀内容再复制一遍，形成递归膨胀链。
  修复：`suppress_next` / `_consume_suppress` 双向做换行归一化（`\r\n → \n`、`\r → \n`）。
- **兜底防线**：即使抑制失败，`ClipboardWatcher` 也会检查读回文本是否含有本工具自己的剪贴板块标记
  `───[剪贴板 xxx]───` / `───[Clipboard xxx]───`（同时含起止），命中即直接丢弃不写入 log。

## [1.1.2] - 2026-07-14

环境部署 + 内部标签格式修正。

### Changed
- **部署方案升级为项目专用虚拟环境**：新增 `setup.bat`，双击一键完成搜索 Python + 创建 `.venv/` + 安装依赖；
  `run_screen_search.bat` / `run_screen_search_debug.bat` / `install_autostart.bat` 三个启动脚本均优先使用项目本地 `.venv`，
  不再依赖全局 Python 安装路径硬编码。
- **键盘记录特殊键标签封样改为 `[*xxx]`**（比如 `[*Ctrl+C]` `[*Enter]` `[*→]`），避免与源代码/终端里的普通 `[xxx]` 语法碰撞。
  输入记录窗口的高亮/剪除正则同步升级。旧日志中已存在的 `[xxx]` 标签不会被新正则命中，不影响程序启动，
  但也不再作为“特殊键”置灰。

### Added
- **OCR 悬浮搜索栏右端新增✖关闭按钮**：除 `Esc`、右键、点空白之外多一个直接关闭入口。
- `.gitignore` 补充排除 `.venv/`、`build/`、`dist/`、`*.spec` 等打包产物。

### Fixed
- 修正 `run_screen_search.bat` / `install_autostart.bat` / `requirements.txt` 中文乱码（前一版介质将 GBK 编码写成 UTF-8 + LF 换行，导致 cmd.exe 无法正确解析）。

## [1.1.1] - 2026-07-13

安全与稳定性修复版本，不变更现有日志格式（JSONL 向后兼容）。

### Security
- **P0-1 隐私默认 opt-in**：键盘记录 / 剪贴板监听 / 中文 IME 捕捉三项开关首次启动均为关闭，
  必须在输入记录面板（`Ctrl+Alt+X`）中手动启用。开关状态保存到 `config.json`，
  托盘工具提示实时反映当前录制状态。
- **P0-2 登录自启拆分为两档**：新增普通用户权限自启选项，不使用 `/RL HIGHEST`，
  仅 OCR / 剪贴板 / 当前用户层面键盘记录，不再默认提供提权持久化通道。
- **P0-2 任务名含脚本路径 hash 后缀**：`ScreenSearch_Full_<sha1[:8]>` / `ScreenSearch_UserOnly_<sha1[:8]>`，
  同一主机上多份安装不会相互覆盖。旧名 `ScreenSearch` 仍可被识别为已安装，卸载时会一并清理。
- **P0-6 剪贴板自循环拑截**：“全选复制”/“复制选区”/选词复制写入剪贴板时，
  同时登记 3s 抑制预期，避免本程序自己写的内容又被剪贴板监听器重新记入日志。

### Fixed
- **P0-3 OCR 竞态**：为每次 OCR 捕获分配递增 generation；超时发生时提升 generation，
  旧 OCR 线程即使后来返回也无法污染新任务字典；RapidOCR 实例调用添加互斥锁，防止同时开两次捕获时中间状态碰撞。
- **P1-8 UIA 中文 IME 捕捉**：控件标识优先用 `RuntimeId`（而非 hwnd/Name 元组），
  避免同一控件 Name 变化时被误认为新控件重置基线；新增同一控件 2 秒内重复提交同一段 CJK 文本去重；
  `val` 变短时直接重置基线，不再当作“新增”处理。
- **P1-1 README 与实际行为同步**：`kind` 字段的实际取值重新列为 `key` / `ime` / `clip`，
  保留清理时机修正为“启动时 + 每天首次追写时”（而非日零点定时），OCR 释放时机修正为“下一次 OCR 开始时”。

### Added
- 输入记录面板新增三个隐私开关【⌨️ 键盘记录】【📋 剪贴板监听】【🆎 中文 IME 捕捉】，均默认关闭。
- 输入记录面板新增【🚩 开机启动(普通)】复选框（无需 UAC，仅当前用户权限）。
- 托盘 tooltip 新增一行录制状态提示，例如 `Recording: K+C` / `Recording: OFF`。
- `requirements.txt` 添加可选依赖 `comtypes` / `uiautomation`（中文 IME 捕捉）；未安装时，【🆎 中文 IME 捕捉】开关自动置灰，其他功能不受影响。

### Changed
- `KeyLogStore` 配置文件格式升级（向后兼容）：`config.json` 现以 UTF-8 + `indent=2` 写入，
  新增字段 `keylog_enabled` / `clipboard_enabled` / `uia_enabled`，旧字段 `retention_days` 保留。
- **登录自启两档互斥**：【🚀 开机启动(管理员)】与【🚩 开机启动(普通)】任一声明开启时，会自动卸载另一档已存在的任务（避免两档共存重复拉起相同进程）。

## [1.0.0] - 2026-07-13

### Added
- 全局屏幕 OCR 搜索（`Ctrl+Alt+F`）
  - RapidOCR (PP-OCRv4) 主引擎 + Windows `winocr` 回退
  - 覆盖层实时搜词、逐项高亮、跳转 `Enter` / `Shift+Enter`
  - "选词模式"：从 OCR 结果框选文本并复制
  - "重新识别"：页面变化时不必关窗重抓
- 键盘 + 剪贴板输入记录（`Ctrl+Alt+X`）
  - 内核级键盘 hook 记录物理键
  - 剪贴板复制事件同步记录（可关）
  - JSON Lines 追加、按天保留 3/7/15 天
- 系统托盘图标 + 中英双语界面
- Windows 计划任务登录自启（`install_autostart.bat`）
- 独立项目结构 + MIT LICENSE + 完整 README

### Known Issues
详见 [README.md § 已知问题](./README.md#已知问题-known-issues)：
- **屏幕 OCR** 中文/含空格行高亮框会向右累积漂移（RapidOCR 无字符级 bbox + DBNet 吞空格）
- **输入记录** 无法捕获中文输入法上屏文字，只能拿到拼音 + 选字数字混杂串（内核 hook 无法穿透 TSF/IMM32）
- 极端宽高比图像可能零检测（DBNet 训练分布限制）

### Authors
- **S.huaizhong** — 设计与决策
- **LaoXia (ark-code-latest)** — 实现（OpenClaw agent 协作）
