"""
test_meeting.py — 纪要抽取的正确性验证（含反例）

不靠肉眼：造一段"模拟会议"，里面故意塞进
  * 应被识别的待办（带时间词的）
  * 应被识别的决议
  * **不该被识别的疑问句**（"这个要不要改？"）—— 被误判就说明规则有洞
跑一遍看抽取结果是否符合预期，再生成 HTML 供肉眼确认。

    python test_meeting.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from meeting_notes import MeetingRecorder, save

BASE = time.time() - 1800

SCRIPT = [
    # (句子, 期望类别: todo / decision / none, 距上句秒数)
    ("今天我们讨论一下多智能体强化学习的收敛性问题。", "none", 0),
    ("实验结果显示比基线好了百分之五，但方差还是偏大。", "none", 12),
    ("奖励函数的设计可能有点问题，需要再检查一下。", "todo", 15),
    ("这个要不要改？", "none", 9),                       # 疑问句：不该算待办
    ("结论是采用 VDN 加 CARS 的组合方案。", "decision", 20),
    ("需要尽快补充对比实验，明天之前把结果发出来。", "todo", 14),
    ("记得把超参数整理成表格，下周一同步给大家。", "todo", 11),
    ("我们是不是应该先复现一下基线？", "none", 10),        # 疑问句：不该算待办
    ("那就这么定下来，下一阶段按这个方案推进。", "decision", 18),
    ("区块链共识机制的效率还有优化空间。", "none", 13),
    ("还需要调研一下最新的论文，月底交一份汇总。", "todo", 16),
    # 反例：ASR 常把几句串成一条超长字幕，它不该再单独列一遍（会被短句覆盖）
    ("结论是采用 VDN 加 CARS 的组合方案，需要尽快补充对比实验，"
     "明天之前把结果发出来，记得把超参数整理成表格，下周一同步给大家。", "none", 20),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    rec = MeetingRecorder()
    t = BASE
    expect_todo, expect_dec = [], []
    for text, kind, gap in SCRIPT:
        t += gap
        rec.add(text, ts=t)
        if kind == "todo":
            expect_todo.append(text)
        elif kind == "decision":
            expect_dec.append(text)

    got_todo = {d["text"] for d in rec.todos()}
    got_dec = {d["text"] for d in rec.decisions()}

    hit = lambda exp, got: [s for s in exp if any(
        s[:12] in g for g in got)]
    false_pos = lambda exp, got: [g for g in got if not any(
        any(s[:12] in g for s in exp) for _ in [0])]

    print("=" * 66)
    print(f"投入 {len(SCRIPT)} 句：待办 {len(expect_todo)} / 决议 {len(expect_dec)} / 干扰 {len(SCRIPT) - len(expect_todo) - len(expect_dec)}")
    print("-" * 66)
    t_ok = len(hit(expect_todo, got_todo))
    d_ok = len(hit(expect_dec, got_dec))
    print(f"待办召回 {t_ok}/{len(expect_todo)}   决议召回 {d_ok}/{len(expect_dec)}")

    print("\n抽出的待办：")
    for d in rec.todos():
        print(f"  [{'截止:' + d['due'] if d['due'] else '无时限'}] {d['text']}")
    print("抽出的决议：")
    for d in rec.decisions():
        print(f"  {d['text']}")

    # 反例检查：疑问句必须一个都不中
    questions = [s for s, k, _ in SCRIPT if k == "none" and ("？" in s or "?" in s)]
    leaked = [q for q in questions if q in got_todo or q in got_dec]
    print("-" * 66)
    if leaked:
        print(f"❌ 疑问句被误判为待办/决议：{leaked}")
    else:
        print(f"✅ {len(questions)} 条疑问句全部未被误判")

    ok = (t_ok == len(expect_todo) and d_ok == len(expect_dec) and not leaked)
    LONG = ("结论是采用 VDN 加 CARS 的组合方案，需要尽快补充对比实验，"
            "明天之前把结果发出来，记得把超参数整理成表格，下周一同步给大家。")
    if LONG in got_todo or LONG in got_dec:
        print(f"❌ 超长合并句被重复单列（应被短句覆盖）")
        ok = False
    else:
        print("✅ 超长合并句已被短句覆盖，未重复单列")

    print("-" * 66)
    print(f"分段：{len(rec.segments())} 段")
    for s in rec.segments():
        print(f"  {s['start']}–{s['end']}  {s['topic']}")
    print(f"关键词：{rec.keywords(8)}")
    st = rec.stats()
    print(f"统计：{st['sentences']} 句 / {st['chars']} 字 / {st['minutes']:.0f} 分钟 / {st['wpm']:.0f} 字每分钟")

    md, hp = save(rec, tag="test", title="模拟会议纪要（测试样本）")
    print(f"\n产物：{md.name} / {hp.name}")
    print("结果：" + ("通过" if ok else "未通过"))
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
