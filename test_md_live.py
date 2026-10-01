"""test_md_live.py — 真实链路验证：TTS 念中文 -> 系统转写 -> 抓取 -> 自动归档。

为了不污染用户真实归档目录，这里把 md_dir 指向一个临时目录，
验证"真实 ASR 输出也确实会走落盘路径"。默认行为（真实使用时）会写到
E:/Program/Zimu，逻辑已由 test_md_sink.py 单独证明。

运行：
    python test_md_live.py
"""
import os
import sys
import time
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from caption_source import CaptionTap, find_caption_window, restart_livecaptions  # noqa: E402
from datetime import datetime  # noqa: E402

SENTENCES = [
    "这是我们测试字幕自动保存功能的一句话。",
    "每一条字幕都会写进本地的 markdown 文件。",
    "记录结束之后可以随时回看今天的全部内容。",
]


def main() -> int:
    if find_caption_window(timeout=8) is None:
        print("未检测到字幕窗口，尝试自动打开（Win+Ctrl+L）…")
        if not restart_livecaptions(wait=10):
            print("自动打开失败，请手动按 Win+Ctrl+L 后重跑")
            return 2

    tmp = tempfile.mkdtemp(prefix="zimu_live_")
    captured: list[str] = []
    tap = CaptionTap(on_line=lambda line: captured.append(line.text),
                     poll=0.08, md_dir=tmp)  # 临时目录，不污染真实归档
    if not tap.start(wait_window=10.0):
        print("抓取启动失败")
        return 2

    sp = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("_speak.py"))] + SENTENCES,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    print("已发声，抓取中 …")
    time.sleep(18)
    tap.stop()
    try:
        sp.wait(timeout=5)
    except Exception:
        sp.kill()

    today = datetime.now().strftime("%Y-%m-%d")
    p = os.path.join(tmp, f"字幕归档-{today}.md")
    print(f"抓取句数：{len(captured)}")
    print(f"归档文件存在：{os.path.exists(p)}")
    ok = os.path.exists(p)
    if ok:
        content = open(p, encoding="utf-8").read()
        hits = sum(1 for s in SENTENCES if any(s[:6] in line for line in content.splitlines()))
        print(f"原文句子命中（前缀匹配）：{hits}/{len(SENTENCES)}")
        ok = hits >= 1
        print("--- 归档内容 ---")
        print(content)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
