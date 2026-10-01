"""
debug_raw.py — 把字幕窗口的原始文本演化原样打出来

用来看清 ASR 到底怎么更新窗口：是逐字追加、整段替换、还是多行重排。
看清楚之后才谈得上设计切句算法。

跑法：
    python debug_raw.py [秒数]
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from caption_source import (  # noqa: E402
    find_caption_window,
    _find_text_element,
    _element_text,
)

SENTENCES = [
    "今天我们讨论多智能体强化学习的收敛性问题",
    "这个实验结果比基线方法要好一些",
    "区块链共识机制的效率还有优化空间",
]


def main() -> None:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 18.0

    win = find_caption_window(timeout=15)
    if win is None:
        print("未找到字幕窗口，先按 Win + Ctrl + L")
        return
    print(f"窗口：{win.Name!r}\n")

    speaker = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("_speak.py"))] + SENTENCES
    )

    el = _find_text_element(win)
    last = None
    t_end = time.time() + seconds
    n = 0
    while time.time() < t_end:
        if el is None:
            el = _find_text_element(win)
        try:
            raw = _element_text(el) if el else ""
        except Exception as e:
            raw = f"<err {e}>"
        if raw != last:
            n += 1
            ts = time.strftime("%H:%M:%S")
            lines = [l for l in raw.splitlines() if l.strip()]
            print(f"--- #{n} {ts}  行数={len(lines)}", flush=True)
            for i, l in enumerate(lines):
                print(f"      [{i}] {l}", flush=True)
            last = raw
        time.sleep(0.2)

    if speaker.poll() is None:
        speaker.kill()
    print("\n观察要点：文本是追加增长，还是整段被替换？行序会不会重排？")


if __name__ == "__main__":
    main()
