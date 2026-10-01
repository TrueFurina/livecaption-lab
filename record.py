"""record.py — 实时字幕纯记录器（自动落盘到 Markdown）

它是什么 / 不是什么
------------------
- **不是守护进程、不是开机自启服务**。本脚本只在你"想录"的时候跑，
  你不用它的时候，系统里没有任何相关进程，零占用、零资源浪费。
- **事件驱动退出**：打开实时字幕窗口 → 跑本脚本 → 实时记录；
  一旦你关闭字幕窗口，脚本自动停止并把最后一句定稿保存，无需手动 Ctrl+C。
- 微软没有提供"把字幕存成文件"的功能，所以必须有个进程在字幕开着期间
  读取窗口并落盘——但这个过程只在你用的时候存在，关掉字幕它就消失。

用法（两种，任选其一）
----------------------
方式 A · 手动：
  1) 按 Win + Ctrl + L 打开系统的"实时字幕"
  2) python record.py           开始记录
  3) 关闭实时字幕窗口 —— 脚本会自动停止并保存（也可按 Ctrl+C 手动停）
  4) 去 E:/Program/Zimu 看今天的归档文件

方式 B · 一键（推荐）：
  桌面双击"记录字幕.bat"——它会自动帮你打开实时字幕并开始记录；
  你关掉字幕窗口即自动停止。全程不需要你管进程。

注意
----
  - 系统的实时字幕不会自己写文件，必须运行本脚本（仅在你录制期间）才落盘。
  - 直接点控制台右上角 × 强杀进程，可能丢失"正在说的最后一句"；
    关掉字幕窗口或按 Ctrl+C 都会正常定稿保存，请优先用这两种方式退出。
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from caption_source import CaptionTap, DEFAULT_MD_DIR, find_caption_window  # noqa: E402


def _ensure_window() -> bool:
    """确保实时字幕窗口已存在。没有就尝试用系统快捷键打开一次。"""
    if find_caption_window(timeout=1.0) is not None:
        return True
    try:
        import pyautogui
        pyautogui.hotkey("win", "ctrl", "l")   # 切换实时字幕（关闭态→打开）
    except Exception:
        pass
    return find_caption_window(timeout=8.0) is not None


def main() -> int:
    md_dir = DEFAULT_MD_DIR  # 真实监听必须落到真实归档目录
    tap = CaptionTap(md_dir=md_dir)

    count = 0

    def on_line(line):
        nonlocal count
        count += 1
        print(f"  [{line.iso}] {line.text}", flush=True)

    def on_status(s):
        if s == "lost":
            print("[结束] 实时字幕窗口已关闭，记录停止。", file=sys.stderr, flush=True)
        elif s.startswith("error"):
            print(f"[错误] {s}", file=sys.stderr, flush=True)

    tap.on_line = on_line
    tap.on_status = on_status

    def shutdown(*_):
        print("\n正在保存最后一句并退出…", flush=True)
        try:
            tap.stop()
        except Exception:
            pass
        print(f"本次共记录 {count} 句 → {md_dir}", flush=True)
        os._exit(0)

    # Ctrl+C / 任务结束信号：先把最后一句定稿保存再退出
    try:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    except Exception:
        pass
    # 兜底：任何正常退出路径都尝试 flush 一次
    atexit.register(lambda: tap.stop())

    if not _ensure_window():
        print(
            "未能打开/检测到实时字幕窗口。\n"
            "请先按 Win + Ctrl + L 打开系统的「实时字幕」，再重跑：python record.py",
            file=sys.stderr, flush=True,
        )
        return 1

    # 窗口已就绪，wait_window=0 直接启动（start 内部会写"记录开始"分隔行）
    if not tap.start(wait_window=0.0):
        print("字幕窗口已存在，但抓取启动失败。", file=sys.stderr, flush=True)
        return 1

    print(f"字幕记录已开始，自动保存到：{md_dir}", flush=True)
    print("关闭实时字幕窗口即自动停止（也可按 Ctrl+C 手动停）", flush=True)

    # 事件驱动主循环：只要抓取线程还活着就继续；
    # 字幕窗口一旦关闭，CaptionTap 会自行结束线程，这里也跟着退出。
    try:
        while tap.is_running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown()
    shutdown()   # 自然退出（窗口关闭）时也走一遍定稿保存
    return 0


if __name__ == "__main__":
    sys.exit(main())
