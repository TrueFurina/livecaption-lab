"""
test_e2e_overlay.py — 端到端联调：TTS 说话 -> 系统字幕 -> 抓取 -> 浮层双语显示

这是浮层的最终验收：不注入任何假数据，全部走真实链路。
前置：本机已开实时字幕（脚本会自动尝试打开）。

    python test_e2e_overlay.py
产物：shots/e2e-<时间戳>.png
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import win32con
import win32gui

from caption_source import CaptionTap, restart_livecaptions
from overlay import Overlay
from translator import get_translator

SENTENCES = [
    "今天我们讨论一下多智能体强化学习的收敛性问题。",
    "这个实验结果比基线好了百分之五。",
    "下一阶段要补充更多的对比实验。",
]
SHOT_DIR = Path(__file__).with_name("shots")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("1) 确保实时字幕窗口打开 …")
    if not restart_livecaptions():
        print("   打开失败，退出")
        return 1

    # 中文原文 -> 英文译文（演示双语；看英文视频时反过来用即可）
    tr = get_translator("mymemory", target="en", source="zh-CN",
                        cache_path="cache/translate_zh2en.json")
    ov = Overlay(lines=3, font=24, sub_font=18, bottom_margin=80, ttl=45.0, translator=tr)
    if not ov.create():
        print("创建浮层失败")
        return 1

    n_lines = [0]

    def on_line(line):
        n_lines[0] += 1
        print(f"   字幕#{line.seq}: {line.text}", flush=True)
        ov.push(line.text)

    tap = CaptionTap(on_line=on_line, flush_idle=2.0, md_dir=None)
    tap.start()
    print("2) 抓取已启动，3 秒后开始说话 …")
    time.sleep(3.0)

    speaker = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("_speak.py"))] + SENTENCES,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def shoot_and_quit():
        time.sleep(26.0)                      # 说话 ~18s + 翻译收尾
        speaker.terminate()
        SHOT_DIR.mkdir(exist_ok=True)
        try:
            import pyautogui
            p = SHOT_DIR / f"e2e-{time.strftime('%H%M%S')}.png"
            pyautogui.screenshot().save(str(p))
            print(f"3) 截图已保存: {p}")
        except Exception as e:
            print(f"   截图失败: {e}")
        time.sleep(0.3)
        try:
            win32gui.PostMessage(ov.hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass

    threading.Thread(target=shoot_and_quit, daemon=True).start()
    print("浮层运行中 …（26 秒后自动截图退出）")
    try:
        ov.run()
    except KeyboardInterrupt:
        pass
    finally:
        tap.stop()
        if hasattr(tr, "flush"):
            tr.flush()
    print(f"共抓到 {n_lines[0]} 句字幕；翻译统计 {getattr(tr, 'stats', {})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
