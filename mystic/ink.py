"""Shared terminal rendering primitives for CLI and live display output."""

from __future__ import annotations

import os
import re
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
CYAN = "\x1b[36m"
GRAY = "\x1b[90m"


def bold(text: str) -> str:
    return _colorize(text, BOLD)


def dim(text: str) -> str:
    return _colorize(text, DIM)


def red(text: str) -> str:
    return _colorize(text, RED)


def green(text: str) -> str:
    return _colorize(text, GREEN)


def yellow(text: str) -> str:
    return _colorize(text, YELLOW)


def cyan(text: str) -> str:
    return _colorize(text, CYAN)


def gray(text: str) -> str:
    return _colorize(text, GRAY)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def status_icon(status: str) -> str:
    labels = {
        "running": green("[up]"),
        "up": green("[up]"),
        "stopped": red("[down]"),
        "down": red("[down]"),
        "pending": yellow("[wait]"),
        "wait": yellow("[wait]"),
        "in_progress": cyan("[work]"),
        "work": cyan("[work]"),
        "completed": green("[ok]"),
        "ok": green("[ok]"),
        "failed": red("[fail]"),
        "fail": red("[fail]"),
        "cancelled": gray("[x]"),
        "x": gray("[x]"),
    }
    return labels.get(status, "[?]")


def heading(text: str) -> str:
    return f"\n{bold(text)}"


def error_msg(text: str) -> str:
    return f"{red('Error:')} {text}"


def format_duration(ms: int) -> str:
    total_seconds = max(ms // 1000, 0)
    if total_seconds < 60:
        return f"{total_seconds}s"
    total_minutes, seconds = divmod(total_seconds, 60)
    if total_minutes < 60:
        return f"{total_minutes}m {seconds}s"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


def format_phone(phone: str | None) -> str:
    if not phone:
        return ""
    if phone.startswith("+1") and len(phone) == 12 and phone[2:].isdigit():
        return f"+1 ({phone[2:5]}) {phone[5:8]}-{phone[8:12]}"
    return phone


def section(label: str) -> str:
    dash = "─" if _supports_unicode() else "-"
    prefix = "──" if _supports_unicode() else "--"
    return dim(f"\n{prefix} {label} {dash * max(1, 38 - len(strip_ansi(label)))}")


def box(lines: list[str], width: int | None = None) -> str:
    unicode_ok = _supports_unicode()
    horiz = "─" if unicode_ok else "-"
    vert = "│" if unicode_ok else "|"
    tl = "╭" if unicode_ok else "+"
    tr = "╮" if unicode_ok else "+"
    bl = "╰" if unicode_ok else "+"
    br = "╯" if unicode_ok else "+"
    inner = max((len(strip_ansi(l)) for l in lines), default=0)
    inner = max(inner, width or 0)
    top = tl + (horiz * (inner + 2)) + tr
    bottom = bl + (horiz * (inner + 2)) + br
    body = [f"{vert} {l}{' ' * (inner - len(strip_ansi(l)))} {vert}" for l in lines]
    return "\n".join([top, *body, bottom])


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(sys.stdout, "isatty", None)
    return bool(callable(isatty) and isatty())


def _supports_unicode() -> bool:
    encoding = getattr(sys.stdout, "encoding", "") or ""
    return "utf" in encoding.lower()


def _colorize(text: str, code: str) -> str:
    if not _supports_color():
        return text
    return f"{code}{text}{RESET}"
