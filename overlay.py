"""
overlay.py — 屏幕底部实时字幕浮层（双语可选）

干什么
------
把 Windows 11 实时字幕从那个小窗口里"拿出来"，贴到屏幕底部做成一条
半透明字幕条，盖在任何播放器 / 会议 / 网页视频上面看。

为什么不用 tkinter
------------------
托管 Python 环境没有 tkinter，而且 tkinter 做不出真正的鼠标穿透。
这里直接用 Win32 原生分层窗口（pywin32）：
  WS_EX_TOPMOST     永远置顶
  WS_EX_TRANSPARENT 鼠标穿透（不挡你点下面的东西）
  WS_EX_TOOLWINDOW  不进任务栏、不进 Alt+Tab
  WS_EX_NOACTIVATE  不抢焦点（否则看视频时会被打断）
  LWA_ALPHA         整体半透明

实时性设计
----------
在线翻译一次要约 1.2 秒，绝不能让它挡住字幕显示。所以：
  原文立刻显示 -> 译文在后台线程翻 -> 翻好后再补到原文下面。
字幕永远不卡顿。

用法
----
    python overlay.py                              # 只显示原文（全本地，不出网）
    python overlay.py --translate mymemory          # 英文 -> 中文（字幕文本会出网！）
    python overlay.py --lines 3 --font 26 --alpha 230
    python overlay.py --top                         # 放到屏幕顶部

快捷键
------
    Ctrl+Alt+H  显示 / 隐藏
    Ctrl+Alt+Q  退出
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import win32api
import win32con
import win32gui
import win32ui

from caption_source import CaptionTap
from translator import get_translator

WM_UPDATE = win32con.WM_APP + 1
HOTKEY_TOGGLE = 1
HOTKEY_QUIT = 2

DEBUG = os.environ.get("CAPTION_DEBUG") == "1"


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"[overlay] {msg}", file=sys.stderr, flush=True)


# ---- 原生 Unicode 文本绘制 --------------------------------------------------
# pywin32 的 CDC.DrawText 内部走 ANSI 转换，中文会变乱码（实测踩过），
# 所以文字绘制全部改用 ctypes 直调 user32.DrawTextW（Unicode 版）。
_user32 = ctypes.windll.user32
DT_CALCRECT = 0x400
DT_WORDBREAK = 0x10
DT_EDITCONTROL = 0x200
DT_NOPREFIX = 0x800


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def draw_text_w(hdc: int, text: str, rect4: tuple[int, int, int, int], fmt: int):
    """用 DrawTextW 画字。返回 (高度, 实际矩形)。"""
    r = _RECT(*rect4)
    h = _user32.DrawTextW(hdc, text, -1, ctypes.byref(r), fmt)
    return h, (r.left, r.top, r.right, r.bottom)


class Overlay:
    def __init__(
        self,
        lines: int = 3,
        font: int = 24,
        sub_font: int = 18,
        alpha: int = 232,
        width_ratio: float = 0.78,
        bottom_margin: int = 80,
        ttl: float = 20.0,
        bg: tuple[int, int, int] = (16, 16, 20),
        translator=None,
        top: bool = False,
    ) -> None:
        self.max_lines = max(1, lines)
        self.font_size = font
        self.sub_font_size = sub_font
        self.alpha = max(40, min(255, alpha))
        self.width_ratio = width_ratio
        self.bottom_margin = bottom_margin
        self.ttl = ttl
        self.bg = bg
        self.tr = translator
        self.place_top = top

        self.rows: deque[dict] = deque(maxlen=64)
        self._q: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tr")
        self._lock = threading.Lock()

        self.hwnd = 0
        self.visible = True
        self._fonts: dict[str, object] = {}
        self._msg_map: dict[int, object] = {}

        self._screen_w = win32api.GetSystemMetrics(0)
        self._screen_h = win32api.GetSystemMetrics(1)
        self.W = int(self._screen_w * self.width_ratio)
        self.H = 120

    # ---- 窗口创建 --------------------------------------------------------
    def create(self) -> bool:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        hinst = win32api.GetModuleHandle(None)
        cls_name = "CaptionOverlayCls"

        self._msg_map = {
            win32con.WM_PAINT: self._on_paint,
            win32con.WM_ERASEBKGND: self._on_erase,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_HOTKEY: self._on_hotkey,
            WM_UPDATE: self._on_update,
            win32con.WM_TIMER: self._on_timer,
        }

        try:
            wc = win32gui.WNDCLASS()
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            wc.lpfnWndProc = self._msg_map
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass                      # 已注册过，直接复用

        style = win32con.WS_POPUP | win32con.WS_VISIBLE
        ex = (
            win32con.WS_EX_TOPMOST
            | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_NOACTIVATE
        )
        self.hwnd = win32gui.CreateWindowEx(
            ex, cls_name, "CaptionOverlay", style,
            0, 0, self.W, self.H, 0, 0, hinst, None,
        )
        if not self.hwnd:
            return False

        win32gui.SetLayeredWindowAttributes(
            self.hwnd, 0, self.alpha, win32con.LWA_ALPHA
        )
        try:
            win32gui.RegisterHotKey(
                self.hwnd, HOTKEY_TOGGLE, win32con.MOD_CONTROL | win32con.MOD_ALT, ord("H")
            )
            win32gui.RegisterHotKey(
                self.hwnd, HOTKEY_QUIT, win32con.MOD_CONTROL | win32con.MOD_ALT, ord("Q")
            )
        except Exception:
            pass
        ctypes.windll.user32.SetTimer(self.hwnd, 1, 1000, None)   # 每秒检查过期行
        self._resize(self.H)
        return True

    # ---- 消息处理 --------------------------------------------------------
    def _on_erase(self, hwnd, msg, wparam, lparam):
        return 1                                        # 自己画，不让系统擦（防闪烁）

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        try:
            win32gui.UnregisterHotKey(hwnd, HOTKEY_TOGGLE)
            win32gui.UnregisterHotKey(hwnd, HOTKEY_QUIT)
        except Exception:
            pass
        win32gui.PostQuitMessage(0)
        return 0

    def _on_hotkey(self, hwnd, msg, wparam, lparam):
        if wparam == HOTKEY_TOGGLE:
            self.visible = not self.visible
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW if self.visible else win32con.SW_HIDE)
        elif wparam == HOTKEY_QUIT:
            win32gui.DestroyWindow(hwnd)
        return 0

    def _on_timer(self, hwnd, msg, wparam, lparam):
        if self._expire():
            win32gui.InvalidateRect(hwnd, None, True)
        return 0

    def _on_update(self, hwnd, msg, wparam, lparam):
        changed = False
        while True:
            try:
                kind, payload = self._q.get_nowait()
            except queue.Empty:
                break
            dbg(f"update kind={kind}")
            if kind == "new":
                self.rows.append(payload)
                changed = True
                if self.tr is not None:
                    self._pool.submit(self._translate_row, payload)
            elif kind == "tr":
                changed = True                          # 译文已写进 row，重画即可
        if changed:
            win32gui.InvalidateRect(hwnd, None, True)
        return 0

    def _translate_row(self, row: dict) -> None:
        try:
            out = self.tr.translate(row["text"])
        except Exception:
            out = None
        if out:
            row["trans"] = out
            self._q.put(("tr", row))
            try:
                win32gui.PostMessage(self.hwnd, WM_UPDATE, 0, 0)
            except Exception:
                pass

    def _expire(self) -> bool:
        """丢掉过期的行，返回是否有变化。"""
        if self.ttl <= 0:
            while len(self.rows) > self.max_lines:
                self.rows.popleft()
            return False
        now = time.time()
        changed = False
        while self.rows and (now - self.rows[0]["ts"]) > self.ttl:
            self.rows.popleft()
            changed = True
        while len(self.rows) > self.max_lines:
            self.rows.popleft()
            changed = True
        return changed

    # ---- 对外：从任意线程推入新字幕 --------------------------------------
    def push(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        dbg(f"push {text[:40]!r}")
        row = {"text": text, "trans": None, "ts": time.time()}
        self._q.put(("new", row))
        try:
            win32gui.PostMessage(self.hwnd, WM_UPDATE, 0, 0)
        except Exception:
            pass

    # ---- 绘制 ------------------------------------------------------------
    def _font(self, size: int, bold: bool):
        key = f"{size}{'b' if bold else ''}"
        if key not in self._fonts:
            self._fonts[key] = win32ui.CreateFont(
                {"name": "Microsoft YaHei UI", "height": -size, "weight": 700 if bold else 400}
            )
        return self._fonts[key]

    def _resize(self, h: int) -> None:
        h = max(40, min(h, int(self._screen_h * 0.6)))
        self.H = h
        x = (self._screen_w - self.W) // 2
        y = self.bottom_margin if self.place_top else self._screen_h - h - self.bottom_margin
        win32gui.SetWindowPos(
            self.hwnd, win32con.HWND_TOPMOST, x, y, self.W, h,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )

    def _layout(self, dc) -> tuple[list, int]:
        """算出要画哪些块、总高度多少。"""
        pad_x, pad_y = 18, 12
        text_w = self.W - pad_x * 2
        blocks = []
        total = pad_y * 2

        for row in list(self.rows)[-self.max_lines:]:
            parts = [("main", row["text"], self.font_size)]
            if row.get("trans"):
                parts.append(("sub", row["trans"], self.sub_font_size))
            for kind, txt, size in parts:
                font = self._font(size, kind == "main")
                dc.SelectObject(font)
                # DrawTextW + DT_CALCRECT：返回所需高度（int）
                hdc = dc.GetSafeHdc()
                fmt = DT_CALCRECT | DT_WORDBREAK | DT_EDITCONTROL | DT_NOPREFIX
                need_h, _ = draw_text_w(hdc, txt, (0, 0, text_w, 0), fmt)
                h = max(need_h, size + 4)
                blocks.append((kind, txt, h, size))
                total += h + 4
        return blocks, total

    def _on_paint(self, hwnd, msg, wparam, lparam):
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            dc = win32ui.CreateDCFromHandle(hdc)
            mem = dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(dc, self.W, self.H)
            mem.SelectObject(bmp)

            blocks, content_h = self._layout(mem)
            dbg(f"paint blocks={len(blocks)} content_h={content_h} H={self.H} rows={len(self.rows)}")
            if content_h != self.H:
                self._resize(content_h)                 # 窗口紧贴内容，不留空条
                win32gui.EndPaint(hwnd, ps)
                win32gui.InvalidateRect(hwnd, None, False)
                return 0

            mem.FillSolidRect((0, 0, self.W, self.H), _rgb(self.bg))
            mem.SetBkMode(win32con.TRANSPARENT)

            pad_x, pad_y = 18, 12
            text_w = self.W - pad_x * 2
            y = pad_y
            hdc = mem.GetSafeHdc()
            fmt = DT_WORDBREAK | DT_EDITCONTROL | DT_NOPREFIX
            for kind, txt, h, size in blocks:
                font = self._font(size, kind == "main")
                mem.SelectObject(font)
                rect = (pad_x, y, pad_x + text_w, y + h)
                # 描边：先画四周黑字，再画正文，保证任何画面背景下都看得清
                mem.SetTextColor(_rgb((0, 0, 0)))
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    draw_text_w(hdc, txt, (rect[0] + dx, rect[1] + dy,
                                           rect[2] + dx, rect[3] + dy), fmt)
                mem.SetTextColor(_rgb((255, 255, 255)) if kind == "main" else _rgb((130, 226, 226)))
                draw_text_w(hdc, txt, rect, fmt)
                y += h + 4

            dc.BitBlt((0, 0), (self.W, self.H), mem, (0, 0), win32con.SRCCOPY)
        except Exception as e:
            print(f"[paint] {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            try:
                win32gui.EndPaint(hwnd, ps)
            except Exception:
                pass
        return 0

    def run(self) -> None:
        win32gui.PumpMessages()


def _rgb(c: tuple[int, int, int]) -> int:
    return c[0] | (c[1] << 8) | (c[2] << 16)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="屏幕底部实时字幕浮层")
    ap.add_argument("--lines", type=int, default=3, help="最多显示几行（默认 3）")
    ap.add_argument("--font", type=int, default=24, help="原文字号（默认 24）")
    ap.add_argument("--sub-font", type=int, default=18, help="译文字号（默认 18）")
    ap.add_argument("--alpha", type=int, default=232, help="不透明度 0-255（默认 232）")
    ap.add_argument("--width", type=float, default=0.78, help="占屏宽比例（默认 0.78）")
    ap.add_argument("--margin", type=int, default=80, help="距屏幕边缘像素（默认 80）")
    ap.add_argument("--ttl", type=float, default=20.0, help="每行保留秒数（默认 20）")
    ap.add_argument("--top", action="store_true", help="放到屏幕顶部而不是底部")
    ap.add_argument("--translate", default="none",
                    help="翻译源：none（默认，不出网）/ mymemory（字幕会发到第三方！）")
    ap.add_argument("--to", default="zh-CN", help="目标语言（默认 zh-CN）")
    ap.add_argument("--from", dest="src", default="en", help="源语言（默认 en）")
    ap.add_argument("--log", default="", help="同时把字幕落盘到该 JSONL 文件")
    args = ap.parse_args()

    tr = get_translator(args.translate, target=args.to, source=args.src,
                        cache_path="cache/translate.json")
    if tr.name != "none":
        print("⚠️  在线翻译已开启：字幕文本会发送到第三方服务器，")
        print("    实时字幕「音频不出本机」的优势不再成立。不确定就不要开。")

    ov = Overlay(
        lines=args.lines, font=args.font, sub_font=args.sub_font, alpha=args.alpha,
        width_ratio=args.width, bottom_margin=args.margin, ttl=args.ttl,
        translator=tr, top=args.top,
    )
    if not ov.create():
        print("创建浮层窗口失败")
        return 1

    tap = CaptionTap(
        on_line=lambda line: ov.push(line.text),
        on_status=lambda s: print(f"[status] {s}", flush=True) if s.startswith("error") else None,
        jsonl=args.log or None,
    )
    if not tap.start(wait_window=0):
        print("没找到实时字幕窗口，请先按 Win + Ctrl + L")

    print("浮层已启动。Ctrl+Alt+H 显示/隐藏，Ctrl+Alt+Q 退出。")
    try:
        ov.run()
    except KeyboardInterrupt:
        pass
    finally:
        tap.stop()
        if hasattr(tr, "flush"):
            tr.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
