"""
meeting_notes.py — 会议 / 课堂实时纪要

把字幕流变成一份能直接发出去的纪要：待办清单、决议、议题分段、关键词、
带时间戳的完整流水。

为什么默认不接大模型
--------------------
接 LLM 摘要质量更好，但要花钱、要联网、每次调用都要把会议内容发出去。
所以主力是一套零成本规则引擎（离线可跑、不出网、不花钱）：
    - 待办：靠"需要/记得/尽快 + 时间词"这类中文会议口头禅识别
    - 决议：靠"决定/确定/结论/拍板/就这样"识别
    - 议题分段：靠静默间隔 + 句数
想上 LLM，加 --llm deepseek（见 summarizer.py，会打印每次调用的 token 用量）。

用法
----
    python meeting_notes.py --live                  # 实时听，边听边刷新纪要
    python meeting_notes.py --from logs/xxx.jsonl   # 事后从日志生成
产物：reports/meeting-<时间戳>.md 与 .html
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / "reports"

# ---- 规则库（中文会议口头禅，实测调出来的） --------------------------------
TODO_TRIGGERS = (
    "需要", "要记得", "记得", "别忘", "务必", "尽快", "抓紧", "安排", "跟进",
    "确认一下", "补充", "整理一下", "整理", "提交", "准备一下", "准备",
    "拉一下", "发一下", "改一下", "加一下", "做一下", "看一下", "处理一下",
    "调研", "修复", "优化一下", "完成", "补齐", "交一下", "约一下", "同步一下",
)
DECISION_TRIGGERS = (
    "决定", "确定", "结论", "就这样", "定下来", "拍板", "采用", "通过",
    "同意", "方案是", "最终", "我们就", "那就", "一致认为", "不再",
)
# 疑问句不算待办（"这个要不要改？"是提问，不是指派）
QUESTION_HINTS = ("吗？", "吗?", "呢？", "呢?", "要不要", "是不是", "能不能", "可不可以")
DEADLINE_PAT = (
    r"(今天|明天|后天|大后天|下周[一二三四五六日天]?|周[一二三四五六日]|本周|月底|"
    r"周内|[0-9]+月[0-9]+[日号]|[0-9]+号|截止[^，。]{0,8}|deadline[^，。]{0,8})"
)
STOPWORDS = {
    "我们", "你们", "他们", "这个", "那个", "这样", "那样", "什么", "怎么",
    "可以", "就是", "然后", "所以", "因为", "但是", "如果", "还是", "不过",
    "一个", "一下", "现在", "时候", "有点", "非常", "比较", "其实", "而且",
    "大家", "自己", "这里", "那里", "没有", "不是", "已经", "还有", "这些",
    "那些", "之后", "之前", "目前", "大概", "可能", "应该", "需要", "问题",
    "觉得", "看看", "来说", "的话", "这样子", "嗯", "啊", "哦", "对对",
}


@dataclass
class Utterance:
    text: str
    ts: float

    @property
    def clock(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M:%S")


class MeetingRecorder:
    """累积字幕，随时产出纪要。"""

    def __init__(self, gap: float = 45.0, seg_max: int = 15) -> None:
        self.gap = gap
        self.seg_max = seg_max
        self.items: list[Utterance] = []

    def add(self, text: str, ts: float | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.items.append(Utterance(text, ts if ts is not None else time.time()))

    # ---- 统计 ----------------------------------------------------------
    def stats(self) -> dict:
        if not self.items:
            return {"sentences": 0, "chars": 0, "minutes": 0.0, "wpm": 0}
        t0, t1 = self.items[0].ts, self.items[-1].ts
        minutes = max((t1 - t0) / 60.0, 0.1)
        chars = sum(len(u.text) for u in self.items)
        return {
            "sentences": len(self.items),
            "chars": chars,
            "minutes": minutes,
            "wpm": chars / minutes,
            "start": datetime.fromtimestamp(t0).strftime("%H:%M:%S"),
            "end": datetime.fromtimestamp(t1).strftime("%H:%M:%S"),
        }

    # ---- 分段 ----------------------------------------------------------
    def segments(self) -> list[dict]:
        out: list[dict] = []
        cur: list[Utterance] = []
        for u in self.items:
            if cur and (u.ts - cur[-1].ts > self.gap or len(cur) >= self.seg_max):
                out.append(self._mk_seg(cur))
                cur = []
            cur.append(u)
        if cur:
            out.append(self._mk_seg(cur))
        return out

    def _mk_seg(self, items: list[Utterance]) -> dict:
        words = self._keywords([u.text for u in items], top=3)
        return {
            "start": items[0].clock,
            "end": items[-1].clock,
            "topic": " / ".join(w for w, _ in words) or "（无关键词）",
            "items": items,
        }

    # ---- 关键词 --------------------------------------------------------
    def _keywords(self, texts: list[str], top: int = 12) -> list[tuple[str, int]]:
        try:
            import jieba
            tokens = []
            for t in texts:
                for w in jieba.lcut(t):
                    w = w.strip()
                    if len(w) < 2 or w in STOPWORDS or w.isdigit():
                        continue
                    if all("\u4e00" <= c <= "\u9fff" or c.isalnum() for c in w):
                        tokens.append(w)
            counts = Counter(tokens)
        except ImportError:
            # 没装 jieba 就退化为 2-gram，质量差些但不至于崩
            counts = Counter()
            for t in texts:
                s = "".join(c for c in t if "\u4e00" <= c <= "\u9fff")
                counts.update(s[i:i + 2] for i in range(len(s) - 1))
        # 长词优先：把被包含且频次接近的短词压掉
        kept = [(w, c) for w, c in counts.most_common(80) if c >= 2]
        kept.sort(key=lambda x: (-x[1], -len(x[0])))
        return kept[:top]

    def keywords(self, top: int = 14) -> list[tuple[str, int]]:
        return self._keywords([u.text for u in self.items], top=top)

    # ---- 待办 / 决议 ----------------------------------------------------
    @staticmethod
    def _is_question(text: str) -> bool:
        return any(h in text for h in QUESTION_HINTS)

    @staticmethod
    def _split(text: str) -> list[str]:
        """按标点切子句（保留标点），子句里没有标点时返回整句。"""
        import re

        parts = re.split(r"(?<=[，,。；;！!？?])", text)
        return [p.strip() for p in parts if p.strip()] or [text]

    @staticmethod
    def _covered(text: str, other: str) -> bool:
        """text 的实质内容是不是已经被更短的 other 覆盖。

        ASR 经常把连着说的几句用逗号串成一条超长字幕，它和前面已经
        输出的几条短句内容重复。不处理就会在纪要里出现"同一件事列两遍"。
        """
        a, b = (text, other) if len(text) >= len(other) else (other, text)
        if not b:
            return False
        m = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match(
            0, len(a), 0, len(b))
        return m.size / len(b) >= 0.75

    @classmethod
    def _dedup(cls, items: list[dict]) -> list[dict]:
        """短句优先保留；被已保留条目覆盖的长合并句丢掉；最后按时间排回。"""
        kept: list[dict] = []
        for it in sorted(items, key=lambda d: len(d["text"])):
            if any(cls._covered(it["text"], k["text"]) for k in kept):
                continue
            kept.append(it)
        return sorted(kept, key=lambda d: d["ts"])

    def todos(self) -> list[dict]:
        """抽待办。

        语速快时 ASR 会把整段话用逗号连成一条超长字幕，整条列进纪要没
        法用。所以先按标点切成子句，逐个子句判断；碰到时间词子句就并入
        上一条待办当作截止时间。
        """
        import re

        out: list[dict] = []
        for u in self.items:
            if self._is_question(u.text):
                continue
            pending: dict | None = None
            prev: str | None = None
            for sub in self._split(u.text):
                m = re.search(DEADLINE_PAT, sub)
                has_todo = any(k in sub for k in TODO_TRIGGERS)
                is_dec = any(k in sub for k in DECISION_TRIGGERS)
                if has_todo:
                    # 带上紧邻的前半句做上下文（"奖励函数有问题，需要再检查一下"），
                    # 但不要把决议内容塞进待办里
                    ctx = ""
                    if (prev and len(prev) < 20
                            and not re.search(r"[。！？!?]$", prev)
                            and not any(k in prev for k in DECISION_TRIGGERS)):
                        ctx = prev
                    pending = {"text": ctx + sub, "time": u.clock,
                               "due": m.group(1) if m else "", "ts": u.ts, "joins": 0}
                    out.append(pending)
                elif (pending is not None and not is_dec and pending["joins"] < 2
                        and not re.search(r"[。！？!?]$", pending["text"])
                        and (m or len(sub) <= 14)):
                    # 把紧随其后的"截止时间 / 怎么做"补进来，但最多补两句，
                    # 否则 ASR 一逗到底时会把整段话全吞进一条待办
                    pending["text"] += sub
                    pending["joins"] += 1
                    if m and not pending["due"]:
                        pending["due"] = m.group(1)
                else:
                    pending = None
                prev = sub
        return self._dedup(out)

    def decisions(self) -> list[dict]:
        import re

        out: list[dict] = []
        for u in self.items:
            if self._is_question(u.text):
                continue
            subs = self._split(u.text)
            i = 0
            while i < len(subs):
                if any(k in subs[i] for k in DECISION_TRIGGERS):
                    # 决议常跨子句（"那就这么定下来，下一阶段按这个方案推进"），
                    # 从命中处一直接到句末，别只留半句
                    buf = subs[i]
                    j = i + 1
                    while j < len(subs) and not re.search(r"[。！？!?]$", buf):
                        buf += subs[j]
                        j += 1
                    out.append({"text": buf.strip(), "time": u.clock, "ts": u.ts})
                    i = j
                else:
                    i += 1
        return self._dedup(out)

    # ---- 输出 ------------------------------------------------------------
    def to_markdown(self, title: str = "会议纪要") -> str:
        st = self.stats()
        L: list[str] = [f"# {title}", ""]
        if st.get("sentences"):
            L.append(f"- 时段：{st['start']} – {st['end']}（{st['minutes']:.0f} 分钟）")
            L.append(f"- 发言：{st['sentences']} 句 / {st['chars']} 字，语速 {st['wpm']:.0f} 字/分钟")
        L.append("")

        todos, decs = self.todos(), self.decisions()
        if todos:
            L.append("## 待办")
            for i, d in enumerate(todos, 1):
                due = f"（{d['due']}）" if d["due"] else ""
                L.append(f"{i}. {d['text']}{due}  `{d['time']}`")
            L.append("")
        if decs:
            L.append("## 决议")
            for d in decs:
                L.append(f"- {d['text']}  `{d['time']}`")
            L.append("")

        kws = self.keywords()
        if kws:
            L.append("## 关键词")
            L.append("  ".join(f"{w}({c})" for w, c in kws))
            L.append("")

        L.append("## 议题分段")
        for seg in self.segments():
            L.append(f"### {seg['start']}–{seg['end']}  {seg['topic']}")
            for u in seg["items"]:
                L.append(f"- `{u.clock}` {u.text}")
            L.append("")
        return "\n".join(L)

    def to_html(self, title: str = "会议纪要") -> str:
        st = self.stats()
        todos, decs = self.todos(), self.decisions()
        kws = self.keywords()
        segs = self.segments()
        max_kw = kws[0][1] if kws else 1

        def e(s: str) -> str:
            return html.escape(str(s))

        todo_html = "".join(
            f'<li><label><input type="checkbox"> <span class="t">{e(d["text"])}</span>'
            + (f' <em class="due">{e(d["due"])}</em>' if d["due"] else "")
            + f' <span class="ts">{e(d["time"])}</span></label></li>'
            for d in todos
        ) or '<li class="muted">没识别到明确的待办</li>'

        dec_html = "".join(
            f'<li><span class="t">{e(d["text"])}</span>'
            f'<span class="ts">{e(d["time"])}</span></li>'
            for d in decs
        ) or '<li class="muted">没识别到明确的决议</li>'

        kw_html = "".join(
            f'<span class="chip" style="--w:{c / max_kw:.3f}">{e(w)}<i>{c}</i></span>'
            for w, c in kws
        ) or '<span class="muted">数据不足</span>'

        seg_html = "".join(
            f'<section class="seg"><header><span class="seg-t">{e(s["start"])}–{e(s["end"])}</span>'
            f'<span class="seg-topic">{e(s["topic"])}</span></header>'
            + "".join(f'<p><b>{e(u.clock)}</b> {e(u.text)}</p>' for u in s["items"])
            + "</section>"
            for s in segs
        ) or '<p class="muted">没有内容</p>'

        cards = ""
        if st.get("sentences"):
            cards = f"""
        <div class="cards">
          <div class="card"><span class="n">{st['minutes']:.0f}</span><span class="l">时长(分钟)</span></div>
          <div class="card"><span class="n">{st['sentences']}</span><span class="l">发言句数</span></div>
          <div class="card"><span class="n">{st['chars']}</span><span class="l">总字数</span></div>
          <div class="card"><span class="n">{st['wpm']:.0f}</span><span class="l">字/分钟</span></div>
          <div class="card"><span class="n">{len(todos)}</span><span class="l">待办</span></div>
          <div class="card"><span class="n">{len(decs)}</span><span class="l">决议</span></div>
        </div>"""

        return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{e(title)}</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--fg:#1d2129;--mut:#8a9099;--acc:#2b6cb0;--ok:#2f855a}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--fg);
font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei UI",sans-serif}}
.wrap{{max-width:940px;margin:0 auto}}
h1{{font-size:24px;margin:0 0 4px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:20px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;margin-bottom:24px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;text-align:center}}
.card .n{{display:block;font-size:26px;font-weight:700;color:var(--acc)}}
.card .l{{display:block;font-size:12px;color:var(--mut);margin-top:2px}}
h2{{font-size:17px;margin:26px 0 10px;padding-left:10px;border-left:4px solid var(--acc)}}
ul{{list-style:none;padding:0;margin:0;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 4px}}
li{{padding:9px 14px;border-bottom:1px solid #f0f2f4}}
li:last-child{{border-bottom:0}}
li .t{{font-weight:500}}
li .due{{color:#c05621;font-style:normal;background:#fff5eb;padding:1px 7px;border-radius:6px;font-size:12px}}
li .ts{{color:var(--mut);font-size:12px;margin-left:8px}}
.muted{{color:var(--mut);font-size:13px;padding:12px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px}}
.chip{{position:relative;display:inline-flex;align-items:center;gap:6px;background:var(--card);
border:1px solid var(--line);border-radius:999px;padding:5px 12px;font-size:13px;overflow:hidden}}
.chip::before{{content:"";position:absolute;left:0;top:0;bottom:0;width:calc(var(--w)*100%);
background:rgba(43,108,176,.13)}}
.chip i{{font-style:normal;color:var(--mut);font-size:11px;position:relative}}
.seg{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin-bottom:12px}}
.seg header{{display:flex;gap:12px;align-items:baseline;margin-bottom:8px}}
.seg-t{{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}}
.seg-topic{{font-weight:600;color:var(--ok)}}
.seg p{{margin:5px 0;font-size:14px}}
.seg p b{{color:var(--mut);font-weight:400;font-size:12px;margin-right:6px}}
</style></head><body><div class="wrap">
<h1>{e(title)}</h1>
<div class="sub">由 Windows 11 实时字幕自动生成 · {e(datetime.now().strftime('%Y-%m-%d %H:%M'))}</div>
{cards}
<h2>待办</h2><ul>{todo_html}</ul>
<h2>决议</h2><ul>{dec_html}</ul>
<h2>关键词</h2><div class="chips">{kw_html}</div>
<h2>议题分段</h2>{seg_html}
</div></body></html>"""


def from_jsonl(path: str | Path) -> MeetingRecorder:
    rec = MeetingRecorder()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec.add(d.get("text", ""), d.get("ts"))
    return rec


def save(rec: MeetingRecorder, tag: str = "", title: str = "会议纪要") -> tuple[Path, Path]:
    REPORTS.mkdir(exist_ok=True)
    stamp = tag or time.strftime("%Y%m%d-%H%M%S")
    md = REPORTS / f"meeting-{stamp}.md"
    hp = REPORTS / f"meeting-{stamp}.html"
    md.write_text(rec.to_markdown(title), encoding="utf-8")
    hp.write_text(rec.to_html(title), encoding="utf-8")
    return md, hp


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="字幕流 → 会议纪要")
    ap.add_argument("--live", action="store_true", help="实时监听字幕并生成纪要")
    ap.add_argument("--from", dest="src", help="从 JSONL 日志生成")
    ap.add_argument("--log", default="", help="--live 时同时落盘字幕 JSONL")
    ap.add_argument("--refresh", type=float, default=30.0, help="实时模式下刷新间隔（秒）")
    ap.add_argument("--minutes", type=float, default=0, help="实时模式运行时长（默认一直跑）")
    ap.add_argument("--title", default="会议纪要")
    ap.add_argument("--llm", default="", help="可选：deepseek/moonshot/zhipu/dashscope（会产生费用）")
    args = ap.parse_args()

    if args.src:
        rec = from_jsonl(args.src)
        st = rec.stats()
        print(f"读取 {st['sentences']} 句，跨度 {st['minutes']:.0f} 分钟")
    elif args.live:
        from caption_source import CaptionTap

        rec = MeetingRecorder()
        tap = CaptionTap(on_line=lambda line: rec.add(line.text, line.ts),
                         jsonl=args.log or None)
        if not tap.start(wait_window=5):
            print("没找到实时字幕窗口，请先按 Win + Ctrl + L")
            return 1
        print("实时纪要已开始，Ctrl+C 结束…", flush=True)
        t0 = time.time()
        last = 0.0
        try:
            while True:
                time.sleep(1.0)
                if args.minutes and time.time() - t0 > args.minutes * 60:
                    break
                if time.time() - last >= args.refresh:
                    last = time.time()
                    md, hp = save(rec, tag="live", title=args.title)
                    print(f"  已刷新：{md.name}（{rec.stats()['sentences']} 句）", flush=True)
        except KeyboardInterrupt:
            print("\n结束")
        finally:
            tap.stop()
    else:
        ap.print_help()
        return 1

    if args.llm:
        try:
            from summarizer import get_summarizer
            sm = get_summarizer(args.llm)
            print("\n--- LLM 摘要 ---")
            print(sm.summarize(rec) or "（调用失败，已降级为规则结果）")
        except Exception as e:
            print(f"LLM 摘要失败：{type(e).__name__}: {e}")

    md, hp = save(rec, title=args.title)
    print(f"\n纪要已生成：\n  {md}\n  {hp}")
    print(f"待办 {len(rec.todos())} 条 / 决议 {len(rec.decisions())} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
