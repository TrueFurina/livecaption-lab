"""
test_dedup_sentence.py — 验证"整段重复" bug 已根治。

复现用户 2026-10-01 存档里"发芽了，出来从未买过盲盒…"被整段吐了 5 遍的现象：
  字幕窗口最后一行是一句正在逐字增长、且句首被 ASR 反复修订的长句；
  旧逻辑误判每次修订为"新句子"，把上一句整段重新 commit。

新逻辑：当前句增长时不提交，只有它下面出现新行（定稿）或行数变化才提交一次。
本测试直接驱动 _feed_sentence，断言该长句只落盘一次且为最终版本、且全表无近似重复。
"""

from caption_source import CaptionTap, is_placeholder  # noqa


def _make_tap():
    tap = CaptionTap(md_dir=None)          # 不落盘真实目录
    tap._primed = True                    # 跳过 prime 时间窗
    tap._start_ts = 0.0
    return tap


def _feed(tap, *lines):
    """模拟窗口当前可见文本：每行一条，最后一行是 live tail。"""
    raw = "\n".join(lines)
    tap._feed_sentence(raw)


def test_no_whole_paragraph_duplication():
    tap = _make_tap()

    # 早期已说完的两句（稳定历史行，会被 lines[:-1] 提交一次）
    H1 = "记住谁都快，"
    H2 = "粉转粉了，记住了呀，谁说没记住的，那我肯定记住成年我记得学度快，谢谢哥六"

    # 正在增长 + 句首被 ASR 修订的长句（用户存档 33-37 行那段）
    s1 = ("发芽了，出来从未买过盲盒，这一点大家必须要替我证实，还有我从来没有接过广告，"
          "我没有接过奇怪的广告，只接过供销，没有接过奇怪的广告，这一点必须要给我证实一下米娜如果有着问")
    s2 = ("发芽了，出来从未买过盲盒，这一点大家必须要替我证实，还有我从来没有接过广告，"
          "我没有接过奇怪的广告，只接过供销，没有接过奇怪的广告，这一点必须要给我证实一下米娜，"
          "如果有人问起来就这么说，见月神2号也随见着礼物等消息是不是单独发动态，"
          "我请问了我提督队联合来给你发邮件不叫看没看到被赖了发了呀")
    s3 = s2[:-4] + "发了呀"                       # 句尾微调
    s4 = s2[:-4] + "都发了"                        # 继续增长
    s5 = ("发芽了，出来从未买过盲盒，这一点大家必须要替我证实，还有我从来没有接过广告，"
          "我没有接过奇怪的广告，只接过供销，没有接过奇怪的广告，这一点必须要给我证实一下米娜，"
          "如果有人问起来就这么说，见月神2号也随见着礼物等消息是不是单独发动态，"
          "我请问了我提督队联合来给你发邮件不叫看没看到都发了见着那个提督的那个对联礼物"
          "已经都发完了，已经都发完了。")            # 最终版本

    # 1) 历史行 + 增长中的长句（每次只是最后一行变长 / 句首修订）
    _feed(tap, H1, H2, s1)
    _feed(tap, H1, H2, s2)
    _feed(tap, H1, H2, s3)
    _feed(tap, H1, H2, s4)
    _feed(tap, H1, H2, s5)
    # 2) 新的一句出现在长句下方 -> 长句定稿
    F = "你们到底在笑什么"
    _feed(tap, H1, H2, s5, F)

    texts = [ln.text for ln in tap.lines]
    fa_lines = [t for t in texts if "发芽了" in t]

    print("落盘句子数:", len(texts))
    print("包含'发芽了'的行数:", len(fa_lines))
    for t in fa_lines:
        print("  -", t[:40], "...")

    # 断言 1：该长句只落盘一次（根治整段重复）
    assert len(fa_lines) == 1, f"整段重复未根治：'发芽了'出现 {len(fa_lines)} 次"
    # 断言 2：落盘的是最终版本（含'都发完了'），不是第一版残句
    assert "都发完了" in fa_lines[0], "落盘的不是最终版本"
    # 断言 3：全表无近似重复（任意两行最长公共子串占比 < 0.8）
    import difflib
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
            if m.size / max(len(a), 1) >= 0.8:
                raise AssertionError(f"仍存在近似重复行：\n  A={a}\n  B={b}")
    print("PASS: 整段重复已根治，长句仅落盘一次且为最终版本，全表无近似重复。")


def test_idle_flush_final_utterance_once():
    """最后一句没有后续新句时，靠空闲超时定稿，也只落盘一次（取最终版）。"""
    tap = _make_tap()
    growing = ["他唱得很好听", "他唱得很好听，真的", "他唱得很好听，真的很动人"]
    for g in growing:
        _feed(tap, g)
    import time as _t
    tap._pending_ts = _t.time() - 10   # 让 _check_idle 认为已静默 10s
    tap.flush_idle = 5.0
    tap._check_idle()
    texts = [ln.text for ln in tap.lines]
    hits = [t for t in texts if "他唱得很好听" in t]
    assert len(hits) == 1, f"空闲定稿重复：{len(hits)} 次"
    assert hits[0] == "他唱得很好听，真的很动人", "空闲定稿未取最终版"
    print("PASS: 空闲定稿也只落盘一次且为最终版本。")


if __name__ == "__main__":
    test_no_whole_paragraph_duplication()
    test_idle_flush_final_utterance_once()
    print("\nALL TESTS PASSED")
