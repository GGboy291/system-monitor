# -*- coding: utf-8 -*-
"""系统指标采集：CPU / 内存 / GPU / 网络 / 磁盘 / 进程。

GPU 使用 Windows PDH 性能计数器（与任务管理器同源），无需安装厂商 SDK。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import platform
import re
import time
import winreg

import psutil

# --------------------------------------------------------------------------
# GPU：PDH 性能计数器
# --------------------------------------------------------------------------

_pdh = ctypes.WinDLL("pdh.dll")
_pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(wt.HANDLE)]
_pdh.PdhOpenQueryW.restype = ctypes.c_uint32
_pdh.PdhAddEnglishCounterW.argtypes = [wt.HANDLE, ctypes.c_wchar_p, ctypes.c_size_t,
                                       ctypes.POINTER(wt.HANDLE)]
_pdh.PdhAddEnglishCounterW.restype = ctypes.c_uint32
_pdh.PdhCollectQueryData.argtypes = [wt.HANDLE]
_pdh.PdhCollectQueryData.restype = ctypes.c_uint32
_pdh.PdhGetFormattedCounterArrayW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.DWORD),
                                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_uint32

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


class _FmtValue(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("longValue", ctypes.c_long),
                    ("doubleValue", ctypes.c_double),
                    ("largeValue", ctypes.c_longlong),
                    ("AnsiStringValue", ctypes.c_char_p),
                    ("WideStringValue", ctypes.c_wchar_p)]
    _anonymous_ = ("u",)
    _fields_ = [("CStatus", wt.DWORD), ("u", _U)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _FmtValue)]


_ENGINE_RE = re.compile(
    r"^pid_\d+_luid_(0x[0-9A-Fa-f]+)_(0x[0-9A-Fa-f]+)_phys_(\d+)_eng_(\d+)_engtype_(.+)$")

# 引擎类型展示名（合并同类项）
_TYPE_LABEL = [
    ("High Priority 3D", "3D"),
    ("3D", "3D"),
    ("VideoDecode", "视频解码"),
    ("VideoEncode", "视频编码"),
    ("Video JPEG", "视频"),
    ("Video Processing", "视频"),
    ("Compute", "计算"),
    ("Copy", "拷贝"),
]


def _read_counter_array(counter) -> list:
    """读取通配符计数器的全部实例，返回 [(实例名, 数值), ...]。"""
    size = wt.DWORD(0)
    count = wt.DWORD(0)
    rc = _pdh.PdhGetFormattedCounterArrayW(counter, PDH_FMT_DOUBLE,
                                           ctypes.byref(size), ctypes.byref(count), None)
    if rc not in (0, PDH_MORE_DATA) or size.value == 0:
        return []
    buf = ctypes.create_string_buffer(size.value)
    rc = _pdh.PdhGetFormattedCounterArrayW(
        counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count),
        ctypes.cast(buf, ctypes.c_void_p))
    if rc != 0:
        return []
    items = ctypes.cast(buf, ctypes.POINTER(_FmtItem))
    out = []
    for i in range(count.value):
        item = items[i]
        if item.FmtValue.CStatus == 0:
            out.append((item.szName or "", float(item.FmtValue.doubleValue)))
    return out


def _reg_value(path: str, name: str):
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


class GpuSampler:
    """GPU 使用率 / 显存占用。读取失败时 available=False，界面自动降级。"""

    COUNTER_ENGINE = r"\GPU Engine(*)\Utilization Percentage"
    COUNTER_MEMORY = r"\GPU Adapter Memory(*)\Dedicated Usage"

    def __init__(self):
        self.available = False
        self.name = ""
        self.vram_total = 0
        self._query = wt.HANDLE()
        self._engine = wt.HANDLE()
        self._memory = wt.HANDLE()
        self._load_static_info()
        self._setup()

    def _load_static_info(self):
        key = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0000"
        self.name = _reg_value(key, "DriverDesc") or "GPU"
        vram = _reg_value(key, "HardwareInformation.qwMemorySize")
        self.vram_total = vram if isinstance(vram, int) and vram > 0 else 0

    def _setup(self):
        if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            return
        ok = True
        for path, holder in ((self.COUNTER_ENGINE, "_engine"),
                             (self.COUNTER_MEMORY, "_memory")):
            counter = wt.HANDLE()
            if _pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(counter)) != 0:
                ok = False
            setattr(self, holder, counter)
        if not ok:
            return
        _pdh.PdhCollectQueryData(self._query)
        time.sleep(0.08)
        _pdh.PdhCollectQueryData(self._query)
        self.available = True

    def sample(self) -> dict:
        if not self.available:
            return {"available": False, "name": self.name, "vram_total": self.vram_total,
                    "util": 0.0, "vram_used": 0, "types": []}

        _pdh.PdhCollectQueryData(self._query)

        engines, engine_types = {}, {}
        for name, value in _read_counter_array(self._engine):
            m = _ENGINE_RE.match(name)
            if not m:
                continue
            key = (m.group(1), m.group(3), m.group(4))  # luid, phys, engine index
            engines[key] = engines.get(key, 0.0) + value
            engine_types[key] = m.group(5)

        engines = {k: min(100.0, v) for k, v in engines.items()}

        # 每类引擎占用 = 该类型中最繁忙的物理引擎
        types = {}
        for key, value in engines.items():
            label = None
            etype = engine_types.get(key, "")
            for needle, name_cn in _TYPE_LABEL:
                if etype.startswith(needle):
                    label = name_cn
                    break
            if label is None:
                continue
            types[label] = max(types.get(label, 0.0), value)

        vram_used = 0
        for name, value in _read_counter_array(self._memory):
            vram_used = max(vram_used, value)

        overall = max(engines.values()) if engines else 0.0
        top_types = sorted(types.items(), key=lambda kv: -kv[1])[:2]

        return {"available": True, "name": self.name, "vram_total": self.vram_total,
                "util": overall, "vram_used": int(vram_used), "types": top_types}


# --------------------------------------------------------------------------
# 整体采样
# --------------------------------------------------------------------------

class SystemSampler:
    """周期性采集全部指标，每组速率均由两次采样的差值计算。"""

    def __init__(self):
        self.ncpu = psutil.cpu_count(logical=True) or 1
        self.ncores = psutil.cpu_count(logical=False) or 1
        self.cpu_name = (_reg_value(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                                    "ProcessorNameString") or platform.processor()).strip()
        self.gpu = GpuSampler()

        self._net_last = psutil.net_io_counters()
        self._net_t = time.time()
        self._disk_last = psutil.disk_io_counters()
        self._disk_t = self._net_t
        self.net_total = [self._net_last.bytes_recv, self._net_last.bytes_sent]
        self.disk_total = [getattr(self._disk_last, "read_bytes", 0),
                           getattr(self._disk_last, "write_bytes", 0)]

        self._proc_times = {}
        self._proc_t = time.time()
        self._procs = []
        self._proc_count = 0
        self._proc_stamp = 0.0

        self._volumes = []
        self._vol_t = 0.0

        self._boot = psutil.boot_time()
        self._os = f"Windows {platform.release()}  (Build {platform.version().split('.')[-1]})"
        self._host = platform.node()

        # 预热 CPU 统计（首次调用返回 0）
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)
        # 预热进程 CPU 时间，保证首个采样周期即能算出占用率
        self._scan_processes(time.time())

    # -- 采集 ------------------------------------------------------------

    def sample(self) -> dict:
        now = time.time()

        cpu = psutil.cpu_percent(None)
        cores = psutil.cpu_percent(None, percpu=True)
        freq = psutil.cpu_freq()
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        net = psutil.net_io_counters()
        dt_net = max(now - self._net_t, 1e-3)
        down = max(0.0, (net.bytes_recv - self._net_last.bytes_recv) / dt_net)
        up = max(0.0, (net.bytes_sent - self._net_last.bytes_sent) / dt_net)
        self._net_last, self._net_t = net, now
        self.net_total = [net.bytes_recv, net.bytes_sent]

        disk = psutil.disk_io_counters()
        dt_disk = max(now - self._disk_t, 1e-3)
        if disk:
            read = max(0.0, (disk.read_bytes - self._disk_last.read_bytes) / dt_disk)
            write = max(0.0, (disk.write_bytes - self._disk_last.write_bytes) / dt_disk)
            self._disk_last, self._disk_t = disk, now
            self.disk_total = [disk.read_bytes, disk.write_bytes]
        else:
            read = write = 0.0

        if now - self._proc_stamp >= 2.0:
            self._scan_processes(now)
            self._proc_stamp = now
        if now - self._vol_t >= 15.0:
            self._scan_volumes()
            self._vol_t = now

        gpu = self.gpu.sample()

        return {
            "ts": now,
            "cpu": {"total": cpu, "cores": cores,
                    "freq": (freq.current / 1000.0) if freq and freq.current else 0.0,
                    "name": self.cpu_name, "threads": self.ncpu, "phys": self.ncores,
                    "procs": self._proc_count},
            "mem": {"percent": vm.percent, "used": vm.used, "total": vm.total,
                    "swap_percent": swap.percent, "swap_used": swap.used,
                    "swap_total": swap.total},
            "gpu": gpu,
            "net": {"down": down, "up": up},
            "disk": {"read": read, "write": write},
            "volumes": list(self._volumes),
            "procs": list(self._procs),
            "meta": {"uptime": now - self._boot, "host": self._host, "os": self._os,
                     "net_total": list(self.net_total), "disk_total": list(self.disk_total)},
        }

    def _scan_processes(self, now):
        dt = max(now - self._proc_t, 1e-3)
        rows = []
        alive = set()
        for proc in psutil.process_iter(["name", "cpu_times", "memory_info"]):
            try:
                pid = proc.pid
                if pid == 0:  # System Idle Process，恒占满统计口径，不计入排行
                    continue
                info = proc.info
                times = info["cpu_times"]
                if times is None:
                    continue
                total = times.user + times.system
                prev = self._proc_times.get(pid)
                alive.add(pid)
                self._proc_times[pid] = total
                if prev is None:
                    continue
                rows.append({"pid": pid,
                             "name": info["name"] or f"pid {pid}",
                             "cpu": max(0.0, (total - prev) / dt / self.ncpu * 100.0),
                             "mem": (info["memory_info"].rss if info["memory_info"] else 0)
                                    / 1048576.0})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for pid in list(self._proc_times):
            if pid not in alive:
                del self._proc_times[pid]
        rows.sort(key=lambda r: (r["cpu"], r["mem"]), reverse=True)
        self._procs = rows[:12]
        self._proc_count = len(alive)
        self._proc_t = now

    def _scan_volumes(self):
        vols = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            vols.append({"mount": part.mountpoint.rstrip("\\") or part.mountpoint,
                         "total": usage.total, "used": usage.used,
                         "percent": usage.percent})
        self._volumes = vols
