"""
_speak.py — selftest_tts.py 的发声子进程

独立进程念句子，避免 pyttsx3 的 SAPI 与抓取侧的 UIA COM 调用打架。

    python _speak.py "第一句" "第二句" ...
"""

from __future__ import annotations

import sys
import time

import pyttsx3


def speak_one(sentence: str) -> None:
    # 每句重新 init：pyttsx3 复用同一个 engine 调 runAndWait 时，
    # 第二句起经常不发声（已知 bug），重新初始化最稳。
    engine = pyttsx3.init()
    for v in engine.getProperty("voices"):
        name = f"{v.name} {v.id}".lower()
        if any(k in name for k in ("chinese", "zh-cn", "huihui", "yaoyao", "kangkang", "xiaoxiao")):
            engine.setProperty("voice", v.id)
            break
    engine.setProperty("rate", 150)
    engine.setProperty("volume", 1.0)
    engine.say(sentence)
    engine.runAndWait()
    engine.stop()


def main() -> None:
    sentences = sys.argv[1:]
    if not sentences:
        return

    for s in sentences:
        print(f"念：{s}", flush=True)
        speak_one(s)
        time.sleep(2.5)     # 每句之间留白，让 ASR 有机会断句
    print("念完", flush=True)


if __name__ == "__main__":
    main()
