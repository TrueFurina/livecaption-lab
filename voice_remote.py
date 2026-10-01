"""
voice_remote.py — 语音遥控器：说一句话，电脑就干一件事

字幕流 → 关键词匹配 → 执行动作。规则写在 rules.json 里，改规则不用改代码。

前置条件（很重要）
------------------
默认实时字幕只转写"系统播放的声音"，听不到你说话。
想用嘴控制电脑，先在字幕窗口里打开：
    字幕窗口 → 设置(齿轮) → 偏好设置 → 打开「包括麦克风音频」

跑法
----
    python voice_remote.py              # 开始监听
    python voice_remote.py --test       # 自检：念一句触发语，看有没有执行

内置动作
--------
    note        把这句追加到 notes/voice-notes.md
    screenshot  截一张图存到 shots/
    open        打开程序或网址（target 字段）
    hotkey      发一组按键（keys 字段，如 ["ctrl","shift","s"]）
    notify      用语音回你一句话（say 字段）
    log         只打印，不做事（调试用）
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import pyautogui  # noqa: E402

from caption_source import CaptionTap  # noqa: E402

BASE = Path(__file__).parent
RULES_FILE = BASE / "rules.json"
NOTES_DIR = BASE / "notes"
SHOTS_DIR = BASE / "shots"

DEFAULT_RULES = {
    "cooldown": 4,          # 同一条规则的冷却秒数，防止一句话反复触发
    "rules": [
        {
            "name": "记一下",
            "keywords": ["记一下", "记下来", "备注一下"],
            "action": "note",
        },
        {
            "name": "截图",
            "keywords": ["截图", "截个图", "截屏"],
            "action": "screenshot",
        },
        {
            "name": "打开记事本",
            "keywords": ["打开记事本"],
            "action": "open",
            "target": "notepad.exe",
        },
        {
            "name": "打开浏览器",
            "keywords": ["打开浏览器", "上网"],
            "action": "open",
            "target": "https://www.bing.com",
        },
        {
            "name": "播放暂停",
            "keywords": ["暂停播放", "继续播放"],
            "action": "hotkey",
            "keys": ["space"],
        },
        {
            "name": "下班打卡",
            "keywords": ["下班了", "收工"],
            "action": "notify",
            "say": "辛苦了，记得先保存文件",
        },
    ],
}


def load_rules() -> dict:
    if not RULES_FILE.exists():
        RULES_FILE.write_text(
            json.dumps(DEFAULT_RULES, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已生成默认规则文件：{RULES_FILE}")
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def clean(text: str) -> str:
    """去掉标点，方便关键词匹配。"""
    return text.strip(" 。！？，、；:!?.,;:")


def say(text: str) -> None:
    """语音回应走独立子进程，避免 SAPI 和抓取侧的 UIA 抢 COM。"""
    subprocess.Popen(
        [sys.executable, str(BASE / "_speak.py"), text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def do_action(rule: dict, text: str) -> str:
    """执行一条规则，返回人话描述。"""
    act = rule.get("action", "log")
    now = datetime.now()

    if act == "note":
        NOTES_DIR.mkdir(exist_ok=True)
        f = NOTES_DIR / f"voice-notes-{now:%Y-%m-%d}.md"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"- {now:%H:%M:%S} {clean(text)}\n")
        return f"已记到 {f.name}"

    if act == "screenshot":
        SHOTS_DIR.mkdir(exist_ok=True)
        p = SHOTS_DIR / f"shot-{now:%Y%m%d-%H%M%S}.png"
        pyautogui.screenshot(str(p))
        return f"已截图 {p.name}"

    if act == "open":
        import os
        target = rule.get("target", "")
        if not target:
            return "规则缺少 target"
        os.startfile(target)
        return f"已打开 {target}"

    if act == "hotkey":
        keys = rule.get("keys", [])
        if not keys:
            return "规则缺少 keys"
        pyautogui.hotkey(*keys)
        return f"已按键 {'+'.join(keys)}"

    if act == "notify":
        msg = rule.get("say", "好的")
        say(msg)
        return f"回应：{msg}"

    return f"（仅记录）{clean(text)}"


def main() -> None:
    cfg = load_rules()
    rules = cfg.get("rules", [])
    default_cd = float(cfg.get("cooldown", 4))
    last_fired: dict[str, float] = {}

    print("=" * 62)
    print("语音遥控器")
    print("=" * 62)
    print(f"已加载 {len(rules)} 条规则：")
    for r in rules:
        print(f"    {r['name']:<10} {r['action']:<11} 触发词：{'/'.join(r['keywords'])}")
    print("\n提示：必须在字幕设置里打开「包括麦克风音频」，否则听不见你说话")
    print("Ctrl+C 结束\n")

    def handle(line) -> None:
        text = clean(line.text)
        if not text:
            return
        print(f"[{line.iso}] 听到：{text}", flush=True)
        for r in rules:
            if any(k in text for k in r.get("keywords", [])):
                cd = float(r.get("cooldown", default_cd))
                if time.time() - last_fired.get(r["name"], 0) < cd:
                    print(f"    冷却中，跳过：{r['name']}")
                    return
                last_fired[r["name"]] = time.time()
                print(f"    >>> 触发「{r['name']}」：{do_action(r, text)}", flush=True)
                return

    tap = CaptionTap(on_line=handle, poll=0.1)
    if not tap.start(wait_window=10.0):
        print("未检测到实时字幕窗口，请先按 Win + Ctrl + L")
        return

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n停止")
        tap.stop()


if __name__ == "__main__":
    main()
