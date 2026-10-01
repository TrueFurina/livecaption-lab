"""
test_overlay.py — 浮层渲染自检（不需要你开口说话）

自动往浮层里塞几条字幕、开在线翻译、等译文到位后截图落盘，
这样能直接用眼睛确认：位置对不对、中文渲染对不对、译文有没有补上、
鼠标穿透和半透明有没有生效。

    python test_overlay.py
产物：shots/overlay-<时间戳>.png
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import win32con
import win32gui

from overlay import Overlay
from translator import get_translator

LINES = [
    "This is a real time caption test.",
    "Let's look at the throughput of the consensus mechanism.",
    "The reward function design has a problem here.",
]
SHOT_DIR = Path(__file__).with_name("shots")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    tr = get_translator("mymemory", target="zh-CN", source="en",
                        cache_path="cache/translate.json")
    ov = Overlay(lines=3, font=24, sub_font=18, alpha=232,
                 bottom_margin=80, ttl=60.0, translator=tr)
    if not ov.create():
        print("创建窗口失败")
        return 1
    print(f"窗口 hwnd={ov.hwnd} 尺寸 {ov.W}x{ov.H}")

    def feed():
        for i, s in enumerate(LINES):
            time.sleep(2.2 if i else 0.6)
            ov.push(s)
            print(f"  推入: {s}", flush=True)

    def shoot_and_quit():
        time.sleep(11.0)                       # 等三条都推完 + 译文回来
        SHOT_DIR.mkdir(exist_ok=True)
        try:
            import pyautogui
            img = pyautogui.screenshot()
            path = SHOT_DIR / f"overlay-{time.strftime('%H%M%S')}.png"
            img.save(str(path))
            print(f"截图已保存: {path}  ({img.size[0]}x{img.size[1]})")
        except Exception as e:
            print(f"截图失败: {type(e).__name__}: {e}")
        time.sleep(0.4)
        # DestroyWindow 只能由创建窗口的线程调用（Windows 规矩），
        # 这里发 WM_CLOSE 让 UI 线程自己走销毁流程
        try:
            win32gui.PostMessage(ov.hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception as e:
            print(f"PostMessage 失败: {e}")

    threading.Thread(target=feed, daemon=True).start()
    threading.Thread(target=shoot_and_quit, daemon=True).start()

    print("浮层显示中…（11 秒后自动截图并退出）")
    try:
        ov.run()
    except KeyboardInterrupt:
        pass
    print(f"翻译统计: {getattr(tr, 'stats', {})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
