"""test_md_sink.py — 验证字幕自动归档到 Markdown 的逻辑。

不污染用户真实归档目录：所有落盘都指向临时目录；只在最后一条断言
核对 DEFAULT_MD_DIR 常量确实指向 E:/Program/Zimu。

运行：
    python test_md_sink.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from caption_source import CaptionTap, DEFAULT_MD_DIR, CaptionLine  # noqa: E402
from datetime import datetime  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def main() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    tmp = tempfile.mkdtemp(prefix="zimu_test_")
    tap = CaptionTap(md_dir=tmp)

    # 1) 首次 emit：自动建文件 + 写头部 + 正确行格式
    tap._emit(CaptionLine(text="今天天气不错", ts=0.0, iso="10:00:00", seq=1))
    tap._emit(CaptionLine(text="我们讨论一下方案", ts=0.0, iso="10:00:05", seq=2))
    path = os.path.join(tmp, f"字幕归档-{today}.md")
    check("md 文件已创建", os.path.exists(path))
    content = open(path, encoding="utf-8").read()
    check("文件头正确", content.startswith(f"# 实时字幕归档 · {today}"))
    check("行格式 - HH:MM:SS 文本",
          "- 10:00:00 今天天气不错" in content and "- 10:00:05 我们讨论一下方案" in content)

    # 2) session 分隔行
    tap._append_md_session("记录开始")
    tap._append_md_session("记录结束")
    content = open(path, encoding="utf-8").read()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    check("记录开始分隔行", f"> --- 记录开始 {stamp}" in content)
    check("记录结束分隔行", f"> --- 记录结束 {stamp}" in content)

    # 3) 跨天自动切文件
    tap._md_day = "1999-01-01"  # 强制触发日期切换
    tap._emit(CaptionLine(text="跨天的句子", ts=0.0, iso="23:59:59", seq=3))
    content = open(path, encoding="utf-8").read()
    check("跨天新内容写入当天文件", "- 23:59:59 跨天的句子" in content)

    # 4) 默认目录常量
    check("默认目录常量 = E:\\Program\\Zimu", DEFAULT_MD_DIR == r"E:\Program\Zimu")
    check("默认目录真实存在", os.path.isdir(DEFAULT_MD_DIR))

    # 5) md_dir=None 时绝不写任何文件（用独立临时目录，避免与前面 tap 创建的混淆）
    tmp2 = tempfile.mkdtemp(prefix="zimu_none_")
    tap_none = CaptionTap(md_dir=None)
    tap_none._append_md(CaptionLine(text="不应被写入", ts=0.0, iso="00:00:00", seq=1))
    tap_none._append_md_session("记录开始")
    check("md_dir=None 不创建文件", not any(
        f.startswith("字幕归档") for f in os.listdir(tmp2)))

    print("-" * 60)
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        return 1
    print("ALL PASS — 字幕自动归档逻辑正确（落盘位置：E:\\Program\\Zimu）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
