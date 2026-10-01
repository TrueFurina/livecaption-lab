"""
verify.py — 端到端自检：真的能从实时字幕里读出字吗？

这条链路里有三个环节都可能断：系统热键没唤起窗口、UIA 找不到窗口、
找到窗口但读不到文本。本脚本逐个验证并给出明确结论。

跑法：
    python verify.py            # 默认采样 20 秒
    python verify.py 45         # 采样 45 秒

注意：
    若实时字幕未运行，本脚本会发送 Win + Ctrl + L 尝试唤起它
    （这是官方热键，幂等；再按一次即可关闭窗口）。
    采样期间请让电脑播放点带人声的音频，否则窗口只显示占位文案。
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import uiautomation as auto  # noqa: E402

from caption_source import (  # noqa: E402
    CaptionTap,
    LC_WINDOW_CLASS,
    find_caption_window,
    _find_text_element,
    _element_text,
)

STEP = 0


def step(msg: str) -> None:
    global STEP
    STEP += 1
    print(f"\n[{STEP}] {msg}", flush=True)


def process_running() -> bool:
    import subprocess
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq LiveCaptions.exe"],
        capture_output=True, timeout=30,
    )
    return "LiveCaptions.exe" in out.stdout.decode("gbk", errors="replace")


def launch() -> bool:
    """用官方热键唤起实时字幕。"""
    try:
        import pyautogui
        pyautogui.hotkey("win", "ctrl", "l")
        return True
    except Exception as e:
        print(f"    热键发送失败：{e}")
        return False


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

    print("=" * 62)
    print("实时字幕抓取 · 端到端自检")
    print("=" * 62)

    step("检查字幕进程")
    if process_running():
        print("    LiveCaptions.exe 已运行")
    else:
        print("    未运行，发送 Win + Ctrl + L 唤起…")
        launch()
        for _ in range(15):
            time.sleep(1)
            if process_running():
                print("    已唤起")
                break
        else:
            print("    唤起失败。请手动按 Win + Ctrl + L 后重跑。")
            return

    step("UIA 定位字幕窗口")
    win = find_caption_window(timeout=15)
    if win is None:
        print(f"    找不到 ClassName={LC_WINDOW_CLASS}")
        print("    结论：抓取不可用")
        return
    print(f"    窗口：{win.Name!r}")

    step("UIA 定位文本元素")
    el = _find_text_element(win)
    if el is None:
        print("    窗口内无文本元素")
        return
    print(f"    AutomationId = {el.AutomationId!r}")
    print(f"    当前内容 = {_element_text(el)!r}")

    step(f"开始采样 {seconds:.0f} 秒（请播放带人声的音频）")
    got: list[str] = []

    def on_line(line):
        got.append(line.text)
        print(f"    [{line.iso}] {line.text}", flush=True)

    tap = CaptionTap(on_line=on_line, poll=0.1, md_dir=None)
    tap.start()
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    tap.stop()

    step("结论")
    if got:
        print(f"    抓取成功：{len(got)} 句")
        print(f"    样例：{got[0][:60]}")
        print("    地基可用，可以开始做上层项目了")
    else:
        print("    本次未抓到任何句子")
        print("    可能原因：没有播放人声音频 / 语言包不匹配 / 字幕处于占位状态")
        print("    请放一段中文视频后重跑本脚本")


if __name__ == "__main__":
    main()
