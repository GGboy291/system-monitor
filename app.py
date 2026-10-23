# -*- coding: utf-8 -*-
"""桌面系统监控面板：CPU / GPU / 内存 / 网络 / 磁盘 / 进程实时可视化。"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime

from stats import SystemSampler
from tray import (ActivationListener, TrayIcon, acquire_single_instance,
                  is_autostart_enabled, set_autostart, signal_running_instance)

# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------
BG = "#070b11"
PANEL = "#0d131d"
PANEL_ALT = "#141c28"
BORDER = "#1c2634"
GRID = "#1a2432"
TEXT = "#e6edf7"
MUTED = "#7b8a9f"
DIM = "#48586d"

C_CPU = "#22d3ee"
C_GPU = "#a78bfa"
C_MEM = "#34d399"
C_NET_D = "#fbbf24"
C_NET_U = "#f472b6"
C_DISK_R = "#60a5fa"
C_DISK_W = "#818cf8"
C_VOL = "#fb7185"

FONT = "Microsoft YaHei UI"
NUM = "Segoe UI"

SAMPLE_INTERVAL = 0.5
HISTORY = 120  # 0.5s * 120 = 60 秒

FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = (getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
            if FROZEN else os.path.dirname(os.path.abspath(__file__)))
ENTRY_SCRIPT = sys.executable if FROZEN else os.path.join(BASE_DIR, "app.py")
ICON_PATH = os.path.join(BASE_DIR, "icon.ico")
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                          "SystemMonitor")
CONFIG_PATH = os.path.join(CONFIG_DIR, "settings.json")

FLOAT_W, FLOAT_H = 300, 186


def load_settings() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def fmt_bytes(value: float, per_second: bool = False) -> tuple[str, str]:
    """返回 (数值, 单位)，单位独立显示，便于大字号排版。"""
    suffix = "/s" if per_second else ""
    value = max(0.0, float(value))
    units = ["B", "KB", "MB", "GB", "TB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)}", units[0] + suffix
    text = f"{value:.1f}" if value < 100 else f"{value:.0f}"
    return text, units[idx] + suffix


def fmt_total(value: float) -> str:
    num, unit = fmt_bytes(value)
    return f"{num} {unit}"


def fmt_uptime(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}:{rem % 60:02d}"


def blend(color: str, other: str, ratio: float) -> str:
    """把 color 按比例混入 other（ratio=0 全为 other）。"""
    c1 = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    c2 = tuple(int(other[i:i + 2], 16) for i in (1, 3, 5))
    mix = tuple(int(a * ratio + b * (1 - ratio)) for a, b in zip(c1, c2))
    return "#%02x%02x%02x" % mix


def nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 2, 5, 10):
        if value <= mult * base:
            return mult * base
    return 10 * base


# --------------------------------------------------------------------------
# 基础控件
# --------------------------------------------------------------------------
class Chart(tk.Canvas):
    """多序列面积折线图，固定 60 秒窗口，右侧为最新数据。"""

    def __init__(self, master, colors: dict, fixed_max: float | None = None,
                 height: int = 60, bg: str = PANEL):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, height=height)
        self._series = {name: deque(maxlen=HISTORY) for name in colors}
        self._colors = colors
        self._fixed_max = fixed_max
        self._scale = fixed_max or 1.0
        self.bind("<Configure>", lambda _e: self.redraw())

    def push(self, values: dict):
        for name, series in self._series.items():
            series.append(max(0.0, float(values.get(name, 0.0))))
        self.redraw()

    def redraw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 12 or height < 12:
            return

        top, bottom = 6, 2
        plot_h = height - top - bottom
        if plot_h <= 4:
            return

        for frac in (0.25, 0.5, 0.75):
            y = top + plot_h * frac
            self.create_line(0, y, width, y, fill=GRID, dash=(2, 5))
        self.create_line(0, top + plot_h, width, top + plot_h, fill="#243140")

        peak = max((max(s) if s else 0.0) for s in self._series.values())
        if self._fixed_max:
            self._scale = self._fixed_max
        else:
            if peak > self._scale or peak < self._scale * 0.35:
                self._scale = nice_max(peak * 1.2) if peak > 0 else 1.0

        scale = max(self._scale, 1e-9)
        slot = width / max(HISTORY - 1, 1)

        for name, series in self._series.items():
            if not series:
                continue
            color = self._colors[name]
            offset = HISTORY - len(series)
            points = []
            for i, value in enumerate(series):
                x = (offset + i) * slot
                y = top + plot_h * (1 - min(value / scale, 1.0))
                points.extend((x, y))
            if len(points) >= 4:
                polygon = points + [points[-2], top + plot_h, points[0], top + plot_h]
                self.create_polygon(*polygon, fill=blend(color, PANEL, 0.16), outline="")
                self.create_line(*points, fill=color, width=2,
                                 capstyle="round", joinstyle="round")
                self.create_oval(points[-2] - 2.5, points[-1] - 2.5,
                                 points[-2] + 2.5, points[-1] + 2.5,
                                 fill=color, outline="")


class Bar(tk.Canvas):
    """细进度条。"""

    def __init__(self, master, color: str, height: int = 4, bg: str = PANEL_ALT):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, height=height)
        self._color = color
        self._ratio = 0.0
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, percent: float):
        self._ratio = max(0.0, min(1.0, percent / 100.0))
        self._draw()

    def _draw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 2:
            return
        self.create_rectangle(0, 0, width, height, fill=PANEL_ALT, outline="")
        fill_w = width * self._ratio
        if fill_w > 0:
            self.create_rectangle(0, 0, max(fill_w, 2), height, fill=self._color, outline="")


class CoreStrip(tk.Canvas):
    """CPU 各逻辑核心占用热力条。"""

    def __init__(self, master, color: str, height: int = 10):
        super().__init__(master, bg=PANEL, highlightthickness=0, bd=0, height=height)
        self._color = color
        self._values: list[float] = []
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, values: list[float]):
        self._values = list(values)
        self._draw()

    def _draw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        count = len(self._values)
        if width < 10 or count == 0:
            return
        gap = 2
        bar_w = max(2.0, (width - gap * (count - 1)) / count)
        for i, value in enumerate(self._values):
            intensity = min(1.0, max(0.0, value / 100.0)) ** 0.6
            fill = blend(self._color, "#172230", 0.12 + 0.88 * intensity)
            x0 = i * (bar_w + gap)
            self.create_rectangle(x0, 0, x0 + bar_w, height, fill=fill, outline="")


class Card(tk.Frame):
    """统一卡片容器：左侧色条 + 标题 + 右侧副标题。"""

    def __init__(self, master, title: str, accent: str, subtitle: str = ""):
        super().__init__(master, bg=PANEL, highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=BORDER)
        head = tk.Frame(self, bg=PANEL)
        head.pack(fill="x", padx=15, pady=(11, 0))
        tk.Frame(head, bg=accent, width=3, height=13).pack(side="left", pady=1)
        tk.Label(head, text=title, bg=PANEL, fg=TEXT,
                 font=(FONT, 10, "bold")).pack(side="left", padx=(8, 0))
        self.subtitle = tk.Label(head, text=subtitle, bg=PANEL, fg=DIM, font=(FONT, 8))
        self.subtitle.pack(side="right")
        self.body = tk.Frame(self, bg=PANEL)
        self.body.pack(fill="both", expand=True, padx=15, pady=(6, 12))


def metric(parent, color: str, unit: str, size: int = 30, bg: str = PANEL):
    """大号数值 + 单位，返回 (容器, 数值Label, 单位Label, 附注Label)。"""
    wrap = tk.Frame(parent, bg=bg)
    row = tk.Frame(wrap, bg=bg)
    row.pack(anchor="w")
    value = tk.Label(row, text="--", bg=bg, fg=color, font=(NUM, size, "bold"))
    value.pack(side="left")
    unit_label = tk.Label(row, text=unit, bg=bg, fg=MUTED, font=(FONT, 8))
    unit_label.pack(side="left", anchor="s", pady=(0, size // 4))
    detail = tk.Label(wrap, text="", bg=bg, fg=DIM, font=(FONT, 8))
    detail.pack(anchor="w")
    return wrap, value, unit_label, detail


def small_label(parent, text: str = "", color: str = DIM, bg: str = PANEL):
    return tk.Label(parent, text=text, bg=bg, fg=color, font=(FONT, 8))


# --------------------------------------------------------------------------
# 各功能卡片
# --------------------------------------------------------------------------
class CpuCard(Card):
    def __init__(self, master):
        super().__init__(master, "处理器 CPU", C_CPU, "60 秒曲线")
        top = tk.Frame(self.body, bg=PANEL)
        top.pack(fill="x")
        wrap, self.value, self.unit, self.detail = metric(top, C_CPU, "%", 32)
        wrap.pack(side="left")
        right = tk.Frame(top, bg=PANEL)
        right.pack(side="right", anchor="ne")
        self.freq = tk.Label(right, text="-- GHz", bg=PANEL, fg=MUTED,
                             font=(NUM, 11, "bold"))
        self.freq.pack(anchor="e")
        small_label(right, "当前频率").pack(anchor="e")

        self.chart = Chart(self.body, {"v": C_CPU}, fixed_max=100, height=52)
        self.chart.pack(fill="both", expand=True, pady=(4, 6))

        self.cores = CoreStrip(self.body, C_CPU, height=10)
        self.cores.pack(fill="x", pady=(0, 6))

        footer = tk.Frame(self.body, bg=PANEL)
        footer.pack(fill="x")
        self.footer = small_label(footer)
        self.footer.pack(side="left")
        self.cores_label = small_label(footer, "逻辑核心")
        self.cores_label.pack(side="right")

    def refresh(self, data: dict):
        cpu = data["cpu"]
        self.value.configure(text=f"{cpu['total']:.0f}")
        self.detail.configure(text=f"{cpu['phys']} 核 {cpu['threads']} 线程")
        self.freq.configure(text=f"{cpu['freq']:.2f} GHz" if cpu["freq"] else "-- GHz")
        self.footer.configure(text=f"{cpu['procs']} 个进程运行中")
        self.chart.push({"v": cpu["total"]})
        self.cores.set(cpu["cores"])


class GpuCard(Card):
    def __init__(self, master):
        super().__init__(master, "显卡 GPU", C_GPU, "")
        top = tk.Frame(self.body, bg=PANEL)
        top.pack(fill="x")
        wrap, self.value, self.unit, self.detail = metric(top, C_GPU, "%", 32)
        wrap.pack(side="left")
        right = tk.Frame(top, bg=PANEL)
        right.pack(side="right", anchor="ne")
        self.vram = tk.Label(right, text="-- / --", bg=PANEL, fg=MUTED,
                             font=(NUM, 11, "bold"))
        self.vram.pack(anchor="e")
        small_label(right, "显存占用").pack(anchor="e")

        self.chart = Chart(self.body, {"v": C_GPU}, fixed_max=100, height=52)
        self.chart.pack(fill="both", expand=True, pady=(4, 6))

        self.vram_bar = Bar(self.body, C_GPU, height=4)
        self.vram_bar.pack(fill="x", pady=(2, 6))

        footer = tk.Frame(self.body, bg=PANEL)
        footer.pack(fill="x")
        self.engines = small_label(footer)
        self.engines.pack(side="left")
        self.vram_pct = small_label(footer)
        self.vram_pct.pack(side="right")

    def refresh(self, gpu: dict):
        self.subtitle.configure(text=gpu["name"][:30])
        if not gpu.get("available"):
            self.value.configure(text="N/A")
            self.engines.configure(text="GPU 计数器不可用")
            return
        self.value.configure(text=f"{gpu['util']:.0f}")
        used, total = gpu["vram_used"], gpu["vram_total"]
        self.vram.configure(text=f"{fmt_total(used)} / {fmt_total(total)}")
        pct = (used / total * 100.0) if total else 0.0
        self.vram_bar.set(pct)
        self.vram_pct.configure(text=f"显存 {pct:.0f}%")
        types = gpu.get("types") or []
        self.engines.configure(text="  ·  ".join(f"{name} {val:.0f}%" for name, val in types)
                               or "引擎空闲")
        self.chart.push({"v": gpu["util"]})


class MemCard(Card):
    def __init__(self, master):
        super().__init__(master, "内存 MEMORY", C_MEM, "")
        top = tk.Frame(self.body, bg=PANEL)
        top.pack(fill="x")
        wrap, self.value, self.unit, self.detail = metric(top, C_MEM, "%", 32)
        wrap.pack(side="left")
        right = tk.Frame(top, bg=PANEL)
        right.pack(side="right", anchor="ne")
        self.used = tk.Label(right, text="-- / --", bg=PANEL, fg=MUTED,
                             font=(NUM, 11, "bold"))
        self.used.pack(anchor="e")
        small_label(right, "物理内存").pack(anchor="e")

        self.chart = Chart(self.body, {"v": C_MEM}, fixed_max=100, height=52)
        self.chart.pack(fill="both", expand=True, pady=(4, 6))

        swap_head = tk.Frame(self.body, bg=PANEL)
        swap_head.pack(fill="x")
        small_label(swap_head, "交换分区").pack(side="left")
        self.swap_text = small_label(swap_head)
        self.swap_text.pack(side="right")

        self.swap_bar = Bar(self.body, "#6ee7b7", height=4)
        self.swap_bar.pack(fill="x", pady=(4, 6))

        self.footer = small_label(self.body)
        self.footer.pack(anchor="w")

    def refresh(self, data: dict):
        mem = data["mem"]
        self.value.configure(text=f"{mem['percent']:.0f}")
        self.detail.configure(text=f"可用 {fmt_total(mem['total'] - mem['used'])}")
        self.used.configure(text=f"{fmt_total(mem['used'])} / {fmt_total(mem['total'])}")
        self.chart.push({"v": mem["percent"]})
        self.swap_bar.set(mem["swap_percent"])
        self.swap_text.configure(
            text=f"{fmt_total(mem['swap_used'])} / {fmt_total(mem['swap_total'])}")
        self.footer.configure(text=f"总计 {fmt_total(mem['total'])} · "
                                   f"交换 {mem['swap_percent']:.0f}%")


class NetCard(Card):
    def __init__(self, master):
        super().__init__(master, "网络 NETWORK", C_NET_D, "下载 / 上传")
        top = tk.Frame(self.body, bg=PANEL)
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)

        down_wrap, self.down, self.down_unit, _ = metric(top, C_NET_D, "/s", 26)
        down_wrap.grid(row=0, column=0, sticky="w")
        small_label(down_wrap, "下载速度", C_NET_D).pack(anchor="w")

        up_wrap, self.up, self.up_unit, _ = metric(top, C_NET_U, "/s", 26)
        up_wrap.grid(row=0, column=1, sticky="w")
        small_label(up_wrap, "上传速度", C_NET_U).pack(anchor="w")

        self.chart = Chart(self.body, {"down": C_NET_D, "up": C_NET_U}, height=48)
        self.chart.pack(fill="both", expand=True, pady=(6, 6))

        self.footer = small_label(self.body)
        self.footer.pack(anchor="w")

    def refresh(self, data: dict):
        net = data["net"]
        d_num, d_unit = fmt_bytes(net["down"], per_second=True)
        u_num, u_unit = fmt_bytes(net["up"], per_second=True)
        self.down.configure(text=d_num)
        self.down_unit.configure(text=d_unit)
        self.up.configure(text=u_num)
        self.up_unit.configure(text=u_unit)
        self.chart.push({"down": net["down"], "up": net["up"]})
        total = data["meta"]["net_total"]
        self.footer.configure(
            text=f"本次 ↓ {fmt_total(total[0])}   ↑ {fmt_total(total[1])}")


class DiskCard(Card):
    def __init__(self, master):
        super().__init__(master, "磁盘读写", C_DISK_R, "累计 IO")
        top = tk.Frame(self.body, bg=PANEL)
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)
        top.grid_columnconfigure(1, weight=1)

        r_wrap, self.read, self.read_unit, _ = metric(top, C_DISK_R, "/s", 26)
        r_wrap.grid(row=0, column=0, sticky="w")
        small_label(r_wrap, "读取", C_DISK_R).pack(anchor="w")

        w_wrap, self.write, self.write_unit, _ = metric(top, C_DISK_W, "/s", 26)
        w_wrap.grid(row=0, column=1, sticky="w")
        small_label(w_wrap, "写入", C_DISK_W).pack(anchor="w")

        self.chart = Chart(self.body, {"read": C_DISK_R, "write": C_DISK_W}, height=48)
        self.chart.pack(fill="both", expand=True, pady=(6, 6))

        self.footer = small_label(self.body)
        self.footer.pack(anchor="w")

    def refresh(self, data: dict):
        disk = data["disk"]
        r_num, r_unit = fmt_bytes(disk["read"], per_second=True)
        w_num, w_unit = fmt_bytes(disk["write"], per_second=True)
        self.read.configure(text=r_num)
        self.read_unit.configure(text=r_unit)
        self.write.configure(text=w_num)
        self.write_unit.configure(text=w_unit)
        self.chart.push({"read": disk["read"], "write": disk["write"]})
        total = data["meta"]["disk_total"]
        self.footer.configure(
            text=f"本次 读 {fmt_total(total[0])}   写 {fmt_total(total[1])}")


class VolumeCard(Card):
    def __init__(self, master):
        super().__init__(master, "磁盘空间", C_VOL, "")
        self.rows = []
        for i in range(4):
            row = tk.Frame(self.body, bg=PANEL)
            row.pack(fill="x", pady=3)
            row.grid_columnconfigure(1, weight=1)
            mount = tk.Label(row, text="--", bg=PANEL, fg=TEXT,
                             font=(FONT, 9, "bold"), width=3, anchor="w")
            mount.grid(row=0, column=0, sticky="w")
            text = tk.Label(row, text="", bg=PANEL, fg=MUTED,
                            font=(FONT, 8), anchor="e")
            text.grid(row=0, column=2, sticky="e")
            bar = Bar(row, C_VOL, height=5)
            bar.grid(row=0, column=1, sticky="ew", padx=8)
            self.rows.append({"frame": row, "mount": mount, "bar": bar, "text": text})
        self.footer = small_label(self.body)
        self.footer.pack(anchor="w", pady=(6, 0))

    def refresh(self, volumes: list):
        for i, row in enumerate(self.rows):
            if i >= len(volumes):
                row["frame"].pack_forget()
                continue
            if not row["frame"].winfo_ismapped():
                row["frame"].pack(fill="x", pady=3, before=self.footer)
            vol = volumes[i]
            row["mount"].configure(text=vol["mount"])
            row["bar"]._color = C_VOL if vol["percent"] < 85 else "#f97316"
            row["bar"].set(vol["percent"])
            used_num, _ = fmt_bytes(vol["used"])
            total_num, total_unit = fmt_bytes(vol["total"])
            row["text"].configure(
                text=f"{used_num}/{total_num} {total_unit} · {vol['percent']:.0f}%")
        self.footer.configure(text=f"共 {len(volumes)} 个分区 · 数值为已用/总量")


class ProcessCard(Card):
    ROWS = 7

    def __init__(self, master):
        super().__init__(master, "进程排行", "#94a3b8", "按 CPU 占用排序")
        header = tk.Frame(self.body, bg=PANEL)
        header.pack(fill="x", pady=(0, 2))
        tk.Label(header, text="进程", bg=PANEL, fg=DIM, font=(FONT, 8)).pack(side="left")
        tk.Label(header, text="内存", bg=PANEL, fg=DIM, font=(FONT, 8),
                 width=9, anchor="e").pack(side="right")
        tk.Label(header, text="CPU", bg=PANEL, fg=DIM, font=(FONT, 8),
                 width=8, anchor="e").pack(side="right", padx=(0, 10))

        self.rows = []
        for _ in range(self.ROWS):
            row = tk.Frame(self.body, bg=PANEL)
            row.pack(fill="x", pady=1)
            name = tk.Label(row, text="", bg=PANEL, fg=TEXT, font=(FONT, 8),
                            anchor="w")
            name.pack(side="left")
            mem = tk.Label(row, text="", bg=PANEL, fg=MUTED, font=(NUM, 8),
                           width=9, anchor="e")
            mem.pack(side="right")
            cpu = tk.Label(row, text="", bg=PANEL, fg=MUTED, font=(NUM, 8),
                           width=8, anchor="e")
            cpu.pack(side="right", padx=(0, 10))
            self.rows.append({"frame": row, "name": name, "cpu": cpu, "mem": mem})

    def refresh(self, procs: list):
        for i, row in enumerate(self.rows):
            if i >= len(procs):
                row["name"].configure(text="")
                row["cpu"].configure(text="")
                row["mem"].configure(text="")
                continue
            proc = procs[i]
            name = proc["name"]
            if len(name) > 26:
                name = name[:25] + "…"
            row["name"].configure(text=name)
            row["cpu"].configure(text=f"{proc['cpu']:.0f}%" if proc["cpu"] >= 1
                                 else f"{proc['cpu']:.1f}%",
                                 fg=TEXT if proc["cpu"] >= 10 else MUTED)
            row["mem"].configure(text=f"{proc['mem']:.0f} MB" if proc["mem"] < 1024
                                 else f"{proc['mem'] / 1024:.1f} GB")


class InfoCard(Card):
    def __init__(self, master):
        super().__init__(master, "系统信息", "#64748b", "")
        self.items = {}
        for key in ("运行时间", "主机名", "操作系统", "处理器", "显卡", "内存"):
            row = tk.Frame(self.body, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=key, bg=PANEL, fg=DIM, font=(FONT, 8),
                     width=7, anchor="w").pack(side="left")
            value = tk.Label(row, text="--", bg=PANEL, fg=TEXT, font=(FONT, 8),
                             anchor="w", justify="left")
            value.pack(side="left", fill="x", expand=True)
            self.items[key] = value

        self.footer = small_label(self.body)
        self.footer.pack(anchor="w", pady=(8, 0))
        self.footer.configure(text=f"每 {SAMPLE_INTERVAL:.1f} 秒刷新 · 曲线窗口 60 秒")

    def refresh(self, data: dict):
        meta = data["meta"]
        cpu = data["cpu"]
        gpu = data["gpu"]
        mem = data["mem"]
        self.items["运行时间"].configure(text=fmt_uptime(meta["uptime"]))
        self.items["主机名"].configure(text=meta["host"])
        self.items["操作系统"].configure(text=meta["os"])
        self.items["处理器"].configure(text=f"{cpu['name']}（{cpu['phys']}C/{cpu['threads']}T）")
        vram = f" · {fmt_bytes(gpu['vram_total'])[0]} {fmt_bytes(gpu['vram_total'])[1]}" \
            if gpu.get("vram_total") else ""
        self.items["显卡"].configure(text=f"{gpu['name']}{vram}")
        self.items["内存"].configure(text=fmt_total(mem["total"]))


# --------------------------------------------------------------------------
# 悬浮迷你窗
# --------------------------------------------------------------------------
class MiniChart(tk.Canvas):
    def __init__(self, master, color: str, bg: str = PANEL):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, height=22)
        self._color = color
        self._series = deque(maxlen=HISTORY)
        self.bind("<Configure>", lambda _e: self._draw())

    def push(self, value: float):
        self._series.append(max(0.0, float(value)))
        self._draw()

    def _draw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        if width < 8 or height < 6 or len(self._series) < 2:
            return
        slot = width / (HISTORY - 1)
        offset = HISTORY - len(self._series)
        points = []
        for i, value in enumerate(self._series):
            x = (offset + i) * slot
            y = (height - 2) - (height - 4) * min(value / 100.0, 1.0)
            points.extend((x, y))
        self.create_polygon(*(points + [points[-2], height, points[0], height]),
                            fill=blend(self._color, PANEL, 0.22), outline="")
        self.create_line(*points, fill=self._color, width=1.5)


class FloatingMonitor(tk.Toplevel):
    """桌面悬浮迷你监控条：可拖动、置顶、双击打开主界面。"""

    def __init__(self, app: "MonitorApp"):
        super().__init__(app)
        self.app = app
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=PANEL)
        self.withdraw()
        self._visible = False
        self._drag_offset = None
        self._build()

        x = app.settings.get("float_x")
        y = app.settings.get("float_y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            x = self.winfo_screenwidth() - FLOAT_W - 28
            y = 64
        x = max(0, min(int(x), self.winfo_screenwidth() - FLOAT_W))
        y = max(0, min(int(y), self.winfo_screenheight() - FLOAT_H))
        self.geometry(f"{FLOAT_W}x{FLOAT_H}+{x}+{y}")
        self._apply_native_corners()

    def _apply_native_corners(self):
        """用 Win11 原生圆角：与主界面同半径，系统抗锯齿，没有毛边。"""
        try:
            self.update_idletasks()
            hwnd = int(self.wm_frame(), 16)
        except (tk.TclError, ValueError):
            return
        try:
            dwm = ctypes.windll.dwmapi
        except (AttributeError, OSError):
            return
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                              ctypes.c_void_p, ctypes.c_uint]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33,
                                  ctypes.byref(pref), ctypes.sizeof(pref))
        border = ctypes.c_uint(0x0034261C)  # COLORREF：#1c2634
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 34,
                                  ctypes.byref(border), ctypes.sizeof(border))

    @property
    def visible(self) -> bool:
        return self._visible

    def _build(self):
        background = tk.Canvas(self, width=FLOAT_W, height=FLOAT_H,
                               bg=PANEL, highlightthickness=0, bd=0)
        background.place(x=0, y=0)
        background.create_line(14, 148, FLOAT_W - 14, 148, fill=BORDER)
        self._bind_motion(background)

        dot = tk.Label(self, text="●", bg=PANEL, fg=C_CPU, font=(NUM, 7))
        dot.place(x=12, y=12)
        title = tk.Label(self, text="系统监控", bg=PANEL, fg=TEXT,
                         font=(FONT, 9, "bold"))
        title.place(x=26, y=10)
        self.clock = tk.Label(self, text="", bg=PANEL, fg=DIM, font=(NUM, 8))
        self.clock.place(x=FLOAT_W - 108, y=12, width=46)
        close = tk.Label(self, text="×", bg=PANEL, fg=MUTED, font=(NUM, 12),
                         cursor="hand2")
        close.place(x=FLOAT_W - 27, y=5)
        close.bind("<Button-1>", self._on_close_click)
        for widget in (dot, title):
            self._bind_motion(widget)

        self.rows = {}
        y = 42
        for key, label, color in (("cpu", "CPU", C_CPU), ("gpu", "GPU", C_GPU),
                                  ("mem", "内存", C_MEM)):
            name = tk.Label(self, text=label, bg=PANEL, fg=MUTED,
                            font=(FONT, 8), anchor="w")
            name.place(x=14, y=y, width=40)
            value = tk.Label(self, text="--", bg=PANEL, fg=color,
                             font=(NUM, 10, "bold"), anchor="e")
            value.place(x=54, y=y - 2, width=54)
            bar = Bar(self, color, height=4)
            bar.place(x=116, y=y + 8, width=170)
            self.rows[key] = (value, bar)
            self._bind_motion(name)
            self._bind_motion(value)
            y += 26

        self.net_down = tk.Label(self, text="↓ --", bg=PANEL, fg=C_NET_D,
                                 font=(NUM, 8, "bold"), anchor="w")
        self.net_down.place(x=14, y=121, width=136)
        self.net_up = tk.Label(self, text="↑ --", bg=PANEL, fg=C_NET_U,
                               font=(NUM, 8, "bold"), anchor="w")
        self.net_up.place(x=154, y=121, width=132)
        for widget in (self.net_down, self.net_up):
            self._bind_motion(widget)

        self.spark = MiniChart(self, C_CPU, bg=PANEL)
        self.spark.place(x=14, y=152, width=FLOAT_W - 28, height=24)
        self._bind_motion(self.spark)

    def _bind_motion(self, widget):
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)
        widget.bind("<ButtonRelease-1>", self._drag_end)
        widget.bind("<Double-Button-1>", lambda _e: self.app.show_main())

    def _drag_start(self, event):
        self._drag_offset = (event.x_root - self.winfo_x(),
                             event.y_root - self.winfo_y())

    def _drag_move(self, event):
        if not self._drag_offset:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        x = max(0, min(x, self.winfo_screenwidth() - FLOAT_W))
        y = max(0, min(y, self.winfo_screenheight() - FLOAT_H))
        self.geometry(f"+{x}+{y}")

    def _drag_end(self, _event):
        self._drag_offset = None
        self.app.settings["float_x"] = self.winfo_x()
        self.app.settings["float_y"] = self.winfo_y()
        self.app.save_settings()

    def _on_close_click(self, _event):
        self.app.toggle_float(False)
        return "break"

    def show(self):
        self.deiconify()
        self.attributes("-topmost", True)
        self.lift()
        self._visible = True

    def hide(self):
        self.withdraw()
        self._visible = False

    def refresh(self, data: dict):
        if not self._visible:
            return
        gpu = data["gpu"]
        gpu_util = gpu["util"] if gpu.get("available") else 0.0
        values = {"cpu": data["cpu"]["total"], "gpu": gpu_util,
                  "mem": data["mem"]["percent"]}
        for key, value in values.items():
            label, bar = self.rows[key]
            if key == "gpu" and not gpu.get("available"):
                label.configure(text="N/A")
            else:
                label.configure(text=f"{value:.0f}%")
            bar.set(value)

        down_num, down_unit = fmt_bytes(data["net"]["down"], per_second=True)
        up_num, up_unit = fmt_bytes(data["net"]["up"], per_second=True)
        self.net_down.configure(text=f"↓ {down_num} {down_unit}")
        self.net_up.configure(text=f"↑ {up_num} {up_unit}")
        self.spark.push(data["cpu"]["total"])
        self.clock.configure(text=datetime.now().strftime("%H:%M:%S"))


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class MonitorApp(tk.Tk):
    def __init__(self, settings: dict, startup: bool = False):
        super().__init__()
        self.settings = settings
        self.autostart = is_autostart_enabled()
        self._actions: queue.Queue = queue.Queue()
        self._quitting = False

        self.title("系统监控")
        self.configure(bg=BG)
        try:
            self.iconbitmap(ICON_PATH)
        except tk.TclError:
            pass
        width, height = 1260, 840
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{max(20, (screen_h - height) // 2 - 30)}")
        self.minsize(1120, 760)

        self.sampler = SystemSampler()
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._last_clock = ""

        self._build_header()
        self._build_body()
        self._apply_dark_titlebar()

        self.float = FloatingMonitor(self)
        self.tray = TrayIcon(ICON_PATH, "系统监控", self._tray_action)
        self.tray.start()
        self._sync_tray()
        self._activation = ActivationListener(lambda: self._actions.put("open"))
        self._activation.start()

        self._thread = threading.Thread(target=self._sampler_loop, daemon=True)
        self._thread.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._pump)

        if startup:
            self.withdraw()

    # -- 布局 ------------------------------------------------------------

    def _build_header(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 2))

        mark = tk.Canvas(header, width=26, height=26, bg=BG, highlightthickness=0)
        mark.pack(side="left")
        mark.create_oval(3, 3, 22, 22, outline=blend(C_CPU, BG, 0.35), width=2)
        mark.create_oval(9, 9, 16, 16, fill=C_CPU, outline="")

        title_box = tk.Frame(header, bg=BG)
        title_box.pack(side="left", padx=(8, 0))
        tk.Label(title_box, text="系统监控", bg=BG, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(title_box, text="SYSTEM MONITOR", bg=BG, fg=DIM,
                 font=(NUM, 7)).pack(anchor="w")

        self.pin_btn = tk.Button(header, text="置顶", command=self._toggle_top,
                                 bg=PANEL, fg=MUTED, activebackground=PANEL_ALT,
                                 activeforeground=TEXT, relief="flat", bd=0,
                                 font=(FONT, 8), padx=12, pady=5, cursor="hand2")
        self.pin_btn.pack(side="right")

        self.float_btn = tk.Button(header, text="悬浮窗", command=self.toggle_float,
                                   bg=PANEL, fg=MUTED, activebackground=PANEL_ALT,
                                   activeforeground=TEXT, relief="flat", bd=0,
                                   font=(FONT, 8), padx=12, pady=5, cursor="hand2")
        self.float_btn.pack(side="right", padx=(0, 6))

        clock_box = tk.Frame(header, bg=BG)
        clock_box.pack(side="right", padx=(0, 14))
        self.clock = tk.Label(clock_box, text="", bg=BG, fg=TEXT,
                              font=(NUM, 15, "bold"))
        self.clock.pack(anchor="e")
        self.uptime = tk.Label(clock_box, text="", bg=BG, fg=DIM, font=(FONT, 8))
        self.uptime.pack(anchor="e")

    def _build_body(self):
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(6, 12))
        for col in range(3):
            body.grid_columnconfigure(col, weight=1, uniform="cards")
        body.grid_rowconfigure(0, weight=30, minsize=250)
        body.grid_rowconfigure(1, weight=27, minsize=225)
        body.grid_rowconfigure(2, weight=26, minsize=215)

        self.cpu_card = CpuCard(body)
        self.gpu_card = GpuCard(body)
        self.mem_card = MemCard(body)
        self.net_card = NetCard(body)
        self.disk_card = DiskCard(body)
        self.vol_card = VolumeCard(body)
        self.proc_card = ProcessCard(body)
        self.info_card = InfoCard(body)

        self.cpu_card.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.gpu_card.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        self.mem_card.grid(row=0, column=2, sticky="nsew", padx=6, pady=6)
        self.net_card.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.disk_card.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)
        self.vol_card.grid(row=1, column=2, sticky="nsew", padx=6, pady=6)
        self.proc_card.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)
        self.info_card.grid(row=2, column=2, sticky="nsew", padx=6, pady=6)

    def _apply_dark_titlebar(self):
        """让 Windows 标题栏跟随深色主题（Win10 2004+ / Win11）。"""
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            enabled = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        except Exception:
            pass

    # -- 采样线程 --------------------------------------------------------

    def _sampler_loop(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                snapshot = self.sampler.sample()
            except Exception:
                snapshot = None
            if snapshot is not None and not self._stop.is_set():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(snapshot)
                except queue.Full:
                    pass
            elapsed = time.time() - started
            self._stop.wait(max(0.05, SAMPLE_INTERVAL - elapsed))

    def _pump(self):
        self._drain_actions()
        snapshot = None
        try:
            snapshot = self._queue.get_nowait()
        except queue.Empty:
            pass
        if snapshot is not None:
            try:
                self._render(snapshot)
                self.float.refresh(snapshot)
            except Exception as exc:  # 单帧渲染异常不应终止界面
                print("render error:", exc)
        self.after(120, self._pump)

    def _drain_actions(self):
        while True:
            try:
                action = self._actions.get_nowait()
            except queue.Empty:
                return
            if action == "open":
                self.show_main()
            elif action == "toggle_float":
                self.toggle_float()
            elif action == "toggle_autostart":
                self.toggle_autostart()
            elif action == "quit":
                self.quit_app()

    def _render(self, data: dict):
        self.cpu_card.refresh(data)
        self.gpu_card.refresh(data["gpu"])
        self.mem_card.refresh(data)
        self.net_card.refresh(data)
        self.disk_card.refresh(data)
        self.vol_card.refresh(data["volumes"])
        self.proc_card.refresh(data["procs"])
        self.info_card.refresh(data)

        now = datetime.now()
        clock_text = now.strftime("%H:%M:%S")
        if clock_text != self._last_clock:
            self.clock.configure(text=clock_text)
            self._last_clock = clock_text
        self.uptime.configure(text=f"{data['meta']['host']} · 已运行 "
                                   f"{fmt_uptime(data['meta']['uptime'])}")

    # -- 交互 ------------------------------------------------------------

    def _toggle_top(self):
        topmost = bool(self.attributes("-topmost"))
        self.attributes("-topmost", not topmost)
        self.pin_btn.configure(fg=C_CPU if not topmost else MUTED,
                               text="已置顶" if not topmost else "置顶")

    # -- 托盘 / 悬浮窗 / 开机自启 -----------------------------------------

    def _tray_action(self, action: str):
        """托盘线程回调，只做转投，界面操作全部回到主线程。"""
        self._actions.put(action)

    def save_settings(self):
        save_settings(self.settings)

    def _sync_tray(self):
        self.tray.set_state(self.float.visible, self.autostart)

    def show_main(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        self.show_float_button_state()

    def show_float_button_state(self):
        self.float_btn.configure(fg=C_CPU if self.float.visible else MUTED,
                                 text="关闭悬浮窗" if self.float.visible else "悬浮窗")

    def toggle_float(self, force: bool | None = None):
        show = (not self.float.visible) if force is None else force
        if show:
            self.float.show()
        else:
            self.float.hide()
        self.settings["float"] = show
        self.save_settings()
        self._sync_tray()
        self.show_float_button_state()

    def toggle_autostart(self):
        enabled = not self.autostart
        if set_autostart(enabled, ENTRY_SCRIPT):
            self.autostart = enabled
            self._sync_tray()
            self.tray.notify("系统监控",
                             "已开启开机自动启动" if enabled else "已关闭开机自动启动")

    def _on_close(self):
        """关闭主窗口 = 最小化到托盘，程序继续在后台运行。"""
        if self._quitting:
            return
        self.withdraw()
        if not self.settings.get("hint_shown"):
            self.tray.notify("系统监控", "已最小化到托盘，双击托盘图标可重新打开")
            self.settings["hint_shown"] = True
            self.save_settings()

    def quit_app(self):
        if self._quitting:
            return
        self._quitting = True
        self.settings["float"] = self.float.visible
        self.save_settings()
        self._stop.set()
        self._activation.stop()
        self.tray.stop()
        self.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    if not acquire_single_instance():
        signal_running_instance()
        return

    startup = "--startup" in sys.argv
    settings = load_settings()
    app = MonitorApp(settings, startup=startup)
    if startup or "--float" in sys.argv or settings.get("float"):
        app.toggle_float(True)
    app.mainloop()


if __name__ == "__main__":
    main()
