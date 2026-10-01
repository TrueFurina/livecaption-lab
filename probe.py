"""
probe.py — 一键体检：你这台机器能不能抓到实时字幕

跑法：
    python probe.py

会依次检查：系统版本 → 有没有装语言包 → 字幕进程在不在 →
UIA 能不能定位窗口 → 能不能读到文本元素。
任何一步不过都会给出明确的下一步动作。
"""

from __future__ import annotations

import ctypes
import platform
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OK = "[OK]"
NO = "[!!]"
INFO = "[--]"


def _section(title: str) -> None:
    print()
    print("=" * 62)
    print(title)
    print("=" * 62)


def check_os() -> None:
    _section("1. 系统版本")
    ver = platform.version()                      # 形如 10.0.26200
    build = int(ver.split(".")[-1]) if ver.split(".")[-1].isdigit() else 0
    print(f"{INFO} platform    : {platform.platform()}")
    print(f"{INFO} build       : {build}")
    if build >= 22000:
        print(f"{OK} 满足实时字幕要求（22H2 / build 22000+）")
    else:
        print(f"{NO} 低于 22000，实时字幕不可用")
    if build >= 26100:
        print(f"{INFO} 24H2+：若本机是 Copilot+ PC（NPU 40+ TOPS）还会有实时翻译")


def check_language_packs() -> None:
    _section("2. 已下载的语音识别语言包")
    # 语言包以 Speech Pack 形式出现在已安装应用里，PowerShell 查询最稳
    ps = (
        "Get-AppxPackage | Where-Object { $_.Name -like '*Speech*' "
        "-or $_.Name -like '*Language*Speech*' } | "
        "Select-Object Name | Format-Table -AutoSize | Out-String"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=60,
        )
        text = out.stdout.decode("utf-8", errors="replace").strip()
        text = text or out.stdout.decode("gbk", errors="replace").strip()
        if text:
            print(text)
        else:
            print(f"{INFO} 未查到（不代表没有，可能尚未下载语言包）")
    except Exception as e:
        print(f"{INFO} 查询失败：{e}")
    print(f"{INFO} 缺语言包？设置 → 时间和语言 → 语言和区域 → 添加语言并勾选语音识别")


def check_process() -> bool:
    _section("3. 字幕进程")
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq LiveCaptions.exe"],
            capture_output=True,
            timeout=30,
        )
        text = out.stdout.decode("gbk", errors="replace")
    except Exception as e:
        print(f"{INFO} 查询失败：{e}")
        return False
    if "LiveCaptions.exe" in text:
        pid_line = [l for l in text.splitlines() if "LiveCaptions.exe" in l]
        print(f"{OK} LiveCaptions.exe 正在运行")
        for l in pid_line:
            print(f"{INFO} {l.strip()}")
        return True
    print(f"{NO} LiveCaptions.exe 未运行")
    print(f"{INFO} 请按 Win + Ctrl + L 打开实时字幕，然后重跑本脚本")
    return False


def check_uia() -> None:
    _section("4. UI Automation 读取")
    try:
        import uiautomation as auto
    except ImportError:
        print(f"{NO} 缺少依赖，请执行：pip install uiautomation")
        return
    print(f"{OK} uiautomation 已安装")

    from caption_source import (
        LC_WINDOW_CLASS,
        ID_CAPTIONS,
        ID_READY,
        find_caption_window,
        _find_text_element,
        _element_text,
    )

    win = find_caption_window()
    if win is None:
        print(f"{NO} 找不到 ClassName={LC_WINDOW_CLASS} 的窗口")
        print(f"{INFO} 确认实时字幕窗口已打开（Win + Ctrl + L）")
        return
    print(f"{OK} 定位到字幕窗口：{win.Name!r}")

    el = _find_text_element(win)
    if el is None:
        print(f"{NO} 窗口内找不到文本元素（{ID_CAPTIONS} / {ID_READY}）")
        return
    print(f"{OK} 定位到文本元素：AutomationId={el.AutomationId!r}")

    text = _element_text(el)
    if not text:
        print(f"{INFO} 当前文本为空 —— 放一段带人声的音频再试")
    else:
        print(f"{OK} 读到文本：")
        for line in text.splitlines()[:8]:
            print(f"    | {line}")


def check_dpi() -> None:
    _section("5. 影响 UIA 稳定性的系统设置")
    try:
        aware = ctypes.windll.shcore.GetProcessDpiAwareness(0)
        print(f"{INFO} 当前进程 DPI 感知：{aware}")
    except Exception:
        print(f"{INFO} DPI 感知查询不可用（不影响抓取）")
    print(f"{INFO} 字幕窗口最小化 = 仍能读取；移到屏幕外 = 读取中断")


def main() -> None:
    print("Windows 11 实时字幕抓取环境体检")
    check_os()
    check_language_packs()
    running = check_process()
    if running:
        check_uia()
        check_dpi()
    _section("结论")
    print("下一步：确认字幕窗口开着，跑 python demo_console.py 看字幕流")


if __name__ == "__main__":
    main()
