"""livecaption-lab 统一入口 —— 一个命令启动任意一个项目。

    python start.py              列出所有项目，输入编号启动
    python start.py dashboard    直接启动（不用看菜单）
    python start.py remote --mic 后面的参数原样转给目标脚本

已收录的项目
------------
  1 probe       环境体检：系统版本、语音包、字幕窗口能不能找到
  2 selftest    TTS 闭环自测：念中文 → 系统转写 → 抓回 → 比对
  3 remote      语音遥控器：说触发词执行动作（记笔记/截图/开程序…）
  4 overlay     双语字幕浮层：屏幕底部半透明字幕条，可开翻译
  5 dashboard   实时看板：词云 + 语速曲线 + 热词 + 字幕流（浏览器）
  6 notes       会议纪要：待办/决议/关键词/时间轴，导出 md + html
  7 daily       声音日报：一天的字幕统计与高频词
  8 record      纯字幕记录：开字幕即录，关窗口即停，落盘 E:/Program/Zimu
  9 test        跑一遍全部自检脚本
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECTS: list[tuple[str, str, str, list[str]]] = [
    # (编号别名, 脚本, 一句话说明, 默认参数)
    ("probe",     "probe.py",           "环境体检：能不能抓到字幕窗口",              []),
    ("selftest",  "selftest_tts.py",    "闭环自测：念中文→转写→抓回→比对",           []),
    ("remote",    "voice_remote.py",    "语音遥控器：说触发词执行动作",              []),
    ("overlay",   "overlay.py",         "双语字幕浮层：底部半透明字幕条",            []),
    ("dashboard", "live_dashboard.py",  "实时看板：词云/语速/热词（浏览器打开）",     ["--port", "8756"]),
    ("notes",     "meeting_notes.py",   "会议纪要：待办/决议/关键词/时间轴",         ["--live"]),
    ("daily",     "demo_daily.py",      "声音日报：一天的字幕统计",                  []),
    ("record",    "record.py",          "纯字幕记录：开字幕即录，关窗口即停，落盘 E:\\Program\\Zimu", []),
]

TESTS = [
    "test_dashboard.py",
    "test_meeting.py",
    "test_remote.py",
]


def run(script: str, args: list[str]) -> int:
    path = HERE / script
    if not path.exists():
        print(f"找不到 {script}")
        return 1
    cmd = [PY, str(path)] + args
    print(f"$ {script} {' '.join(args)}".strip(), flush=True)
    return subprocess.run(cmd, cwd=str(HERE)).returncode


def ask(prompt: str) -> str:
    """读一行输入。管道/重定向下没有 stdin，不能让它把菜单崩掉。"""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def menu() -> int:
    print("=" * 62)
    print("livecaption-lab · 实时字幕实验室")
    print("=" * 62)
    for i, (key, script, desc, _) in enumerate(PROJECTS, 1):
        print(f"  {i}. {key:<10} {desc}")
    print(f"  {len(PROJECTS) + 1}. test       跑一遍全部自检")
    print(f"  0. 退出")
    print("-" * 62)
    choice = ask("输入编号或别名：")
    if choice in ("0", "", "q", "quit"):
        return 0
    if choice == str(len(PROJECTS) + 1) or choice == "test":
        bad = 0
        for t in TESTS:
            print(f"\n### {t}", flush=True)
            if run(t, []) != 0:
                bad += 1
        return 1 if bad else 0
    for i, (key, script, _, default) in enumerate(PROJECTS, 1):
        if choice == str(i) or choice == key:
            extra = ask("附加参数（直接回车用默认）：")
            args = default if not extra else extra.split()
            return run(script, args)
    print(f"没这个选项：{choice}")
    return 1


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        return menu()
    key = argv[0]
    for k, script, _, default in PROJECTS:
        if key == k:
            return run(script, argv[1:] or default)
    if key == "test":
        bad = 0
        for t in TESTS:
            if run(t, []) != 0:
                bad += 1
        return 1 if bad else 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
