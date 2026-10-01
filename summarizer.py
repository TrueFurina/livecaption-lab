"""
summarizer.py — 可选的大模型摘要增强

默认不启用。启用后会把会议字幕文本发到模型厂商并产生费用，所以：
  * 必须显式加 --llm 才会调用；
  * 调用前后打印真实 token 用量，不猜价格、不隐瞒花销；
  * 失败一律降级，绝不拖垮纪要生成（规则引擎的结果照样出）。

支持（都是 OpenAI 兼容的 /chat/completions）：
    deepseek / moonshot / zhipu / dashscope
模型名不写死：先查 /models 列表取第一个可用模型，避免用了已下线的名字。
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Iterable

TIMEOUT = 90.0
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"


class Provider:
    def __init__(self, name: str, base: str, key_env: str, models_url: str) -> None:
        self.name = name
        self.base = base
        self.key = os.environ.get(key_env, "")
        self.models_url = models_url
        self.model: str | None = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Authorization": f"Bearer {self.key}"})
        with self._opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def pick_model(self) -> str | None:
        if self.model:
            return self.model
        try:
            data = self._get(self.models_url)
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            # 排除语音/实时/多模态类，只要纯文本对话模型
            ids = [i for i in ids if not any(
                k in i.lower() for k in ("audio", "realtime", "omni", "tts", "embed"))]
            self.model = ids[0] if ids else None
        except Exception:
            self.model = None
        return self.model

    def chat(self, prompt: str, max_tokens: int = 1200) -> tuple[str, dict]:
        model = self.pick_model()
        if not model or not self.key:
            raise RuntimeError(f"{self.name}: 缺 key 或拿不到模型列表")
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        req = urllib.request.Request(
            self.base,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": UA,
                     "Authorization": f"Bearer {self.key}"},
        )
        with self._opener.open(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        usage = data.get("usage") or {}
        usage["model"] = model
        return text, usage


PROVIDERS = {
    "deepseek": Provider("deepseek", "https://api.deepseek.com/chat/completions",
                         "DEEPSEEK_API_KEY", "https://api.deepseek.com/models"),
    "moonshot": Provider("moonshot", "https://api.moonshot.cn/v1/chat/completions",
                         "MOONSHOT_API_KEY", "https://api.moonshot.cn/v1/models"),
    "zhipu": Provider("zhipu", "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                      "ZHIPU_API_KEY", "https://open.bigmodel.cn/api/paas/v4/models"),
    "dashscope": Provider("dashscope",
                          "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                          "DASHSCOPE_API_KEY",
                          "https://dashscope.aliyuncs.com/compatible-mode/v1/models"),
}

PROMPT = """你是一个会议纪要助手。下面是某场会议/课堂的实时字幕转写文本（无说话人、可能有识别错误）。
请输出严格的 JSON，不要有任何多余文字，格式：
{"summary":"150字以内的一段话总结",
 "decisions":["决议1","决议2"],
 "todos":[{"text":"待办内容","due":"截止时间或空字符串"}],
 "topics":[{"topic":"议题名","detail":"一两句说明"}]}

字幕文本：
"""


class Summarizer:
    def __init__(self, provider: Provider, max_chars: int = 6000) -> None:
        self.p = provider
        self.max_chars = max_chars
        self.last_usage: dict = {}

    def summarize(self, rec) -> str | None:
        text = "\n".join(u.text for u in rec.items)
        if not text.strip():
            return None
        if len(text) > self.max_chars:            # 超长就从尾部截断，保住最近的讨论
            text = text[-self.max_chars:]
        print(f"[llm] 调用 {self.p.name} / {self.p.pick_model()}，输入 {len(text)} 字 …", flush=True)
        try:
            out, usage = self.p.chat(PROMPT + text)
        except Exception as e:
            print(f"[llm] 调用失败：{type(e).__name__}: {e}")
            return None
        self.last_usage = usage
        print(f"[llm] token 用量：{usage}")
        try:
            data = json.loads(out[out.find("{"):out.rfind("}") + 1])
            lines = [data.get("summary", "")]
            if data.get("decisions"):
                lines.append("\n决议：\n" + "\n".join(f"- {d}" for d in data["decisions"]))
            if data.get("todos"):
                lines.append("\n待办：\n" + "\n".join(
                    f"- {t.get('text','')}" + (f"（{t['due']}）" if t.get("due") else "")
                    for t in data["todos"]))
            if data.get("topics"):
                lines.append("\n议题：\n" + "\n".join(
                    f"- {t.get('topic','')}：{t.get('detail','')}" for t in data["topics"]))
            return "\n".join(lines)
        except Exception:
            return out      # 模型没按格式来，原文返回也不算白跑


def get_summarizer(name: str) -> Summarizer | None:
    p = PROVIDERS.get((name or "").lower())
    if not p:
        return None
    return Summarizer(p)


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("用法：python summarizer.py deepseek|moonshot|zhipu|dashscope")
        sys.exit(1)
    sm = get_summarizer(sys.argv[1])
    if not sm:
        print("未知 provider")
        sys.exit(1)

    from meeting_notes import MeetingRecorder
    rec = MeetingRecorder()
    for i, s in enumerate([
        "今天我们讨论一下多智能体强化学习的收敛性问题。",
        "实验结果显示比基线好了百分之五，但方差还是偏大。",
        "结论是采用 VDN 加 CARS 的组合方案。",
        "需要尽快补充对比实验，明天之前把结果发出来。",
        "记得把超参数整理成一个表格，下周一同步给大家。",
    ]):
        rec.add(s, ts=1760000000 + i * 30)
    print(sm.summarize(rec) or "调用失败")
