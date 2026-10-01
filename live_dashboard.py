"""实时字幕看板 —— 把字幕流变成一块活的可视化面板。

启动后在浏览器打开 http://127.0.0.1:<port> 就能看到：
  · 实时字幕流（最新的在上，滚动更新）
  · 动态词云（词频越高字越大，新热词会冒上来）
  · 语速曲线（每分钟字数，反映这段时间是快讲还是慢讲）
  · 情绪倾向（基于内置小词典的粗略打分，不是真情感分析）
  · 热词排行 + 累计统计

三种数据源：
  --live          实时抓取 Win11 实时字幕（默认）
  --demo          造数据演示，不需要字幕窗口也能看效果
  --from file     从 JSONL 字幕日志回放

设计要点
--------
1. 统计放后端（jieba 分词），渲染放前端（Canvas），互不拖累。
2. 前端只做增量轮询：last_seq 没变就不重排词云，省 CPU。
3. 情绪打分用的是几十个词的粗糙词典，只当趋势看，别当结论。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# ---- 停用词：出现再多也不上词云 -------------------------------------------
STOPWORDS = set(
    """
    的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好 自己 这 那 我们 你们 他们
    什么 怎么 这个 那个 可以 因为 所以 但是 然后 如果 还是 这样 那样 一下 现在 已经 还是 嗯 啊 哦 吧 呢 吗 呀
    就是 可能 应该 需要 觉得 知道 一下 有点 一点 一些 东西 时候 问题 地方 方式 情况 一个 两个 吧 嗯 那个 这个
    """.split()
)

# ---- 极简情绪词典（粗估趋势用，不是情感分析模型）--------------------------
POS_WORDS = set(
    "好 不错 优秀 成功 提升 改善 有效 稳定 通过 达成 满意 简单 清晰 方便 快速 顺利 突破 进展"
    " 同意 可以 没问题 厉害 完美 有意思 有趣 棒 强 赢 搞定 完成 收敛 增长 优化 省 值 喜欢 支持 明显 轻松".split()
)
NEG_WORDS = set(
    "问题 错误 失败 bug 卡 慢 崩溃 异常 不行 不能 困难 复杂 麻烦 担心 风险 延迟 超时 占用 冲突"
    " 丢失 退化 下降 糟糕 烦 烂 难 贵 缺 不足 堵 浪费 怀疑 矛盾 漏 错 重 累 压力 瓶颈 阻塞 抖动".split()
)


def tokenize(text: str) -> list[str]:
    """中文分词，过滤停用词和噪声。jieba 不可用时退化为二元切分。"""
    try:
        import jieba

        words = jieba.lcut(text)
    except Exception:
        words = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z]+", text)
    out = []
    for w in words:
        w = w.strip()
        if len(w) < 2:
            continue
        if w in STOPWORDS:
            continue
        if re.fullmatch(r"[A-Za-z]+", w) and len(w) < 3:
            continue
        out.append(w)
    return out


def sentiment(text: str) -> float:
    """粗估情绪倾向，返回 -1..1。词典很粗糙，只看趋势。"""
    words = tokenize(text)
    if not words:
        return 0.0
    pos = sum(1 for w in words if w in POS_WORDS)
    neg = sum(1 for w in words if w in NEG_WORDS)
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 3)


class Dashboard:
    """累积字幕状态，供 HTTP 接口查询。"""

    def __init__(self, bucket: float = 5.0, keep_series: int = 144) -> None:
        self.bucket = bucket            # 曲线每个点代表几秒
        self.keep_series = keep_series
        self.lock = threading.Lock()

        self.lines: list[dict] = []     # {seq, iso, text, ts, sentiment}
        self.word_freq: Counter = Counter()
        self.seq = 0
        self.start_ts = time.time()
        self.status = "waiting"
        self.status_since = time.time()
        self.chars = 0

        # 曲线数据：按 bucket 秒分桶累计
        self._bucket_start = time.time()
        self._bucket_chars = 0
        self._bucket_sent: list[float] = []
        self.series: list[dict] = []    # {t, wpm, sent}

    # ---- 输入 -------------------------------------------------------------
    def add(self, text: str, ts: float | None = None, iso: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        ts = ts or time.time()
        with self.lock:
            self.seq += 1
            self.chars += len(text)
            s = sentiment(text)
            self.lines.append(
                {"seq": self.seq, "iso": iso or time.strftime("%H:%M:%S", time.localtime(ts)),
                 "text": text, "ts": ts, "sentiment": s}
            )
            self.word_freq.update(tokenize(text))
            self._bucket_chars += len(text)
            self._bucket_sent.append(s)
            self._roll_bucket(force=False)

    def set_status(self, status: str) -> None:
        with self.lock:
            if status != self.status:
                self.status = status
                self.status_since = time.time()

    def _roll_bucket(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._bucket_start < self.bucket:
            return
        dur = max(now - self._bucket_start, 0.001)
        wpm = round(self._bucket_chars / dur * 60, 1)
        sent = round(sum(self._bucket_sent) / len(self._bucket_sent), 3) if self._bucket_sent else 0.0
        self.series.append({"t": round(now - self.start_ts, 1), "wpm": wpm, "sent": sent})
        if len(self.series) > self.keep_series:
            self.series = self.series[-self.keep_series:]
        self._bucket_start = now
        self._bucket_chars = 0
        self._bucket_sent = []

    # ---- 输出 -------------------------------------------------------------
    def state(self, top: int = 60, recent: int = 40) -> dict:
        with self.lock:
            self._roll_bucket(force=False)
            elapsed = max(time.time() - self.start_ts, 1.0)
            words = [
                {"w": w, "n": n}
                for w, n in self.word_freq.most_common(top)
                if n >= 2 or len(self.word_freq) < top
            ]
            return {
                "status": self.status,
                "last_seq": self.seq,
                "stats": {
                    "lines": self.seq,
                    "chars": self.chars,
                    "seconds": round(elapsed),
                    "wpm": round(self.chars / elapsed * 60, 1),
                    "vocab": len(self.word_freq),
                    "started": time.strftime("%H:%M:%S", time.localtime(self.start_ts)),
                },
                "words": words,
                "series": self.series[-self.keep_series:],
                "recent": self.lines[-recent:][::-1],
            }


# ---- 演示数据 -------------------------------------------------------------
DEMO_SENTENCES = [
    "今天我们讨论一下多智能体强化学习的收敛性问题",
    "这个实验结果看起来比基线要好一些",
    "共识机制的效率还有比较大的优化空间",
    "我们看一下区块链的吞吐量指标有没有提升",
    "这里的奖励函数设计是有问题的需要重新调整",
    "大家可以看一下屏幕上的这张对比图",
    "下一阶段我们要补充更多的对比实验",
    "这个模型的训练时间有点太长了",
    "数据在第九轮之后开始稳定收敛",
    "我们把超参数再调整一下应该会更快",
    "结论是采用 VDN 加 CARS 的组合方案",
    "记得把超参数整理成表格下周一同步给大家",
    "这个方案的风险在于通信开销可能会超预算",
    "刚才那次训练崩溃了显存不够用",
    "现在的吞吐量已经明显改善了不少",
    "这块逻辑实现起来比想象中简单",
]


def run_demo(dash: Dashboard, stop_event: threading.Event, interval: float = 4.5) -> None:
    """造数据线程：没有字幕窗口时也能看到看板长什么样。

    interval 默认 4.5 秒一句（一句约 18 字），换算约 240 字/分钟，
    和真人说话的语速接近 —— 喂太快语速 KPI 会假得离谱。
    """
    random.seed(20261001)
    dash.set_status("demo")
    while not stop_event.is_set():
        dash.add(random.choice(DEMO_SENTENCES))
        stop_event.wait(interval)


def run_replay(dash: Dashboard, path: Path, stop_event: threading.Event, speed: float = 8.0) -> None:
    """从 JSONL 回放，按 speed 倍速喂给看板。"""
    from caption_source import load_jsonl

    dash.set_status("replay")
    rows = list(load_jsonl(path))
    if not rows:
        dash.set_status("empty")
        return
    t0 = rows[0].get("ts", time.time())
    for r in rows:
        if stop_event.is_set():
            break
        ts = r.get("ts", time.time())
        wait = (ts - t0) / speed
        if wait > 0:
            stop_event.wait(min(wait, 2.0))
        dash.add(r.get("text", ""), ts=ts, iso=r.get("iso"))
    dash.set_status("done")


def run_live(dash: Dashboard, stop_event: threading.Event, jsonl: Path | None) -> None:
    """实时抓取 Win11 实时字幕。"""
    from caption_source import CaptionTap

    def on_line(line):
        dash.add(line.text, ts=line.ts, iso=line.iso)

    def on_status(s):
        dash.set_status(s)

    tap = CaptionTap(on_line=on_line, on_status=on_status, jsonl=jsonl)
    if not tap.start(wait_window=10.0):
        dash.set_status("lost")
        print("没找到实时字幕窗口，请先按 Win + Ctrl + L 打开", file=sys.stderr, flush=True)
        return
    print("已连上实时字幕窗口", flush=True)
    while not stop_event.is_set():
        time.sleep(0.5)
    tap.stop()


# ---- HTTP ----------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>实时字幕看板</title>
<style>
  :root{
    --bg:#f5f7fa; --card:#ffffff; --line:#e3e8ef; --ink:#1a2230; --muted:#6b7684;
    --accent:#2f6feb; --pos:#d92b3a; --neg:#0f9960; --warn:#e8890c;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;}
  header{display:flex;align-items:center;gap:14px;padding:14px 22px;background:var(--card);
         border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
  h1{font-size:17px;margin:0;font-weight:700;letter-spacing:.5px}
  .badge{font-size:12px;padding:3px 10px;border-radius:20px;background:#eef2f7;color:var(--muted)}
  .badge.live{background:#e6f4ea;color:#137a3c}
  .badge.waiting,.badge.lost{background:#fdecea;color:#a3302b}
  .spacer{flex:1}
  .meta{font-size:12px;color:var(--muted)}
  main{max-width:1400px;margin:0 auto;padding:18px 22px 40px}
  .grid{display:grid;grid-template-columns:1.35fr 1fr;gap:16px}
  @media(max-width:1000px){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card h2{font-size:13px;margin:0 0 10px;color:var(--muted);font-weight:600;letter-spacing:.6px}
  .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:16px}
  @media(max-width:1000px){.kpis{grid-template-columns:repeat(3,1fr)}}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
  .kpi .v{font-size:24px;font-weight:700;line-height:1.1}
  .kpi .k{font-size:11px;color:var(--muted);margin-top:4px}
  #cloud{width:100%;height:330px;display:block}
  #chart{width:100%;height:190px;display:block}
  .feed{max-height:330px;overflow:auto}
  .row{display:flex;gap:10px;padding:7px 0;border-bottom:1px dashed #eef1f5;font-size:13.5px;line-height:1.55}
  .row:last-child{border-bottom:none}
  .row .t{color:var(--muted);font-size:11.5px;padding-top:2px;min-width:52px}
  .row .x{flex:1}
  .row .s{font-size:11px;padding:1px 6px;border-radius:4px;align-self:flex-start}
  .s.p{background:#fdecec;color:#a3302b}.s.n{background:#e6f4ea;color:#137a3c}.s.z{background:#f1f3f6;color:#8a94a2}
  .bars{margin-top:6px}
  .bar{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12.5px}
  .bar .w{min-width:74px;color:var(--ink);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar .t{flex:1;height:12px;background:#f0f3f7;border-radius:6px;overflow:hidden}
  .bar .f{height:100%;background:linear-gradient(90deg,#6aa1ff,#2f6feb);border-radius:6px;transition:width .5s}
  .bar .n{min-width:24px;color:var(--muted)}
  .note{font-size:11.5px;color:var(--muted);margin-top:8px;line-height:1.6}
</style>
</head>
<body>
<header>
  <h1>实时字幕看板</h1>
  <span id="status" class="badge">连接中</span>
  <span class="spacer"></span>
  <span class="meta" id="meta">—</span>
</header>
<main>
  <div class="kpis">
    <div class="kpi"><div class="v" id="k-lines">0</div><div class="k">句子数</div></div>
    <div class="kpi"><div class="v" id="k-chars">0</div><div class="k">累计字数</div></div>
    <div class="kpi"><div class="v" id="k-wpm">0</div><div class="k">语速 字/分钟</div></div>
    <div class="kpi"><div class="v" id="k-vocab">0</div><div class="k">不同词汇</div></div>
    <div class="kpi"><div class="v" id="k-dur">0:00</div><div class="k">已进行</div></div>
    <div class="kpi"><div class="v" id="k-sent">—</div><div class="k">情绪倾向</div></div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>词云 · 词频越高字越大</h2>
      <canvas id="cloud"></canvas>
    </div>
    <div class="card">
      <h2>热词 TOP 12</h2>
      <div class="bars" id="bars"></div>
      <div class="note">停用词（的 / 了 / 我们 等）已剔除，只统计长度 ≥2 的词。</div>
    </div>
  </div>

  <div class="grid" style="margin-top:16px">
    <div class="card">
      <h2>语速与情绪曲线（每 5 秒一个点）</h2>
      <canvas id="chart"></canvas>
      <div class="note">蓝线＝语速（字/分钟）；红绿散点＝情绪倾向，偏红偏正面、偏绿偏负面。
        情绪用的是几十个词的粗糙词典打分，<b>只能看趋势，不能当情感分析结论</b>。</div>
    </div>
    <div class="card">
      <h2>实时字幕流</h2>
      <div class="feed" id="feed"></div>
    </div>
  </div>
</main>

<script>
const STATUS_TEXT = {waiting:'等待字幕',capturing:'抓取中',lost:'窗口丢失',demo:'演示数据',replay:'回放中',done:'回放结束',empty:'日志为空'};
let lastSeq = -1, lastWordsKey = '';

function fmtDur(s){const m=Math.floor(s/60),r=Math.floor(s%60);return m+':'+(r<10?'0':'')+r;}
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

const cloud = document.getElementById('cloud');
const cctx = cloud.getContext('2d');
const chart = document.getElementById('chart');
const hctx = chart.getContext('2d');
const PALETTE = ['#2f6feb','#e8890c','#0f9960','#8b5cf6','#d92b3a','#0891b2','#c2410c','#4d7c0f'];

function fit(cv){const r=cv.getBoundingClientRect();const d=window.devicePixelRatio||1;
  if(cv.width!==Math.round(r.width*d)){cv.width=Math.round(r.width*d);cv.height=Math.round(r.height*d);}
  const c=cv.getContext('2d');c.setTransform(d,0,0,d,0,0);return {w:r.width,h:r.height};}

function drawCloud(words){
  const {w,h}=fit(cloud);
  cctx.clearRect(0,0,w,h);
  if(!words.length){
    cctx.fillStyle='#9aa4b2';cctx.font='13px "Microsoft YaHei"';
    cctx.fillText('还没有词，等字幕进来…',10,20);return;
  }
  const max=words[0].n, min=words[words.length-1].n, span=Math.max(max-min,1);
  const placed=[];
  const cx=w/2, cy=h/2;
  words.forEach((it,i)=>{
    const size=Math.round(15+(it.n-min)/span*40);
    cctx.font='700 '+size+'px "Microsoft YaHei",sans-serif';
    const tw=cctx.measureText(it.w).width, th=size*1.18;
    for(let a=0;a<2600;a++){
      const ang=a*0.35, r=8*Math.sqrt(a);
      const x=cx+r*Math.cos(ang)*1.75-tw/2;
      const y=cy+r*Math.sin(ang)*0.85-th/2;
      if(x<2||y<2||x+tw>w-2||y+th>h-2) continue;
      let hit=false;
      for(const p of placed){ if(!(x+tw<p.x||p.x+p.tw<x||y+th<p.y||p.y+p.th<y)){hit=true;break;} }
      if(hit) continue;
      placed.push({x,y,tw,th});
      const alpha=0.55+0.45*(it.n-min)/span;
      cctx.fillStyle=PALETTE[i%PALETTE.length];
      cctx.globalAlpha=alpha;
      cctx.fillText(it.w,x,y+th*0.82);
      cctx.globalAlpha=1;
      break;
    }
  });
}

function drawChart(series){
  const {w,h}=fit(chart);
  hctx.clearRect(0,0,w,h);
  if(series.length<2){
    hctx.fillStyle='#9aa4b2';hctx.font='12px "Microsoft YaHei"';
    hctx.fillText('采集中…',8,16);return;
  }
  const pad=26, gw=w-pad-8, gh=h-pad-16;
  const vals=series.map(p=>p.wpm);
  const vmax=Math.max(60,Math.max(...vals)*1.15);
  hctx.strokeStyle='#eceff4';hctx.lineWidth=1;
  for(let i=0;i<=3;i++){const y=8+gh*i/3;hctx.beginPath();hctx.moveTo(pad,y);hctx.lineTo(w-8,y);hctx.stroke();}
  hctx.fillStyle='#9aa4b2';hctx.font='10px "Microsoft YaHei"';
  hctx.fillText(Math.round(vmax)+'',2,12);hctx.fillText('0',2,8+gh);

  const X=i=>pad+gw*(i/(series.length-1));
  const Y=v=>8+gh-gh*(v/vmax);
  // 情绪散点
  series.forEach((p,i)=>{
    if(!p.sent) return;
    hctx.beginPath();
    hctx.arc(X(i), 8+gh/2-gh/2*p.sent*0.85, 2.6, 0, 6.283);
    hctx.fillStyle = p.sent>0 ? 'rgba(217,43,58,.55)' : 'rgba(15,153,96,.55)';
    hctx.fill();
  });
  // 语速折线
  hctx.beginPath();
  series.forEach((p,i)=>{ i?hctx.lineTo(X(i),Y(p.wpm)):hctx.moveTo(X(i),Y(p.wpm)); });
  hctx.strokeStyle='#2f6feb';hctx.lineWidth=2;hctx.stroke();
  hctx.lineTo(X(series.length-1),8+gh);hctx.lineTo(X(0),8+gh);hctx.closePath();
  hctx.fillStyle='rgba(47,111,235,.10)';hctx.fill();
  // 最新值
  const last=series[series.length-1];
  hctx.fillStyle='#2f6feb';hctx.font='600 11px "Microsoft YaHei"';
  hctx.fillText(last.wpm+' 字/分', X(series.length-1)-46, Math.max(Y(last.wpm)-6,12));
}

function render(d){
  const st=d.stats;
  document.getElementById('k-lines').textContent=st.lines;
  document.getElementById('k-chars').textContent=st.chars;
  document.getElementById('k-wpm').textContent=st.wpm;
  document.getElementById('k-vocab').textContent=st.vocab;
  document.getElementById('k-dur').textContent=fmtDur(st.seconds);
  const s=d.series.slice(-8), avg=s.length? s.reduce((a,b)=>a+b.sent,0)/s.length : 0;
  const se=document.getElementById('k-sent');
  se.textContent = Math.abs(avg)<0.05?'持平':(avg>0?'偏正面 '+avg.toFixed(2):'偏负面 '+avg.toFixed(2));
  se.style.color = Math.abs(avg)<0.05?'#6b7684':(avg>0?'#d92b3a':'#0f9960');

  const badge=document.getElementById('status');
  badge.textContent=STATUS_TEXT[d.status]||d.status;
  badge.className='badge'+(d.status==='capturing'?' live':(d.status==='waiting'||d.status==='lost'?' waiting':''));
  document.getElementById('meta').textContent='开始 '+st.started+' · 每 0.8 秒刷新';

  const key=d.words.map(w=>w.w+':'+w.n).join('|');
  if(key!==lastWordsKey){
    lastWordsKey=key;
    drawCloud(d.words.slice(0,60));
    const top=d.words.slice(0,12), mx=top.length?top[0].n:1;
    document.getElementById('bars').innerHTML=top.map(it=>
      '<div class="bar"><span class="w">'+esc(it.w)+'</span><span class="t"><span class="f" style="width:'
      +Math.round(it.n/mx*100)+'%"></span></span><span class="n">'+it.n+'</span></div>').join('');
  }
  drawChart(d.series);

  if(d.last_seq!==lastSeq){
    lastSeq=d.last_seq;
    document.getElementById('feed').innerHTML=d.recent.map(r=>{
      const s=r.sentiment||0;
      const cls=s>0.05?'p':(s<-0.05?'n':'z');
      const tag=s>0.05?'+':(s<-0.05?'−':'○');
      return '<div class="row"><span class="t">'+esc(r.iso)+'</span><span class="x">'+esc(r.text)
        +'</span><span class="s '+cls+'">'+tag+'</span></div>';
    }).join('');
  }
}

async function tick(){
  try{
    const r=await fetch('/api/state?t='+Date.now());
    render(await r.json());
  }catch(e){
    document.getElementById('status').textContent='连接断开';
    document.getElementById('status').className='badge waiting';
  }
}
tick();
setInterval(tick,800);
window.addEventListener('resize',()=>{lastWordsKey='';tick();});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    dash: Dashboard = None  # 由 main 注入

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            body = json.dumps(self.dash.state(), ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)


def main() -> int:
    ap = argparse.ArgumentParser(description="实时字幕可视化看板")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true", help="造数据演示（不需要字幕窗口）")
    src.add_argument("--from", dest="from_file", help="从 JSONL 字幕日志回放")
    ap.add_argument("--port", type=int, default=8756)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--speed", type=float, default=8.0, help="回放倍速")
    ap.add_argument("--jsonl", help="实时模式下同时把字幕写入该 JSONL 文件")
    args = ap.parse_args()

    dash = Dashboard()
    stop = threading.Event()

    if args.demo:
        t = threading.Thread(target=run_demo, args=(dash, stop), daemon=True)
    elif args.from_file:
        t = threading.Thread(target=run_replay, args=(dash, Path(args.from_file), stop, args.speed), daemon=True)
    else:
        t = threading.Thread(target=run_live, args=(dash, stop, Path(args.jsonl) if args.jsonl else None), daemon=True)
    t.start()

    Handler.dash = dash
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 58, flush=True)
    print(f"实时字幕看板已启动：{url}", flush=True)
    print("浏览器打开上面的地址；Ctrl+C 结束", flush=True)
    print("=" * 58, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
