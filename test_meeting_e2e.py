"""
test_meeting_e2e.py — 纪要端到端：TTS 念会议 → 系统转写 → 抓取 → 生成纪要

和 test_meeting.py 的区别：那份是"喂干净文本"，这份是"真的说出来"，
ASR 会有识别误差、会串句、会补标点，用来检验真实条件下的鲁棒性。

    python test_meeting_e2e.py
产物：reports/meeting-e2e-<时间戳>.md / .html
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from caption_source import CaptionTap, restart_livecaptions
from meeting_notes import MeetingRecorder, save

SPEECH = [
    "今天我们讨论一下多智能体强化学习的收敛性问题。",
    "实验结果显示比基线好了百分之五，但方差还是偏大。",
    "结论是采用 VDN 加 CARS 的组合方案。",
    "需要尽快补充对比实验，明天之前把结果发出来。",
    "记得把超参数整理成表格，下周一同步给大家。",
    "那就这么定下来，下一阶段按这个方案推进。",
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("1) 打开实时字幕 …")
    if not restart_livecaptions():
        print("   失败，退出")
        return 1

    rec = MeetingRecorder()
    tap = CaptionTap(on_line=lambda line: (rec.add(line.text, line.ts),
                                           print(f"   #{line.seq} {line.text}", flush=True)),
                     md_dir=None)
    tap.start()
    print("2) 开始说话 …")
    time.sleep(3.0)

    speaker = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("_speak.py"))] + SPEECH,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def finish():
        time.sleep(45.0)
        speaker.terminate()
        time.sleep(1.5)
        tap.stop()

    threading.Thread(target=finish, daemon=True).start()

    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1.0)
        if not tap._running:
            break

    st = rec.stats()
    print(f"\n3) 共抓到 {st['sentences']} 句")
    todos, decs = rec.todos(), rec.decisions()
    print(f"   待办 {len(todos)} 条：")
    for d in todos:
        print(f"     - {d['text']}" + (f"  [截止 {d['due']}]" if d["due"] else ""))
    print(f"   决议 {len(decs)} 条：")
    for d in decs:
        print(f"     - {d['text']}")

    md, hp = save(rec, tag=f"e2e-{time.strftime('%H%M%S')}", title="会议纪要（端到端实测）")
    print(f"\n4) 产物：\n   {md}\n   {hp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
