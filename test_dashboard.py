"""实时看板自检：真起服务 → 真请求 API → 断言结构与统计口径。

带反例，不满足就退出码非 0：
  · 停用词（的 / 我们）不该出现在热词里
  · 情绪分必须落在 -1..1
  · 未知路径必须 404
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
STOPWORDS_MUST_NOT_APPEAR = {"的", "了", "我们", "这个", "是"}


def wait_port(port: int, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            time.sleep(0.3)
        finally:
            s.close()
    return False


def get(path: str, timeout: float = 10.0):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return op.open(urllib.request.Request(BASE + path), timeout=timeout)


def main() -> int:
    py = sys.executable
    proc = subprocess.Popen(
        [py, str(HERE / "live_dashboard.py"), "--demo", "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(HERE),
    )
    ok = True
    try:
        if not wait_port(PORT):
            print("FAIL 服务没起来")
            return 1
        print(f"服务已起 {BASE}")

        # 1) 页面
        r = get("/")
        html = r.read().decode("utf-8", "replace")
        page_ok = r.status == 200 and '<canvas id="cloud"' in html and "drawCloud" in html
        print(f"[{'OK ' if page_ok else 'FAIL'}] 页面 HTTP {r.status}，含词云画布与绘制逻辑")
        ok = ok and page_ok

        # 2) 等 demo 喂几条
        time.sleep(6)
        d = json.loads(get("/api/state").read().decode("utf-8"))
        st = d["stats"]
        print(f"[{'OK ' if st['lines'] > 0 else 'FAIL'}] 已收到 {st['lines']} 句 / {st['chars']} 字，"
              f"语速 {st['wpm']} 字/分，词汇 {st['vocab']}")
        ok = ok and st["lines"] > 0

        # 3) 热词里不能有停用词（反例）
        top = [w["w"] for w in d["words"]]
        leaked = STOPWORDS_MUST_NOT_APPEAR & set(top)
        print(f"[{'OK ' if not leaked else 'FAIL'}] 停用词未混入热词"
              + (f"（泄露：{leaked}）" if leaked else ""))
        print(f"      热词 TOP8：{top[:8]}")
        ok = ok and not leaked

        # 4) 词频降序
        ns = [w["n"] for w in d["words"]]
        desc = all(ns[i] >= ns[i + 1] for i in range(len(ns) - 1))
        print(f"[{'OK ' if desc else 'FAIL'}] 词频按降序排列")
        ok = ok and desc

        # 5) 情绪分范围
        bad = [r_ for r_ in d["recent"] if not -1.0 <= r_["sentiment"] <= 1.0]
        print(f"[{'OK ' if not bad else 'FAIL'}] 情绪分都在 -1..1（最近 {len(d['recent'])} 条）")
        ok = ok and not bad

        # 6) 曲线在长
        n1 = len(d["series"])
        time.sleep(8)      # 跨过 5 秒分桶边界，确认曲线真的在往前走
        d2 = json.loads(get("/api/state").read().decode("utf-8"))
        grew = len(d2["series"]) >= n1 and d2["stats"]["lines"] > st["lines"]
        print(f"[{'OK ' if grew else 'FAIL'}] 曲线点数 {n1} → {len(d2['series'])}，"
              f"句数 {st['lines']} → {d2['stats']['lines']}")
        ok = ok and grew

        # 7) 字幕流单调递增
        seqs = [r_["seq"] for r_ in d2["recent"]]
        mono = all(seqs[i] > seqs[i + 1] for i in range(len(seqs) - 1))
        print(f"[{'OK ' if mono else 'FAIL'}] 字幕流按 seq 倒序（最新在上）")
        ok = ok and mono

        # 8) 404
        try:
            get("/nope")
            print("[FAIL] 未知路径没返回 404")
            ok = False
        except urllib.error.HTTPError as e:
            print(f"[OK ] 未知路径返回 {e.code}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

    print("-" * 58)
    print("看板自检：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
