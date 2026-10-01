"""
caption_source.py — Windows 11 实时字幕（Live Captions）文本流抓取器

背景
----
微软没有为 Live Captions 提供任何公开 API（无 SDK / COM / WinRT 接口）。
字幕文本只存在于 LiveCaptions.exe 创建的窗口中，唯一稳定的读取方式是
Windows UI Automation（UIA，即无障碍接口）。

本模块把这条链路封装成一个"字幕水龙头"：
    LiveCaptions.exe 窗口 -> UIA 轮询 -> 行级增量去重 -> 带时间戳的事件流

用法
----
    from caption_source import CaptionTap

    tap = CaptionTap(on_line=lambda line: print(line.text))
    tap.start()          # 后台线程开始抓取
    ...
    tap.stop()

前提
----
1) Windows 11 22H2 及以上（本机实测 25H2 / build 26200 可用）
2) 用户手动按 Win + Ctrl + L 打开实时字幕（本模块不代劳启动 GUI）
3) 依赖：pip install uiautomation

已知坑
------
- 窗口最小化后 UIA 仍可读；但把窗口移到屏幕外会导致 UIA 读取失败。
- 空闲状态元素 AutomationId 为 ReadyToCaptionTextBlock，
  开始转写后切换为 CaptionsTextBlock，元素引用需要定期刷新。
- 字幕窗口显示的是最近若干行，滚动时会丢弃旧行，因此必须做增量去重，
  否则同一句话会被重复吐出。
"""

from __future__ import annotations

import json
import difflib
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import uiautomation as auto

# ---- Live Captions 窗口与元素的稳定标识 ------------------------------------
LC_WINDOW_CLASS = "LiveCaptionsDesktopWindow"   # 跨语言版本一致
LC_PROCESS_NAME = "LiveCaptions.exe"

ID_CAPTIONS = "CaptionsTextBlock"        # 正在转写
ID_READY = "ReadyToCaptionTextBlock"     # 空闲占位（尚未开始转写）

# 不同语言版本下的占位文案，读到这些直接丢弃。
# 注意：微软的实际文案会随版本变化（实测 25H2 中文为
# "已准备好在 简体中文(中国大陆) 中显示实时字幕"），
# 因此光靠精确集合不够，下面还有关键词兜底 + 元素 ID 判定。
PLACEHOLDER_TEXTS = {
    "Ready for live subtitles",
    "Für Live-Untertitel bereit",
    "Prêt pour les sous-titres en direct",
    "Listo para los subtítulos en directo",
    "Pronto para legendas ao vivo",
    " pronto per i sottotitoli dal vivo",
    " 실시간 자막 준비 완료",
    "即時輔助字幕已就緒",
    "实时辅助字幕已就绪",
    "ライブ字幕の準備が完了しました",
    "Klaar voor live-ondertiteling",
    "Klar til live-undertekster",
    "Klar för liveundertexter",
    "Valmis live-tekstitykseen",
    "Готово к субтитрам в реальном времени",
    "Gotowe do napisów na żywo",
}

# 关键词兜底：只要命中，一律视为占位而非真实字幕
PLACEHOLDER_HINTS = (
    "已准备好",
    "就绪",
    "準備が完了",
    "준비 완료",
    "Ready for live",
    "Live-Untertitel bereit",
    "pronto per i sottotitoli",
)

# 窗口内可能的元素路径回退（foundIndex 1~3）
_CANDIDATE_INDEXES = (1, 2, 3)

# 字幕自动归档目录：每次"记录字幕"都会把句子追加到这里，按天一个 .md 文件。
# 设为 None 可关闭自动归档（测试 / 演示假数据场景下使用）。
DEFAULT_MD_DIR = r"E:\Program\Zimu"


def _md_header(day: str) -> str:
    """某天 md 文件首次创建时写入的头部。"""
    return (
        f"# 实时字幕归档 · {day}\n\n"
        f"> 由 livecaption-lab 自动保存。每行格式：`- HH:MM:SS 字幕文本`\n"
        f"> 记录之间用分隔行 `--- 记录开始 / 结束 HH:MM:SS ---` 标注。\n\n"
    )


@dataclass
class CaptionLine:
    """一条新增的字幕句子。"""

    text: str
    ts: float                      # time.time() 时间戳
    iso: str                       # 本地时间字符串，方便人看
    seq: int = 0                   # 从 1 开始的序号

    def to_dict(self) -> dict:
        return asdict(self)


def _process_running() -> bool:
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq LiveCaptions.exe"],
            capture_output=True, timeout=30,
        )
        return "LiveCaptions.exe" in out.stdout.decode("gbk", errors="replace")
    except Exception:
        return False


def restart_livecaptions(wait: float = 8.0) -> bool:
    """关掉再打开实时字幕，清空窗口里累积的历史字幕。

    字幕窗口是累积日志，不会自动清。做自测或开始新一段记录前
    重启一次，能避免把上一轮的内容当成新内容。
    """
    try:
        import pyautogui
    except ImportError:
        return False
    if _process_running():
        pyautogui.hotkey("win", "ctrl", "l")
        time.sleep(2.0)
    if not _process_running():
        pyautogui.hotkey("win", "ctrl", "l")
    win = find_caption_window(timeout=wait)
    return win is not None


def find_caption_window(timeout: float = 0.0, poll: float = 0.4) -> auto.Control | None:
    """按 ClassName 定位实时字幕窗口。timeout=0 表示只查一次。"""
    deadline = time.time() + timeout
    while True:
        try:
            win = auto.WindowControl(
                searchDepth=1,
                ClassName=LC_WINDOW_CLASS,
                foundIndex=1,
            )
            if win.Exists(maxSearchSeconds=0.2):
                return win
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def _element_text(ctrl: auto.Control) -> str:
    """尽量拿到元素文本：优先 Name，退化到 TextPattern，再退化到 WindowText。"""
    for getter in (
        lambda: ctrl.Name,
        lambda: ctrl.GetTextPattern().DocumentRange.GetText(-1),
        lambda: ctrl.WindowText(),
    ):
        try:
            v = getter()
            if v:
                return str(v)
        except Exception:
            continue
    return ""


def is_placeholder(text: str) -> bool:
    """判断某行是不是字幕窗口的占位文案，而不是真实转写结果。"""
    t = (text or "").strip()
    if not t:
        return True
    if t in PLACEHOLDER_TEXTS:
        return True
    return any(h in t for h in PLACEHOLDER_HINTS)


def _find_text_element(win: auto.Control) -> auto.Control | None:
    """在字幕窗口内定位文本元素，覆盖空闲态与转写态两种 AutomationId。"""
    for automation_id in (ID_CAPTIONS, ID_READY):
        for idx in _CANDIDATE_INDEXES:
            try:
                el = win.TextControl(AutomationId=automation_id, foundIndex=idx)
                if el.Exists(maxSearchSeconds=0.15):
                    return el
            except Exception:
                continue
    # 再退化：直接找任意带文本的控件
    try:
        for el in win.GetChildren():
            if el.ControlTypeName == "TextControl":
                return el
    except Exception:
        pass
    return None


class CaptionTap:
    """实时字幕抓取器。

    参数
    ----
    on_line:   收到新句子时的回调，签名 (CaptionLine) -> None
    on_status: 状态变化回调，签名 (str) -> None，如 "waiting" / "capturing" / "lost"
    mode:      "sentence"（默认）只吐定稿整句，自动合并逐字增长的中间态；
               "raw" 每有变化就吐一行，会包含大量 partial 碎片
        poll:      轮询间隔（秒），默认 0.1
    jsonl:     若给定路径，则每条句子追加写入该 JSONL 文件（可后续做日报）
    md_dir:    字幕自动归档目录，每句追加到该目录下 `字幕归档-YYYY-MM-DD.md`。
               默认 DEFAULT_MD_DIR（E:/Program/Zimu）；传 None 关闭自动归档。
    dedup_mem: 去重记忆的行数，防止窗口滚动导致重复吐字
    refresh:   元素引用刷新间隔（秒），应对空闲态到转写态的切换
    flush_idle: 最后一句静默多少秒就判定它已说完并定稿输出（默认 2.0）
    """

    def __init__(
        self,
        on_line: Callable[[CaptionLine], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        mode: str = "sentence",
        poll: float = 0.1,
        jsonl: str | Path | None = None,
        md_dir: str | Path | None = DEFAULT_MD_DIR,
        dedup_mem: int = 400,
        refresh: float = 5.0,
        flush_idle: float = 2.0,
        min_chars: int = 2,
    ) -> None:
        self.on_line = on_line
        self.on_status = on_status
        self.mode = mode if mode in ("sentence", "raw") else "sentence"
        self.poll = poll
        self.refresh = refresh
        self.jsonl_path = Path(jsonl) if jsonl else None
        self.md_dir = Path(md_dir) if md_dir else None
        self._md_day: str = ""
        self._md_path: Path | None = None

        self._seen: deque[str] = deque(maxlen=dedup_mem)
        self._seen_set: set[str] = set()
        self._pending: str | None = None   # 正在增长、尚未定稿的那句
        self._pending_ts: float = 0.0
        self._prev_tail: str = ""          # 上一次看到的最后一行
        self._primed: bool = False         # 是否已跳过历史字幕
        self._flushed: bool = False        # 当前这句是否已因空闲超时输出过
        self._last_emitted: str = ""       # 最近一次输出的完整文本，用于只吐增量
        self.prime_window: float = 1.5     # 连接后头几秒算历史
        self._start_ts: float = 0.0
        self.flush_idle = flush_idle
        self.min_chars = min_chars
        self._seq = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.lines: list[CaptionLine] = []

    # ---- 对外接口 ----------------------------------------------------------
    def start(self, wait_window: float = 0.0) -> bool:
        """启动后台抓取线程。wait_window>0 时会等待字幕窗口出现。"""
        if self._running:
            return True
        if wait_window > 0 and find_caption_window(timeout=wait_window) is None:
            self._emit_status("lost")
            return False
        self._running = True
        self._start_ts = time.time()
        self._append_md_session("记录开始")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._flush_pending()      # 收尾时把还在增长的那句定稿吐出来
        self._append_md_session("记录结束")

    def snapshot(self) -> list[CaptionLine]:
        with self._lock:
            return list(self.lines)

    def is_running(self) -> bool:
        """抓取线程是否仍活着。record.py 用它感知"字幕窗口已关闭"从而自动退出。"""
        return bool(self._running and self._thread and self._thread.is_alive())

    # ---- 内部 --------------------------------------------------------------
    def _emit_status(self, status: str) -> None:
        if self.on_status:
            try:
                self.on_status(status)
            except Exception:
                pass

    def _emit(self, line: CaptionLine) -> None:
        with self._lock:
            self.lines.append(line)
        if self.jsonl_path:
            try:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(line.to_dict(), ensure_ascii=False) + "\n")
            except Exception:
                pass
        self._append_md(line)
        if self.on_line:
            try:
                self.on_line(line)
            except Exception:
                pass

    # ---- Markdown 自动归档 -------------------------------------------------
    def _append_md_raw(self, text: str) -> None:
        """把一行文本追加到"当天的"归档 md 文件，必要时创建文件并写头部。"""
        if self.md_dir is None:
            return
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            if day != self._md_day:
                self._md_day = day
                self.md_dir.mkdir(parents=True, exist_ok=True)
                self._md_path = self.md_dir / f"字幕归档-{day}.md"
                if not self._md_path.exists():
                    with open(self._md_path, "w", encoding="utf-8") as f:
                        f.write(_md_header(day))
            with open(self._md_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _append_md(self, line: CaptionLine) -> None:
        self._append_md_raw(f"- {line.iso} {line.text}")

    def _append_md_session(self, kind: str) -> None:
        self._append_md_raw(
            f"> --- {kind} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---"
        )

    def _remember(self, text: str) -> None:
        if text in self._seen_set:
            return
        if len(self._seen) == self._seen.maxlen:
            oldest = self._seen.popleft()
            self._seen_set.discard(oldest)
        self._seen.append(text)
        self._seen_set.add(text)

    def _is_seen(self, text: str) -> bool:
        return text in self._seen_set

    @staticmethod
    def _join_lines(raw: str) -> str:
        """字幕窗口是多行的，滚动时同一句可能同时出现在两行里，
        直接拼接会得到"…问题…问题。"这种重复串，先按行保序去重。"""
        lines: list[str] = []
        for raw_line in raw.splitlines():
            l = raw_line.strip()
            if l and l not in lines:
                lines.append(l)
        return "".join(lines)

    def _strip_prefix(self, text: str) -> tuple[str | None, bool]:
        """剥掉已经输出过的前缀，返回 (增量, 是否为扩展)。

        返回 (None, True) 表示这次内容全是已有内容，没有新东西。
        """
        cands = [self._last_emitted] + [l.text for l in self.lines[-10:]]
        for p in cands:
            if not p or p == text:
                continue
            if text.startswith(p):
                out = text[len(p):].lstrip("，,、；; ")
                return (out or None), True
        return text, False

    def _is_redundant(self, text: str) -> bool:
        """新句是不是已被输出过的内容覆盖（重复句 / 碎片）。

        字幕窗口滚动时会把已输出过的句子换个位置再吐一次，
        也可能先给半截再给整句，这里把这类冗余挡掉。
        """
        for prev in reversed(self.lines[-60:]):
            p = prev.text
            if not p:
                continue
            m = difflib.SequenceMatcher(None, text, p, autojunk=False).find_longest_match(
                0, len(text), 0, len(p)
            )
            if m.size / max(len(text), 1) >= 0.8:
                return True
        return False

    def _commit(self, text: str) -> None:
        """定稿输出一句（同一句只输出一次）。"""
        text = (text or "").strip()
        if not text or is_placeholder(text):
            return
        if len(text) < self.min_chars:
            return
        if self._is_seen(text):
            return

        # ASR 会把连着说的几句用逗号串成累积长句，所以先剥离已知前缀，
        # 只留增量。顺序很关键：必须先剥离再判冗余，否则"长累积句+短增量"
        # 会被冗余规则当成重复内容整句吞掉（实测踩过）。
        out, extended = self._strip_prefix(text)
        if out is None:
            return
        # 增量可能只剩一个句号，按去掉标点后的有效字数再判一次
        if len(out.strip("。！？，、；,.!? ")) < self.min_chars:
            return
        if not extended and self._is_redundant(text):
            self._remember(text)
            return

        if self._is_seen(out):
            return
        self._last_emitted = text
        self._remember(out)
        self._seq += 1
        now_dt = datetime.now()
        self._emit(
            CaptionLine(
                text=out,
                ts=time.time(),
                iso=now_dt.strftime("%H:%M:%S"),
                seq=self._seq,
            )
        )

    def _flush_pending(self) -> None:
        """空闲超时或抓取结束时，把当前这句定稿输出。"""
        if self._prev_tail:
            self._commit(self._prev_tail)
            self._flushed = True
        self._pending = None
        self._pending_ts = 0.0

    def _check_idle(self) -> None:
        """最后一句静默够久就认为说完了，定稿输出。

        没有这条，遇到 ASR 迟迟不补句号的长句（实测会把多句用逗号连起来）
        会一直憋着不输出，实时性很差。
        """
        if self._pending and self._pending_ts and self.flush_idle > 0:
            if time.time() - self._pending_ts >= self.flush_idle:
                self._flush_pending()

    def _feed_sentence(self, raw: str) -> None:
        """sentence 模式：按"末尾追加 + 逐字增长"的真实行为切句。

        实测（见 debug_raw.py 的输出）搞清楚的行为：
          1. 字幕窗口是累积日志，历史行不会变，新句子作为新行追加在末尾；
          2. 正在说的那一句在最后一行里逐字增长。
        因此正确做法是：忽略历史行，只盯最后一行，用前缀关系判断
        它是在增长还是换了新句；换句时把上一句定稿输出。
        """
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        lines = [l for l in lines if not is_placeholder(l)]
        if not lines:
            return

        # 连接后的头 prime_window 秒里读到的都算"历史"，不输出。
        # 不能简单把"第一次读到的内容"当历史 —— 那样会把刚开始说的
        # 第一句也吞掉（实测踩过）。用时间窗更稳。
        if not self._primed:
            if self._start_ts and (time.time() - self._start_ts) < self.prime_window:
                for l in lines:
                    self._remember(l)
                self._prev_tail = lines[-1]
                return
            self._primed = True

        # 非最后一行若有新内容，说明已成行，直接定稿
        for l in lines[:-1]:
            self._commit(l)

        tail = lines[-1]
        if tail == self._prev_tail:
            return

        if self._prev_tail and (
            tail.startswith(self._prev_tail) or self._prev_tail.startswith(tail)
        ):
            # 同一句在增长：补齐被空闲超时截断掉的后续内容
            if len(tail) >= len(self._prev_tail):
                delta = tail[len(self._prev_tail):]
                self._prev_tail = tail
                self._pending = tail
                self._pending_ts = time.time()
                if self._flushed and len(delta) >= 8:
                    self._commit(delta)
            return

        # 换了新的一句：先把上一句定稿吐出去
        if self._prev_tail:
            self._commit(self._prev_tail)
        self._prev_tail = tail
        self._pending = tail
        self._pending_ts = time.time()
        self._flushed = False

    def _new_lines(self, raw: str) -> list[str]:
        """从窗口当前文本里挑出真正新增的行。"""
        out: list[str] = []
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in PLACEHOLDER_TEXTS or is_placeholder(line):
                continue
            if self._is_seen(line):
                continue
            self._remember(line)
            out.append(line)
        return out

    def _loop(self) -> None:
        win = find_caption_window()
        if win is None:
            self._emit_status("lost")
            self._running = False
            return

        el = _find_text_element(win)
        last_refresh = time.time()
        self._emit_status("ready")

        # 整个循环加异常保护：否则一次 UIA 调用失败就会静默杀掉抓取线程，
        # 表现为"抓了几句之后就再也没输出了"，极难排查。
        try:
            self._run_loop(win, el, last_refresh)
        except Exception as e:
            self._emit_status(f"error:{type(e).__name__}:{e}")
            self._running = False
            raise

    def _run_loop(self, win, el, last_refresh: float) -> None:
        while self._running:
            now = time.time()
            if el is None or (now - last_refresh) > self.refresh:
                el = _find_text_element(win)
                last_refresh = now
                if el is None:
                    self._emit_status("waiting")
                    if not win.Exists(maxSearchSeconds=0.2):
                        win = find_caption_window(timeout=3.0)
                        if win is None:
                            self._emit_status("lost")
                            break
                    time.sleep(self.poll)
                    continue

            try:
                aid = el.AutomationId or ""
            except Exception:
                aid = ""

            # 只有转写态元素（CaptionsTextBlock）的内容才是真实字幕。
            # 空闲态（ReadyToCaptionTextBlock）里放的是占位文案，必须跳过，
            # 否则会把"已准备好…显示实时字幕"当成一句真实字幕吐出去。
            if aid == ID_READY:
                self._emit_status("waiting")
                time.sleep(self.poll)
                continue

            raw = _element_text(el)
            if raw:
                self._emit_status("capturing")
                if self.mode == "sentence":
                    self._feed_sentence(raw)
                else:
                    now_dt = datetime.now()
                    for text in self._new_lines(raw):
                        self._seq += 1
                        self._emit(
                            CaptionLine(
                                text=text,
                                ts=time.time(),
                                iso=now_dt.strftime("%H:%M:%S"),
                                seq=self._seq,
                            )
                        )
            if self.mode == "sentence":
                self._check_idle()
            time.sleep(self.poll)


def load_jsonl(path: str | Path) -> Iterable[dict]:
    """读取由 jsonl 参数落盘的日志文件。"""
    p = Path(path)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
