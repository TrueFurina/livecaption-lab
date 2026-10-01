"""
test_remote.py — 语音遥控器自检

启动 voice_remote.py，用 TTS 念一句触发语，检查它有没有真的执行动作。

不用开麦克风也能测：TTS 走系统音频输出，实时字幕照样会转写。

    python test_remote.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).parent
PROBE = "记一下 明天要交实验报告"


def main() -> None:
    notes_file = BASE / "notes" / f"voice-notes-{time.strftime('%Y-%m-%d')}.md"
    before = notes_file.read_text(encoding="utf-8") if notes_file.exists() else ""

    print("启动语音遥控器…", flush=True)
    remote = subprocess.Popen(
        [sys.executable, str(BASE / "voice_remote.py")],
        cwd=str(BASE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    time.sleep(6)        # 等字幕接上 + 跨过 prime_window
    print(f"念触发语：{PROBE}", flush=True)
    subprocess.run(
        [sys.executable, str(BASE / "_speak.py"), PROBE],
        cwd=str(BASE),
        timeout=60,
    )
    time.sleep(6)        # 等 ASR 定稿 + 规则触发

    remote.terminate()
    try:
        out, _ = remote.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        remote.kill()
        out, _ = remote.communicate()

    print("\n----- 遥控器输出 -----")
    print(out or "<无输出>")

    after = notes_file.read_text(encoding="utf-8") if notes_file.exists() else ""
    added = after[len(before):].strip()

    print("\n----- 判定 -----")
    if added:
        print(f"触发成功，笔记新增内容：\n    {added}")
    else:
        print("未触发")
        print("排查：字幕窗口是否开着 / 系统音量是否静音 / 规则关键词是否匹配")


if __name__ == "__main__":
    main()
