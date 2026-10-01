"""
selftest_tts.py — 端到端闭环自测

用 TTS 念几句中文（走系统音频输出），让实时字幕转写，
再用 CaptionTap 抓回来，最后和原文逐句比对，给出真实识别率。

这一步能一次性验证整条链：TTS 发声 → 系统音频 → LiveCaptions ASR
→ 字幕窗口 → UIA → 本项目的去重管线 → 文本。

跑法：
    python selftest_tts.py              # 默认念内置的 6 句
    python selftest_tts.py --voices     # 只列出可用的 TTS 语音

判读：
    相似度 ≥ 0.8 说明链路健康；普遍偏低通常是音量太小或 TTS 音色太机械。
"""

from __future__ import annotations

import difflib
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import pyttsx3  # noqa: E402

from caption_source import CaptionTap, restart_livecaptions  # noqa: E402

SENTENCES = [
    "今天我们讨论多智能体强化学习的收敛性问题",
    "这个实验结果比基线方法要好一些",
    "区块链共识机制的效率还有优化空间",
    "请大家看一下屏幕上的这张流程图",
    "下一阶段需要补充更多的对比实验",
    "训练数据在第九轮之后开始收敛",
]


def pick_voice(engine) -> str | None:
    """优先挑中文语音，找不到就返回 None（用系统默认）。"""
    preferred = None
    fallback = None
    for v in engine.getProperty("voices"):
        name = f"{v.name} {v.id}".lower()
        if any(k in name for k in ("chinese", "zh-cn", "huihui", "yaoyao", "kangkang", "xiaoxiao")):
            preferred = v.id
            break
        if fallback is None:
            fallback = v.id
    return preferred or fallback


def list_voices() -> None:
    engine = pyttsx3.init()
    for v in engine.getProperty("voices"):
        print(f"{v.id}\n    name={v.name}  lang={getattr(v, 'languages', '')}")
    engine.stop()


def similarity(a: str, b: str) -> float:
    """原文被识别结果覆盖的比例。

    不能拿整句去比相似度：实时字幕会把连续说的几句拼成一长串，
    整句比对会被长度差拖垮。这里取最长公共连续片段占原文的比例。
    """
    if not a or not b:
        return 0.0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
        0, len(a), 0, len(b)
    )
    return m.size / len(a)


def main() -> None:
    if "--voices" in sys.argv:
        list_voices()
        return

    # 字幕窗口是累积日志，不清的话上一轮跑的相同句子会被判成冗余而丢掉
    print("重启实时字幕以清空历史…")
    if not restart_livecaptions():
        print("重启失败，请确认实时字幕可用（Win + Ctrl + L）")
        return

    # 主进程不初始化 pyttsx3：SAPI 会占用 COM，和抓取侧的 UIA 冲突
    print("发声由子进程 _speak.py 负责（隔离 SAPI 与 UIA 的 COM 争用）")
    print("\n提示：实时字幕会转写系统输出音频，请确保系统音量不是静音\n")

    captured: list[str] = []
    tap = CaptionTap(on_line=lambda line: captured.append(line.text), poll=0.08, md_dir=None)
    if not tap.start(wait_window=10.0):
        print("未检测到实时字幕窗口，请先按 Win + Ctrl + L")
        return

    time.sleep(4.0)     # 等 ASR 预热 + 跨过 prime_window，否则第一句会被当成历史吞掉

    print("=" * 62)
    # TTS 放到独立子进程：pyttsx3 的 SAPI 与主线程的 COM 调用会互相干扰，
    # 实测同进程内念第二句起就抓不到字幕了，分离后恢复正常。
    speaker = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("_speak.py"))]
        + SENTENCES,
    )
    deadline = time.time() + 90
    while speaker.poll() is None and time.time() < deadline:
        time.sleep(0.5)
    if speaker.poll() is None:
        speaker.kill()
    time.sleep(3.5)     # 等 ASR 收尾，否则最后一句来不及定稿
    print("=" * 62)

    time.sleep(1.0)
    tap.stop()

    print("\n抓取到的字幕：")
    for c in captured:
        print(f"    - {c}")

    print("\n逐句比对（原文 vs 最相近的抓取结果）：")
    scores = []
    pool = captured or ["<空>"]
    for s in SENTENCES:
        best = max(pool, key=lambda c: similarity(s, c))
        sc = similarity(s, best)
        scores.append(sc)
        print(f"    {sc:.2f}  原文「{s}」")
        print(f"          抓到「{best}」")

    if scores:
        avg = sum(scores) / len(scores)
        print(f"\n平均相似度：{avg:.2f}   命中句数：{sum(1 for s in scores if s >= 0.6)}/{len(scores)}")
        print("判定：" + ("链路健康" if avg >= 0.8 else "可用但识别率偏低，检查音量/音色"))


if __name__ == "__main__":
    main()
