"""
translator.py — 字幕翻译源（可插拔，默认关闭）

设计原则
--------
Windows 11 实时字幕最大的优势是"音频不出本机"。一旦接在线翻译，
字幕文本就会发到第三方服务器，这个优势就没了。所以：

  * 默认不翻译（none），保持全本地；
  * 想看译文，必须显式加 --translate 参数，并且知道代价。

优先级建议（零隐私成本优先）：
  1. 系统自带翻译：字幕窗口 → 设置(齿轮) → 翻译字幕为 → 选语言。
     这是微软本地/云端的官方链路，浮层抓到什么就显示什么。
  2. 在线翻译：本模块的 mymemory，免费匿名额度，会出网。

用法
----
    from translator import get_translator
    tr = get_translator("mymemory", target="zh-CN", cache_path="cache/tr.json")
    tr.translate("This is a caption line.")   -> "这是一条字幕。"
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_MAX_CHARS = 480          # 匿名接口单次上限约 500 字节，留余量
DEFAULT_TIMEOUT = 8.0


class Translator:
    """翻译源基类。"""

    name = "base"

    def translate(self, text: str) -> str | None:
        raise NotImplementedError


class NoneTranslator(Translator):
    """不翻译，原样显示。默认。"""

    name = "none"

    def translate(self, text: str) -> str | None:
        return None


class MyMemoryTranslator(Translator):
    """MyMemory 免费匿名翻译。

    代价要说清：
      - 字幕文本会发到 api.mymemory.translated.net；
      - 匿名额度约每日 5000 字符 / 若干次请求，用完当天返回限流；
      - 质量不如大模型翻译，短句尚可，长句一般。
    自带磁盘缓存：同一句话只翻译一次，既省额度也降低延迟。
    """

    name = "mymemory"

    def __init__(
        self,
        target: str = "zh-CN",
        source: str = "en",
        cache_path: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        email: str | None = None,
    ) -> None:
        self.target = target
        self.source = source
        self.timeout = timeout
        self.email = email
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache: dict[str, str] = {}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._limited = False          # 当天额度用尽后不再重试
        self._last_error = ""
        self.stats = {"calls": 0, "cache_hits": 0, "fails": 0}
        if self.cache_path:
            self._load_cache()

    # ---- 缓存 ----------------------------------------------------------
    def _load_cache(self) -> None:
        try:
            if self.cache_path and self.cache_path.exists():
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}

    def _save_cache(self) -> None:
        try:
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(
                    json.dumps(self.cache, ensure_ascii=False, indent=0), encoding="utf-8"
                )
        except Exception:
            pass

    # ---- 翻译 ----------------------------------------------------------
    def translate(self, text: str) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        if len(text) > MYMEMORY_MAX_CHARS:
            text = text[:MYMEMORY_MAX_CHARS]

        if text in self.cache:
            self.stats["cache_hits"] += 1
            return self.cache[text]
        if self._limited:
            return None

        params = {"q": text, "langpair": f"{self.source}|{self.target}"}
        if self.email:
            params["de"] = self.email
        url = MYMEMORY_URL + "?" + urllib.parse.urlencode(params)

        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with self._opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as e:                      # 网络失败一律降级为不翻译
            self.stats["fails"] += 1
            self._last_error = f"{type(e).__name__}: {e}"
            return None

        self.stats["calls"] += 1
        body = data.get("responseData") or {}
        out = (body.get("translatedText") or "").strip()
        info = str(data.get("responseDetails") or "")

        # MyMemory 限流时不报错，而是把说明塞进译文里，必须识别出来
        if not out or "MYMEMORY WARNING" in out.upper() or "LIMIT" in info.upper():
            self._limited = True
            self._last_error = info or out
            return None
        if out.upper() == text.upper():
            return None                              # 没翻出来（源语言就是目标语言）

        self.cache[text] = out
        if len(self.cache) % 20 == 1:
            self._save_cache()
        return out

    def flush(self) -> None:
        self._save_cache()


class CachedTranslator(Translator):
    """任意翻译源 + 磁盘缓存 + 失败降级（不因翻译挂掉字幕流）。"""

    def __init__(self, inner: Translator, cache_path: str | Path | None = None) -> None:
        self.inner = inner
        self.name = f"cached({inner.name})"
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache: dict[str, str] = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:
                self.cache = {}

    def translate(self, text: str) -> str | None:
        key = (text or "").strip()
        if not key:
            return None
        if key in self.cache:
            return self.cache[key]
        try:
            out = self.inner.translate(key)
        except Exception:
            out = None
        if out:
            self.cache[key] = out
            if self.cache_path and len(self.cache) % 20 == 1:
                try:
                    self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self.cache_path.write_text(
                        json.dumps(self.cache, ensure_ascii=False, indent=0),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
        return out


def get_translator(
    name: str = "none",
    target: str = "zh-CN",
    source: str = "en",
    cache_path: str | Path | None = None,
    **kw,
) -> Translator:
    """按名字取翻译源。未知名字一律退化成 none，绝不因配置错误崩掉。"""
    name = (name or "none").lower()
    if name in ("none", "off", "false", ""):
        return NoneTranslator()
    if name in ("mymemory", "mm"):
        return MyMemoryTranslator(target=target, source=source, cache_path=cache_path, **kw)
    return NoneTranslator()


def translate_batch(tr: Translator, texts: Iterable[str]) -> list[str | None]:
    return [tr.translate(t) for t in texts]


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    samples = [
        "This is a real time caption test.",
        "Let's look at the throughput of the consensus mechanism.",
        "The reward function design has a problem here.",
    ]
    tr = get_translator("mymemory", target="zh-CN", cache_path="cache/tr_test.json")
    print(f"翻译源：{tr.name}")
    t0 = time.time()
    for s in samples:
        print(f"  EN: {s}")
        print(f"  ZH: {tr.translate(s)}")
    print(f"耗时 {time.time() - t0:.2f}s  统计 {getattr(tr, 'stats', {})}  限流={getattr(tr, '_limited', False)}")
