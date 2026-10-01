"""
demo_daily.py — 把抓下来的字幕日志渲染成一份"声音日报"网页

跑法：
    python demo_daily.py                 # 用今天的日志
    python demo_daily.py logs/captions-2026-10-01.jsonl

产出：
    reports/daily-<日期>.html   （单文件，双击即开，无外部依赖）

日报内容：总句数 / 总时长 / 逐小时活跃柱状图 / 高频词 / 完整流水时间轴。
这是"听了些什么"的自量化视角 —— 一天下来，你的耳朵都接收了什么。
"""

from __future__ import annotations

import html
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from caption_source import load_jsonl  # noqa: E402

BASE = Path(__file__).parent
REPORTS = BASE / "reports"

STOPWORDS = set(
    "的 了 是 我 你 他 她 它 我们 你们 他们 这 那 有 在 就 不 也 都 和 与 对 吧 啊 呢 吗 "
    "很 太 好 要 会 说 着 过 还 被 把 让 给 到 上 下 里 中 个 们 这 那 什么 怎么 可以 因为 "
    "所以 但是 然后 就是 不是 没有 一个 一下 现在 这个 那个 如果 已经 还是 这样 那样 一些 "
    "而且 并且 或者 于 以 之 其 此 该 等 做 去 来 看 想 知道 觉得 应该 可能 真的 非常 比较".split()
)

CJK = r"[\u4e00-\u9fff]"


def tokenize(texts: list[str]) -> Counter:
    """无依赖中文词频：2/3-gram 合并 + 停用词过滤，外加英文单词。"""
    bi, tri = Counter(), Counter()
    en = Counter()
    for t in texts:
        for seg in re.findall(rf"{CJK}+", t):
            for i in range(len(seg) - 1):
                bi[seg[i:i + 2]] += 1
            for i in range(len(seg) - 2):
                tri[seg[i:i + 3]] += 1
        for w in re.findall(r"[A-Za-z]{3,}", t):
            en[w.lower()] += 1

    # 若 3-gram 出现次数接近其组成的 2-gram，判定它更像是完整词，保留并抑制 2-gram
    words: Counter = Counter()
    for g3, c3 in tri.items():
        if c3 < 2:
            continue
        sub = min(bi.get(g3[:2], 0), bi.get(g3[1:], 0))
        if sub and c3 >= 0.75 * sub:
            words[g3] = c3
    for g2, c2 in bi.items():
        if c2 < 2:
            continue
        if g2 in STOPWORDS:
            continue
        words[g2] = max(words.get(g2, 0), c2)
    for w, c in en.items():
        if c >= 2:
            words[w] = c
    return words


def build_report(rows: list[dict], src: Path) -> Path:
    REPORTS.mkdir(exist_ok=True)
    rows = sorted(rows, key=lambda r: r.get("ts", 0))
    texts = [r["text"] for r in rows if r.get("text")]
    if not rows:
        raise SystemExit("日志为空，先跑 demo_console.py 抓一会儿")

    t0, t1 = rows[0]["ts"], rows[-1]["ts"]
    span_min = max((t1 - t0) / 60.0, 0.1)
    total_chars = sum(len(t) for t in texts)
    day = datetime.fromtimestamp(t0).strftime("%Y-%m-%d")

    hours = Counter()
    for r in rows:
        hours[datetime.fromtimestamp(r["ts"]).hour] += 1
    max_h = max(hours.values()) if hours else 1

    words = tokenize(texts)
    top_words = [w for w in words.most_common(60) if w[0] not in STOPWORDS][:24]
    max_w = top_words[0][1] if top_words else 1

    by_minute: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        key = datetime.fromtimestamp(r["ts"]).strftime("%H:%M")
        by_minute[key].append(r["text"])

    # ---- HTML ----
    hour_bars = "\n".join(
        f'<div class="bar-row"><span class="bar-lab">{h:02d}</span>'
        f'<span class="bar" style="width:{hours.get(h,0)/max_h*100:.1f}%"></span>'
        f'<span class="bar-num">{hours.get(h,0)}</span></div>'
        for h in range(24)
    )
    word_chips = "\n".join(
        f'<span class="chip" style="--w:{c/max_w:.3f}">{html.escape(w)}'
        f'<i>{c}</i></span>'
        for w, c in top_words
    ) or '<span class="muted">暂无足够数据</span>'

    timeline = "\n".join(
        f'<div class="slot"><div class="slot-t">{html.escape(k)}</div>'
        f'<div class="slot-lines">'
        + "".join(f'<p>{html.escape(t)}</p>' for t in v)
        + "</div></div>"
        for k, v in sorted(by_minute.items())
    )

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>声音日报 {day}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:32px;background:#FAFAF8;color:#2C2C2A;
font:14px/1.7 "Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:22px;font-weight:500;margin:0 0 4px}}
h2{{font-size:15px;font-weight:500;margin:36px 0 12px;
padding-bottom:6px;border-bottom:1px solid #E5E3DC}}
.sub{{color:#888780;font-size:13px;margin-bottom:24px}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{flex:1;min-width:150px;background:#fff;border:1px solid #E5E3DC;
border-radius:12px;padding:16px 18px}}
.card b{{display:block;font-size:24px;font-weight:500;line-height:1.2}}
.card span{{color:#888780;font-size:12px}}
.bar-row{{display:flex;align-items:center;gap:8px;height:20px}}
.bar-lab{{width:26px;color:#888780;font-size:12px}}
.bar{{height:12px;background:#7F77DD;border-radius:3px;min-width:2px}}
.bar-num{{color:#888780;font-size:12px}}
.chip{{display:inline-block;margin:4px 6px 4px 0;padding:5px 10px;border-radius:999px;
background:rgba(127,119,221,{0.10});border:1px solid rgba(127,119,221,.35);font-size:13px}}
.chip i{{font-style:normal;color:#888780;font-size:11px;margin-left:6px}}
.slot{{display:flex;gap:16px;padding:10px 0;border-bottom:1px dashed #EDEBE4}}
.slot-t{{width:48px;color:#888780;font-size:13px;flex:none}}
.slot-lines p{{margin:0 0 3px}}
.muted{{color:#888780}}
footer{{margin:40px 0 0;color:#B4B2A9;font-size:12px}}
</style></head><body><div class="wrap">
<h1>声音日报 · {day}</h1>
<div class="sub">数据来源：Windows 11 实时字幕（本地转写，不出本机） · {html.escape(src.name)}</div>

<div class="cards">
<div class="card"><b>{len(texts)}</b><span>字幕句数</span></div>
<div class="card"><b>{total_chars}</b><span>总字数</span></div>
<div class="card"><b>{span_min:.0f} 分钟</b><span>覆盖时长</span></div>
<div class="card"><b>{(len(texts)/span_min if span_min else 0):.1f}</b><span>句 / 分钟</span></div>
</div>

<h2>逐小时活跃度</h2>
{hour_bars}

<h2>高频词</h2>
<div>{word_chips}</div>

<h2>完整流水</h2>
{timeline}

<footer>由 livecaption-lab 生成 · {datetime.now():%Y-%m-%d %H:%M}</footer>
</div></body></html>"""

    out = REPORTS / f"daily-{day}.html"
    out.write_text(doc, encoding="utf-8")
    return out


def main() -> None:
    if len(sys.argv) > 1:
        src = Path(sys.argv[1])
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        src = BASE / "logs" / f"captions-{today}.jsonl"
    if not src.exists():
        raise SystemExit(f"找不到日志文件：{src}\n先跑 demo_console.py 抓一段字幕。")

    rows = list(load_jsonl(src))
    out = build_report(rows, src)
    print(f"已生成日报：{out}  （{len(rows)} 条记录）")


if __name__ == "__main__":
    main()
