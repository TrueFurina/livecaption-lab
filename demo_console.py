"""
demo_console.py — 最小可跑 demo：把系统字幕实时打印到控制台并落盘

跑法：
    1) 按 Win + Ctrl + L 打开实时字幕
    2) 随便放个带人声的视频 / 开个会议
    3) python demo_console.py
    4) Ctrl+C 结束

落盘：logs/captions-<日期>.jsonl，每行 {"text","ts","iso","seq"}
后续可用 demo_daily.py 把这份日志渲染成可视化日报。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from caption_source import CaptionTap, CaptionLine  # noqa: E402

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"captions-{datetime.now():%Y-%m-%d}.jsonl"

STATUS_TEXT = {
    "ready": "已连接字幕窗口，等待音频",
    "waiting": "字幕窗口空闲中（占位状态）",
    "capturing": "正在转写",
    "lost": "字幕窗口丢失 —— 请按 Win + Ctrl + L",
}


def on_line(line: CaptionLine) -> None:
    print(f"\033[96m[{line.iso}]\033[0m #{line.seq:>4}  {line.text}", flush=True)


def on_status(status: str) -> None:
    prev = getattr(on_status, "prev", None)
    if status != prev:
        print(f"\033[93m>> {STATUS_TEXT.get(status, status)}\033[0m", flush=True)
        on_status.prev = status


def main() -> None:
    print("=" * 62)
    print("实时字幕抓取 demo")
    print("=" * 62)
    print(f"日志文件：{LOG_FILE}")
    print("按 Ctrl+C 结束\n")

    tap = CaptionTap(on_line=on_line, on_status=on_status, poll=0.1, jsonl=LOG_FILE)
    if not tap.start(wait_window=10.0):
        print("未检测到实时字幕窗口。请先按 Win + Ctrl + L 打开，再重跑。")
        return

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n停止中…")
        tap.stop()
        lines = tap.snapshot()
        print(f"本次共抓到 {len(lines)} 句，已写入 {LOG_FILE}")


if __name__ == "__main__":
    main()
