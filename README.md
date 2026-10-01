# Live Captions Lab · Windows 11 实时字幕实验工具箱

Windows 11 自带「实时字幕」（Win+Ctrl+L），识别很准、全本地、不出网，但**微软没有提供任何"把字幕存成文件"的功能**。
本项目用 Windows UI Automation 读取字幕窗口，把字幕流抓出来做二次利用，并附带一套小工具。

> ⚠️ **先说清楚**：「抓取实时字幕并保存」这件事**不是首创**，已有不少开源实现（见文末「同类项目」）。
> 本仓库的独特价值在于两点：① 对字幕窗口**「累积日志」行为的工程化处理**（切句算法更抗乱序/重复）；
> ② 一套**上层应用**（语音遥控器 / 双语浮层 / 会议纪要 / 实时看板 / 声音日报），而不只是"存文件"。

---

## 核心洞察：字幕窗口是「累积日志」

实测（build 26200 / 25H2）搞清的字幕窗口真实行为，是本项目切句算法的基础：

1. 字幕窗口是**累积日志**：历史行**永不变**；
2. 新句子作为**新行追加在末尾**；
3. 正在说的那一句在**最后一行里逐字增长**（`今天` → `今天我们` → `今天我们讨论`）。

所以正确做法是**只盯最后一行**，用前缀关系判断它是在增长还是换了新句，换句时把上一句定稿输出。
（错误做法：把所有行拼接，会得到"后半段+前半段"的错序重复长串。）
在此基础上还处理了：占位文案误判、首句被吞、长句增量被去重规则吞、**先剥前缀再判冗余**（顺序不能反）、UIA 异常静默杀线程等坑。

---

## 功能地图

| 入口（`start.py <别名>`） | 脚本 | 作用 |
|---|---|---|
| `record` | `record.py` | **纯字幕记录**：开字幕即录、关窗口即停，逐句落盘 Markdown |
| `remote` | `voice_remote.py` | 语音遥控器：说触发词执行动作（记笔记/截图/开程序/发按键…） |
| `overlay` | `overlay.py` | 屏幕底部半透明**双语字幕浮层**（可开翻译，默认关） |
| `notes` | `meeting_notes.py` | 会议纪要：待办 / 决议 / 关键词 / 时间轴，导出 md + html |
| `dashboard` | `live_dashboard.py` | 实时看板：词云 / 语速曲线 / 热词 / 字幕流（浏览器打开） |
| `daily` | `demo_daily.py` | 声音日报：一天的字幕统计与高频词 HTML |
| `probe` | `probe.py` | 环境体检：能不能抓到字幕窗口 |
| `selftest` | `selftest_tts.py` | 闭环自测：念中文 → 转写 → 抓回 → 比对 |
| `test` | — | 跑一遍全部自检脚本 |

抓取核心在 `caption_source.py`（`CaptionTap` 类）：UIA 轮询 → 行级增量去重 → 带时间戳的事件流，并内置 **Markdown 自动归档**（按天一个 `字幕归档-YYYY-MM-DD.md`）。

---

## 安装

```bash
# 需要 Windows 11（实时字幕可用），Python 3.10+
pip install uiautomation pyautogui pyttsx3 pywin32 jieba
```

- `uiautomation`：UIA 读取字幕窗口（唯一稳定途径，微软无公开 API）。
- `pyautogui`：用快捷键开/关字幕、语音遥控器发按键。
- `pyttsx3`：闭环自测用的本地 TTS（仅测试需要，正式使用不需要）。
- `pywin32`：`overlay.py` 原生分层窗口渲染（避免 tkinter 在本环境不可用）。
- `jieba`：看板/日报的中文分词。

---

## 快速开始（记录字幕到 Markdown）

**方式 A · 一键（推荐）**：桌面双击 `记录字幕.bat` —— 它会自动按 `Win+Ctrl+L` 打开字幕并开始记录；
想停就**关掉实时字幕窗口**，脚本自动退出并把最后一句定稿保存。

**方式 B · 手动**：

```bash
# 1) 先按 Win+Ctrl+L 打开系统的「实时字幕」
# 2) 跑记录器
python record.py
# 3) 关闭字幕窗口即自动停止（也可 Ctrl+C）
```

归档文件在 `E:/Program/Zimu/字幕归档-YYYY-MM-DD.md`（每行 `- HH:MM:SS 文本`）。
可在 `caption_source.py` 改 `DEFAULT_MD_DIR`。

> 注意：系统的实时字幕**默认只转写系统播放的声音**。要记录你**自己说话**，需在字幕窗口设置里打开「包括麦克风音频」。
> 直接点控制台 × 强杀进程可能丢"正在说的最后一句"（已定稿的不丢）；关窗口或 Ctrl+C 都会正常保存。

---

## 其它入口示例

```bash
python start.py overlay      # 底部双语浮层（Ctrl+Alt+H 隐藏 / Ctrl+Alt+Q 退出）
python start.py notes --live # 边听边生成会议纪要
python start.py dashboard --port 8756   # 浏览器打开实时看板
python start.py remote       # 语音遥控器（说"记一下 …"等触发词）
python start.py probe        # 环境体检
```

---

## 工作原理

- 进程 `LiveCaptions.exe`；窗口 `ClassName = LiveCaptionsDesktopWindow`（跨语言一致）。
- 文本元素 `AutomationId = CaptionsTextBlock`（转写中）/ `ReadyToCaptionTextBlock`（空闲占位，需跳过）。
- 轮询间隔默认 0.1s；元素引用定期刷新（空闲态↔转写态会切换 ID）。
- 全本地离线：音频不出本机、不落盘；翻译默认关闭（开启会把字幕文本发到翻译接口）。

---

## 已知限制

- **微软无公开 API**：只能 UIA 读屏。窗口被移到屏幕外会导致 UIA 读取失败（最小化则仍可读）。
- 字幕**不含说话人信息**，纪要只能做"说了什么"，不能做"谁说的"。
- 情绪打分是小词典粗估，看板已注明"不能当情感分析结论"。
- 翻译默认关闭，因翻译接口会令字幕文本出网，破坏实时字幕的全本地优势。

---

## 同类项目（Related work）

「保存实时字幕」是已知痛点，已有不少实现，思路都是 UI Automation 读屏。列在这里以示不掠美：

- [`corbamico/get-livecaptions-rs`](https://github.com/corbamico/get-livecaptions-rs) — Rust，最早（2024-05），双证 MIT/Apache
- [`corbamico/get-livecaptions-cpp`](https://github.com/corbamico/get-livecaptions-cpp) — C++/WinRT 兄弟版
- [`tomjimlondon/get-livecaptions`](https://github.com/tomjimlondon/get-livecaptions) — Rust + Slint 界面 + OpenAI 问答
- [`SorMaze/LivecapSaver`](https://github.com/SorMaze/LivecapSaver) — Rust 命令行 TXT 保存
- [`LiveCaptionsHelper/SaveLiveCaptions`](https://github.com/LiveCaptionsHelper/SaveLiveCaptions) — Python，浮层小面板，MIT，活跃
- [`Tessie27/Live-Caption-Saver`](https://github.com/Tessie27/Live-Caption-Saver) — Python，OCR / Whisper 双模式
- [`bocaj2222/LiveCaptionsLoggerJap`](https://github.com/bocaj2222/LiveCaptionsLoggerJap) — Java + Tesseract OCR，源自一道微软官方论坛提问
- [`LunaTranslator`](https://github.com/HIllya51/LunaTranslator) — 翻译软件，内嵌"间接读取 LiveCaptions"模式

本项目与它们的差异：更稳健的"累积日志"切句算法 + 一整套上层应用（而非只做存文件）。

---

## License

[MIT](./LICENSE) © 2026 糖露星霜•暖霞拾光
