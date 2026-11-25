#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""opslens · 服务器体检镜

一条命令，给服务器做一次全身 CT：
资源 / 磁盘 / 安全 / 服务 / 基础 五类共 20+ 项检查，
输出 0-100 健康分、A-F 等级、分级问题清单，以及一份可分享的 HTML 报告。

设计约束：
  * 零第三方依赖，只用 Python 标准库；
  * 单文件分发，curl 下来就能跑；
  * Linux 全功能，macOS / Windows 自动降级（不报错、不乱判）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as _html
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata

VERSION = "0.1.0"
APP = "opslens"
TAGLINE = "服务器体检镜"

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

CATEGORIES = [
    # key, 中文名, 该类总分（五大类加起来正好 100 分）
    ("resource", "资源", 30),
    ("disk", "磁盘", 22),
    ("security", "安全", 26),
    ("service", "服务", 12),
    ("base", "基础", 10),
]

CATEGORY_NAME = {k: n for k, n, _ in CATEGORIES}
CATEGORY_WEIGHT = {k: w for k, _, w in CATEGORIES}

GRADES = [
    (90, "A", "运行良好"),
    (80, "B", "基本健康"),
    (70, "C", "需要关注"),
    (60, "D", "存在风险"),
    (0, "F", "高危"),
]

DEFAULT_THRESHOLDS = {
    "cpu_load_warn": 0.7,
    "cpu_load_crit": 1.5,
    "cpu_usage_warn": 70,
    "cpu_usage_crit": 90,
    "mem_warn": 70,
    "mem_crit": 85,
    "swap_warn": 50,
    "swap_crit": 85,
    "disk_warn": 80,
    "disk_crit": 90,
    "inode_warn": 80,
    "inode_crit": 90,
    "zombie_warn": 5,
    "zombie_crit": 20,
    "dns_warn_ms": 300,
    "dns_crit_ms": 2000,
}

STATUS_ICON = {"ok": "✔", "warn": "!", "crit": "✘", "info": "·", "skip": "–"}
ANSI = {"ok": "32", "warn": "33", "crit": "31", "info": "36", "skip": "90"}
STATUS_LABEL = {"ok": "正常", "warn": "提示", "crit": "严重", "info": "信息", "skip": "跳过"}


# --------------------------------------------------------------------------
# 基础工具
# ------------------------------------------------------------------


class Finding(object):
    """一条检查结果。points 是该项在满分模型里的分值。"""

    __slots__ = ("key", "category", "title", "status", "detail", "advice", "points")

    def __init__(self, key, category, title, status, detail, advice="", points=0):
        self.key = key
        self.category = category
        self.title = title
        self.status = status
        self.detail = detail
        self.advice = advice
        self.points = points

    @property
    def deduction(self):
        if self.status == "crit":
            return float(self.points)
        if self.status == "warn":
            return round(self.points * 0.6, 2)
        return 0.0

    def to_dict(self):
        return {
            "key": self.key,
            "category": self.category,
            "category_name": CATEGORY_NAME.get(self.category, self.category),
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "advice": self.advice,
            "points": self.points,
            "deduction": self.deduction,
        }


def fnd(key, category, title, status, detail, advice="", points=0):
    return Finding(key, category, title, status, detail, advice, points)


def safe(fn, *args, **kwargs):
    default = kwargs.pop("_default", None)
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def run(cmd, timeout=5):
    """执行一条外部命令，返回 (返回码, stdout, stderr)。永不抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            universal_newlines=True,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception:
        return 127, "", ""


def read_text(path, limit=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read() if limit is None else fh.read(limit)
    except Exception:
        return None


def human_bytes(num):
    if num is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    val = float(num)
    for unit in units:
        if abs(val) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return "%d%s" % (int(val), unit)
            return "%.1f%s" % (val, unit)
        val /= 1024.0
    return "%.1fPB" % val


def pct(part, total):
    if not total:
        return None
    return round(100.0 * part / total, 1)


def _dw(text):
    """按终端显示宽度计算字符长度（中文算 2）。"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text, width):
    extra = width - _dw(text)
    return text + " " * max(0, extra)


def grade_of(score):
    for threshold, letter, note in GRADES:
        if score >= threshold:
            return letter, note
    return "F", "高危"


# --------------------------------------------------------------------------
# 采集器：全部是纯读取 / 只读命令，不修改系统
# ------------------------------------------------------------------


def os_release():
    data = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        text = read_text(path)
        if not text:
            continue
        for line in text.splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            data[key.strip()] = val.strip().strip('"').strip("'")
        break
    return data.get("PRETTY_NAME") or data.get("NAME") or platform.platform()


def cpu_model_cores():
    model = platform.processor() or ""
    cores = os.cpu_count() or 1
    text = read_text("/proc/cpuinfo")
    if text:
        for line in text.splitlines():
            if line.lower().startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
    return (model.strip() or "unknown"), cores


def read_uptime():
    text = read_text("/proc/uptime")
    if not text:
        return None
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def safe_loadavg():
    try:
        return os.getloadavg()
    except Exception:
        return None


def parse_cpu_stat(text):
    """从 /proc/stat 里取第一行 cpu 的 (idle, total) 累计值。"""
    for line in text.splitlines():
        if line.startswith("cpu "):
            vals = []
            for token in line.split()[1:]:
                try:
                    vals.append(int(token))
                except ValueError:
                    vals.append(0)
            if len(vals) < 4:
                return None
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return idle, sum(vals)
    return None


def cpu_percent(snap_a, snap_b):
    if not snap_a or not snap_b:
        return None
    delta_idle = snap_b[0] - snap_a[0]
    delta_total = snap_b[1] - snap_a[1]
    if delta_total <= 0:
        return None
    busy = 100.0 * (1.0 - float(delta_idle) / float(delta_total))
    return round(max(0.0, min(100.0, busy)), 1)


def sample_cpu_pct(seconds=0.6):
    text_a = read_text("/proc/stat")
    if not text_a:
        return None
    snap_a = parse_cpu_stat(text_a)
    time.sleep(max(0.1, min(3.0, seconds)))
    snap_b = parse_cpu_stat(read_text("/proc/stat") or "")
    return cpu_percent(snap_a, snap_b)


def parse_meminfo(text):
    data = {}
    if not text:
        return data
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        match = re.search(r"(\d+)", rest)
        if match:
            data[key.strip()] = int(match.group(1)) * 1024  # kB -> bytes
    return data


def collect_mem():
    info = parse_meminfo(read_text("/proc/meminfo"))
    if not info:
        return None
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    if avail is None and info.get("MemFree") is not None:
        avail = info["MemFree"] + info.get("Buffers", 0) + info.get("Cached", 0)
    if not total or avail is None:
        return None
    used = max(0, total - avail)
    swap_total = info.get("SwapTotal", 0) or 0
    swap_free = info.get("SwapFree", 0) or 0
    return {
        "total": total,
        "available": avail,
        "used": used,
        "percent": pct(used, total),
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
        "swap_percent": pct(max(0, swap_total - swap_free), swap_total) if swap_total else None,
    }


REAL_FS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs",
    "vfat", "fat", "msdos", "exfat", "ntfs", "ntfs3", "hfs", "hfsplus", "apfs",
    "ufs", "beegfs", "ceph", "overlay",
}


def parse_proc_mounts(text):
    mounts = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        mountpoint = mountpoint.replace("\\040", " ")
        mounts.append({"device": device, "mountpoint": mountpoint, "fstype": fstype})
    return mounts


def collect_disks():
    text = read_text("/proc/mounts")
    if not text:
        return _fallback_disk()
    rows = []
    seen = set()
    for mount in parse_proc_mounts(text):
        fstype = mount["fstype"]
        if fstype not in REAL_FS:
            continue
        if mount["device"].startswith("/dev/loop"):
            continue
        point = mount["mountpoint"]
        if point in seen:
            continue
        seen.add(point)
        try:
            usage = shutil.disk_usage(point)
        except Exception:
            continue
        inode_total = inode_free = None
        try:
            stat = os.statvfs(point)
            if stat.f_files:
                inode_total = stat.f_files
                inode_free = stat.f_favail
        except Exception:
            pass
        rows.append(
            {
                "mountpoint": point,
                "device": mount["device"],
                "fstype": fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": pct(usage.used, usage.total),
                "inode_total": inode_total,
                "inode_free": inode_free,
                "inode_percent": pct(inode_total - inode_free, inode_total)
                if inode_total and inode_free is not None
                else None,
            }
        )
    rows.sort(key=lambda r: (r["percent"] or 0), reverse=True)
    return rows


def _fallback_disk():
    """非 Linux（无 /proc/mounts）时，至少把系统盘算进来。"""
    root = os.path.abspath(os.sep)
    try:
        usage = shutil.disk_usage(root)
    except Exception:
        return []
    return [{
        "mountpoint": root,
        "device": "system",
        "fstype": "native",
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": pct(usage.used, usage.total),
        "inode_total": None,
        "inode_free": None,
        "inode_percent": None,
    }]


def parse_sshd_config(text):
    """把 sshd_config 解析成 {指令: 值}，忽略注释、Match 之后的段落。"""
    data = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("match "):
            break
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key = parts[0].lower()
        value = parts[1].strip()
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        data[key] = value.strip('"').strip("'")
    return data


def collect_sshd():
    for path in ("/etc/ssh/sshd_config", "/usr/local/etc/ssh/sshd_config"):
        text = read_text(path)
        if text is not None:
            return {"path": path, "config": parse_sshd_config(text)}
    return None


def collect_firewall():
    if platform.system() != "Linux":
        return {"active": None, "kind": None}
    for service, name in (("ufw", "ufw"), ("firewalld", "firewalld")):
        code, out, _ = run(["systemctl", "is-active", service])
        if code == 0 and out.strip() == "active":
            return {"active": True, "kind": name}
    code, out, _ = run(["nft", "list", "ruleset"])
    if code == 0 and out.count("chain") >= 1:
        return {"active": True, "kind": "nftables"}
    code, out, _ = run(["iptables", "-S"])
    if code == 0:
        custom = [ln for ln in out.splitlines() if ln.startswith("-A")]
        if custom:
            return {"active": True, "kind": "iptables（%d 条自定义规则）" % len(custom)}
    return {"active": False, "kind": None}


def collect_shadow():
    path = "/etc/shadow"
    try:
        stat = os.stat(path)
    except Exception:
        return None
    mode = stat.st_mode & 0o777
    text = read_text(path)
    accounts = None
    empty = None
    if text is not None:
        accounts = 0
        empty = 0
        for line in text.splitlines():
            parts = line.split(":")
            if len(parts) < 2:
                continue
            accounts += 1
            if parts[1] == "":
                empty += 1
    return {"path": path, "mode": mode, "accounts": accounts, "empty": empty}


def collect_failed_units():
    code, out, _ = run(["systemctl", "list-units", "--state=failed", "--no-legend", "--plain"])
    if code != 0:
        code, out, _ = run(["systemctl", "--failed", "--no-legend", "--plain"])
        if code != 0:
            return None
    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("0 loaded"):
            continue
        token = line.split()[0]
        if token.endswith(".service") or token.endswith(".mount") or token.endswith(".socket"):
            names.append(token)
    return names


def detect_reboot_required():
    if platform.system() != "Linux":
        return None
    flags = []
    if os.path.exists("/var/run/reboot-required") or os.path.exists("/run/reboot-required"):
        flags.append("系统标记需要重启")
    running = platform.release()
    newest = None
    try:
        candidates = [
            (os.path.getmtime(os.path.join("/boot", name)), name.replace("vmlinuz-", ""))
            for name in os.listdir("/boot")
            if name.startswith("vmlinuz-")
        ]
        if candidates:
            newest = max(candidates)[1]
    except Exception:
        newest = None
    if newest and running and newest != running:
        flags.append("运行内核 %s，已安装 %s" % (running, newest))
    return flags


def collect_ntp():
    code, out, _ = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if code == 0 and out.strip():
        return out.strip().lower() == "yes"
    code, out, _ = run(["chronyc", "tracking"])
    if code == 0:
        for line in out.splitlines():
            if "Leap status" in line:
                return "normal" in line.lower()
    return None


def probe_dns(host="github.com"):
    """做两次解析：冷启动那次往往被系统 DNS 缓存/代理拖慢，取较快的一次作为判定值。"""
    samples = []
    error = None
    for _ in range(2):
        started = time.time()
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            samples.append(round((time.time() - started) * 1000, 1))
        except Exception as exc:
            error = str(exc)[:80]
            break
    if not samples:
        return {"ok": False, "host": host, "ms": None, "error": error or "解析失败"}
    return {"ok": True, "host": host, "ms": min(samples), "samples": samples}


def parse_proc_route(text):
    lines = (text or "").splitlines()
    if not lines:
        return []
    headers = lines[0].split()
    rows = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < len(headers):
            continue
        rows.append(dict(zip(headers, parts)))
    return rows


def collect_route():
    rows = parse_proc_route(read_text("/proc/net/route"))
    if not rows:
        return None
    gateways = []
    for row in rows:
        if row.get("Destination") == "00000000" and row.get("Gateway", "00000000") != "00000000":
            gateways.append(row.get("Iface"))
    return {"default_gateway": bool(gateways), "iface": gateways[0] if gateways else None}


def count_zombies():
    base = "/proc"
    if not os.path.isdir(base):
        return None
    total = 0
    try:
        entries = os.listdir(base)
    except Exception:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        text = read_text(os.path.join(base, name, "stat"))
        if not text:
            continue
        head, _, tail = text.rpartition(")")
        fields = tail.split()
        if fields and fields[0] == "Z":
            total += 1
    return total


def collect_listen_ports():
    ports = {}
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        text = read_text(name)
        if not text:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            if parts[3] != "0A":
                continue
            try:
                port = int(parts[1].split(":")[1], 16)
            except (IndexError, ValueError):
                continue
            ports[port] = ports.get(port, 0) + 1
    return sorted(ports)


def collect_top_mem(limit=3):
    base = "/proc"
    if not os.path.isdir(base):
        return None
    page = 4096
    try:
        page = os.sysconf("SC_PAGE_SIZE")
    except Exception:
        pass
    procs = []
    try:
        entries = os.listdir(base)
    except Exception:
        return None
    for name in entries:
        if not name.isdigit():
            continue
        text = read_text(os.path.join(base, name, "stat"))
        if not text:
            continue
        head, _, tail = text.rpartition(")")
        comm = head.rpartition("(")[2]
        fields = tail.split()
        try:
            rss_pages = int(fields[21])
        except (IndexError, ValueError):
            continue
        procs.append({"pid": int(name), "comm": comm, "rss": rss_pages * page})
    procs.sort(key=lambda p: p["rss"], reverse=True)
    return procs[:limit]


def collect_docker():
    if not shutil.which("docker"):
        return None
    code, out, _ = run(["docker", "ps", "-q"], timeout=6)
    if code != 0:
        return None
    return len([ln for ln in out.splitlines() if ln.strip()])


GB = 1024 ** 3


def demo_context():
    """内置演示数据：不碰真实主机，用来预览报告样式 / 截图。"""
    return {
        "hostname": "web-prod-01",
        "kernel": "6.1.0-28-amd64",
        "arch": "x86_64",
        "python": platform.python_version(),
        "os": "Debian GNU/Linux 12 (bookworm)",
        "cpu_model": "Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz",
        "cpu_cores": 8,
        "uptime": 62 * 86400 + 5 * 3600 + 1800,
        "loadavg": (1.72, 1.41, 1.08),
        "cpu_pct": 23.4,
        "mem": {
            "total": 16 * GB, "available": 6 * GB, "used": 10 * GB, "percent": 62.5,
            "swap_total": 4 * GB, "swap_used": int(0.4 * GB), "swap_percent": 10.0,
        },
        "disks": [
            {"mountpoint": "/", "device": "/dev/vda1", "fstype": "ext4",
             "total": 200 * GB, "used": 174 * GB, "free": 26 * GB, "percent": 87.0,
             "inode_total": 13107200, "inode_free": 4970000, "inode_percent": 62.1},
            {"mountpoint": "/data", "device": "/dev/vdb1", "fstype": "xfs",
             "total": 1000 * GB, "used": 610 * GB, "free": 390 * GB, "percent": 61.0,
             "inode_total": 65536000, "inode_free": 58000000, "inode_percent": 11.5},
        ],
        "sshd": {"path": "/etc/ssh/sshd_config",
                 "config": {"permitrootlogin": "yes", "passwordauthentication": "yes", "port": "22"}},
        "firewall": {"active": True, "kind": "ufw"},
        "shadow": {"path": "/etc/shadow", "mode": 0o640, "accounts": 12, "empty": 0},
        "failed_units": ["nginx.service", "php8.2-fpm.service"],
        "reboot_flags": ["运行内核 6.1.0-28-amd64，已安装 6.1.0-31-amd64"],
        "ntp": True,
        "dns": {"ok": True, "host": "github.com", "ms": 18.6},
        "route": {"default_gateway": True, "iface": "eth0"},
        "zombies": 7,
        "listen": [22, 80, 443, 3306, 6379],
        "top_mem": [
            {"pid": 812, "comm": "mysqld", "rss": int(1.9 * GB)},
            {"pid": 1024, "comm": "php-fpm", "rss": int(0.8 * GB)},
            {"pid": 233, "comm": "redis-server", "rss": int(0.2 * GB)},
        ],
        "docker": 3,
    }


def collect_context(sample=0.6):    return {
        "hostname": socket.gethostname(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "os": safe(os_release, _default=platform.system()),
        "cpu_model": None,
        "cpu_cores": os.cpu_count() or 1,
        "uptime": safe(read_uptime),
        "loadavg": safe(safe_loadavg),
        "cpu_pct": safe(sample_cpu_pct, sample),
        "mem": safe(collect_mem),
        "disks": safe(collect_disks, _default=[]),
        "sshd": safe(collect_sshd),
        "firewall": safe(collect_firewall),
        "shadow": safe(collect_shadow),
        "failed_units": safe(collect_failed_units),
        "reboot_flags": safe(detect_reboot_required),
        "ntp": safe(collect_ntp),
        "dns": safe(probe_dns),
        "route": safe(collect_route),
        "zombies": safe(count_zombies),
        "listen": safe(collect_listen_ports, _default=[]),
        "top_mem": safe(collect_top_mem),
        "docker": safe(collect_docker),
    }


# --------------------------------------------------------------------------
# 检查项
# ------------------------------------------------------------------


def check_resource(ctx, th):
    items = []
    cores = ctx.get("cpu_cores") or 1
    load = ctx.get("loadavg")
    if load:
        ratio = load[0] / float(cores)
        if ratio >= th["cpu_load_crit"]:
            status, advice = "crit", "负载持续超过核数，先查 top / sar 找出吃 CPU 的进程，必要时扩容或限流"
        elif ratio >= th["cpu_load_warn"]:
            status, advice = "warn", "负载偏高，建议观察 15 分钟均值与最忙进程"
        else:
            status, advice = "ok", ""
        items.append(fnd("cpu_load", "resource", "CPU 负载", status,
                         "1 分钟 %.2f / %d 核（%.2f/核），5/15 分钟 %.2f / %.2f"
                         % (load[0], cores, ratio, load[1], load[2]),
                         advice, 10))
    else:
        items.append(fnd("cpu_load", "resource", "CPU 负载", "skip", "当前平台不支持读取负载均值", "", 10))

    cpu_pct = ctx.get("cpu_pct")
    if cpu_pct is None:
        items.append(fnd("cpu_usage", "resource", "CPU 使用率", "skip", "无法采样 /proc/stat", "", 6))
    else:
        if cpu_pct >= th["cpu_usage_crit"]:
            status, advice = "crit", "CPU 使用率长期接近满载，确认是否有死循环或内存回收压力"
        elif cpu_pct >= th["cpu_usage_warn"]:
            status, advice = "warn", "CPU 使用率偏高，留意突发流量与定时任务叠加"
        else:
            status, advice = "ok", ""
        items.append(fnd("cpu_usage", "resource", "CPU 使用率", status,
                         "采样 %.1f%%（%d 核 %s）" % (cpu_pct, cores, ctx.get("cpu_model") or "CPU"),
                         advice, 6))

    mem = ctx.get("mem")
    if not mem:
        items.append(fnd("mem_usage", "resource", "内存使用", "skip", "无法读取 /proc/meminfo", "", 10))
        items.append(fnd("swap_usage", "resource", "交换分区", "skip", "无法读取 /proc/meminfo", "", 4))
        return items
    value = mem["percent"]
    if value >= th["mem_crit"]:
        status, advice = "crit", "内存接近耗尽，关注 OOM 风险；先查占用最高的进程，再考虑加内存或加 swap"
    elif value >= th["mem_warn"]:
        status, advice = "warn", "内存使用偏高，确认缓存与业务进程占比"
    else:
        status, advice = "ok", ""
    items.append(fnd("mem_usage", "resource", "内存使用", status,
                     "%.1f%%（已用 %s / 共 %s）" % (value, human_bytes(mem["used"]), human_bytes(mem["total"])),
                     advice, 10))

    if not mem["swap_total"]:
        items.append(fnd("swap_usage", "resource", "交换分区", "info", "未启用 swap（纯内存运行，注意 OOM 兜底）", "", 4))
    else:
        swap_pct = mem["swap_percent"] or 0
        if swap_pct >= th["swap_crit"]:
            status, advice = "crit", "swap 大量占用，说明物理内存不足，性能会明显下降"
        elif swap_pct >= th["swap_warn"]:
            status, advice = "warn", "swap 已开始被使用，关注内存水位"
        else:
            status, advice = "ok", ""
        items.append(fnd("swap_usage", "resource", "交换分区", status,
                         "%.1f%%（%s / %s）" % (swap_pct, human_bytes(mem["swap_used"]), human_bytes(mem["swap_total"]))
                         , advice, 4))
    return items


def check_disk(ctx, th):
    items = []
    disks = ctx.get("disks") or []
    if not disks:
        items.append(fnd("disk_space", "disk", "磁盘空间", "skip", "未找到可统计的本地文件系统", "", 14))
        items.append(fnd("inode", "disk", "inode 使用", "skip", "未找到可统计的本地文件系统", "", 8))
        return items

    worst = disks[0]
    value = worst["percent"] or 0
    if value >= th["disk_crit"]:
        status, advice = "crit", "空间即将耗尽，立刻清理日志 / 镜像 / 备份，并检查是否有异常写入"
    elif value >= th["disk_warn"]:
        status, advice = "warn", "空间偏紧，建议清理旧日志与无用缓存，或扩容"
    else:
        status, advice = "ok", ""
    detail = "%s 使用 %.1f%%（%s / %s，剩余 %s）" % (
        worst["mountpoint"], value, human_bytes(worst["used"]),
        human_bytes(worst["total"]), human_bytes(worst["free"]),
    )
    others = [d for d in disks[1:] if (d["percent"] or 0) >= th["disk_warn"]]
    if others:
        detail += "；另有 %d 个挂载点超过 %d%%" % (len(others), th["disk_warn"])
    if not any(d["mountpoint"] == "/" for d in disks):
        # 根分区被过滤掉（overlay 之外的未知类型、或 / 本身就是内存盘）
        # 此时磁盘评分只覆盖子挂载点，不能装作满分
        detail += "；根分区 / 未纳入统计"
        if status == "ok":
            status = "warn"
            advice = ("根分区 / 的文件系统不在采集范围（或 / 是内存盘），磁盘评分只覆盖 %s 等挂载点，"
                      "请人工确认根盘容量" % worst["mountpoint"])
    items.append(fnd("disk_space", "disk", "磁盘空间", status, detail, advice, 14))

    inode_worst = None
    for disk in disks:
        if disk["inode_percent"] is None:
            continue
        if inode_worst is None or disk["inode_percent"] > inode_worst["inode_percent"]:
            inode_worst = disk
    if not inode_worst:
        items.append(fnd("inode", "disk", "inode 使用", "skip", "文件系统未提供 inode 统计", "", 8))
    else:
        value = inode_worst["inode_percent"]
        if value >= th["inode_crit"]:
            status, advice = "crit", "inode 耗尽，常见于海量小文件（session / 邮件队列 / 缓存），删目录比删文件有效"
        elif value >= th["inode_warn"]:
            status, advice = "warn", "inode 偏紧，排查小文件堆积"
        else:
            status, advice = "ok", ""
        items.append(fnd("inode", "disk", "inode 使用", status,
                         "%s %.1f%%（已用 %d / 共 %d）" % (
                             inode_worst["mountpoint"], value,
                             inode_worst["inode_total"] - inode_worst["inode_free"],
                             inode_worst["inode_total"]), advice, 8))
    return items


def check_security(ctx, th):
    items = []
    sshd = ctx.get("sshd")
    if not sshd:
        items.append(fnd("ssh_root", "security", "SSH root 登录", "skip", "未找到 sshd_config", "", 6))
        items.append(fnd("ssh_password_auth", "security", "SSH 密码登录", "skip", "未找到 sshd_config", "", 6))
    else:
        cfg = sshd["config"]
        root_login = (cfg.get("permitrootlogin") or "prohibit-password").lower()
        if root_login == "yes":
            status, advice = "crit", "root 可直接密码登录，暴力破解的首要目标；改成 PermitRootLogin no（或 prohibit-password）并只用密钥"
        elif root_login in ("prohibit-password", "without-password", "forced-commands-only"):
            status, advice = "warn", "root 允许登录（仅密钥），建议彻底关闭 root 远程登录，改用普通用户 + sudo"
        else:
            status, advice = "ok", ""
        items.append(fnd("ssh_root", "security", "SSH root 登录", status,
                         "PermitRootLogin = %s（%s）" % (root_login, sshd["path"]), advice, 6))

        pwd_auth = (cfg.get("passwordauthentication") or "yes").lower()
        if pwd_auth == "yes":
            status, advice = "warn", "密码登录开放，容易被扫爆破；配置密钥登录后设 PasswordAuthentication no"
        elif pwd_auth == "no":
            status, advice = "ok", ""
        else:
            status, advice = "info", "该值非标准，建议显式写 PasswordAuthentication no"
        items.append(fnd("ssh_password_auth", "security", "SSH 密码登录", status,
                         "PasswordAuthentication = %s" % pwd_auth, advice, 6))

    firewall = ctx.get("firewall")
    if not firewall:
        items.append(fnd("firewall", "security", "防火墙", "skip", "无法探测防火墙状态（可能缺少权限）", "", 6))
    elif firewall.get("active") is None:
        items.append(fnd("firewall", "security", "防火墙", "skip",
                         "非 Linux 平台，请用系统自带防火墙工具核查", "", 6))
    elif firewall.get("active"):
        items.append(fnd("firewall", "security", "防火墙", "ok", "%s 已启用" % firewall["kind"], "", 6))
    else:
        items.append(fnd("firewall", "security", "防火墙", "warn",
                         "未检测到 ufw / firewalld / nftables / iptables 规则",
                         "公网机器至少放行必要端口；云上优先用安全组，主机内再补一层", 6))

    shadow = ctx.get("shadow")
    if not shadow:
        items.append(fnd("shadow_perm", "security", "关键文件权限", "skip", "无法读取 /etc/shadow（需要 root）", "", 4))
        items.append(fnd("empty_password", "security", "空密码账号", "skip", "无法读取 /etc/shadow（需要 root）", "", 4))
    else:
        mode = shadow["mode"]
        if mode & 0o007 or mode > 0o640:
            status, advice = "warn", "权限过宽，任何用户都能读到密码哈希；chmod 640 /etc/shadow 并把属组设为 shadow"
        else:
            status, advice = "ok", ""
        items.append(fnd("shadow_perm", "security", "关键文件权限", status,
                         "/etc/shadow 权限 %04o" % mode, advice, 4))

        empty = shadow["empty"]
        if empty is None:
            items.append(fnd("empty_password", "security", "空密码账号", "skip",
                             "无权限读取 /etc/shadow，无法核查空密码账号", "", 4))
        elif empty:
            status, advice = "crit", "存在空密码账号，等于把门开着；用 passwd -l 锁定或直接删除"
        else:
            status, advice = "ok", ""
        if empty is not None:
            items.append(fnd("empty_password", "security", "空密码账号", status,
                             "%d 个账号中 %d 个密码字段为空" % (shadow["accounts"], empty), advice, 4))
    return items


def check_service(ctx, th):
    items = []
    units = ctx.get("failed_units")
    if units is None:
        items.append(fnd("failed_units", "service", "systemd 失败单元", "skip", "无 systemd 或无法查询", "", 6))
    elif units:
        items.append(fnd("failed_units", "service", "systemd 失败单元", "crit",
                         "%d 个单元处于 failed：%s" % (len(units), "、".join(units[:5])),
                         "逐个 systemctl status 看日志，修好后再 systemctl reset-failed，别让失败单元反复重启", 6))
    else:
        items.append(fnd("failed_units", "service", "systemd 失败单元", "ok", "没有 failed 单元", "", 6))

    flags = ctx.get("reboot_flags")
    if flags is None:
        items.append(fnd("reboot", "service", "内核更新", "skip", "无法判断是否需要重启", "", 3))
    elif flags:
        items.append(fnd("reboot", "service", "内核更新", "warn", "；".join(flags), "找维护窗口重启，让内核与库的补丁真正生效", 3))
    else:
        items.append(fnd("reboot", "service", "内核更新", "ok", "运行内核即已安装的最新内核", "", 3))

    ntp = ctx.get("ntp")
    if ntp is None:
        items.append(fnd("ntp", "service", "时间同步", "skip", "无法查询 NTP 状态（无 timedatectl / chrony）", "", 3))
    elif ntp:
        items.append(fnd("ntp", "service", "时间同步", "ok", "NTP 已同步", "", 3))
    else:
        items.append(fnd("ntp", "service", "时间同步", "warn", "NTP 未同步", "证书、日志、集群一致性都怕时间漂移；启用 chrony / systemd-timesyncd", 3))
    return items


def check_base(ctx, th):
    items = []
    dns = ctx.get("dns")
    if not dns:
        items.append(fnd("dns", "base", "DNS 解析", "skip", "未执行解析探测", "", 4))
    elif not dns.get("ok"):
        items.append(fnd("dns", "base", "DNS 解析", "crit",
                         "解析 %s 失败：%s" % (dns.get("host"), dns.get("error") or "未知错误"),
                         "先看 /etc/resolv.conf，再试 dig / nslookup；DNS 挂了会连带包管理、证书续签、外部接口全挂", 4))
    else:
        latency = dns.get("ms") or 0
        samples = dns.get("samples") or []
        extra = ""
        if len(samples) > 1 and samples[0] >= 3 * (latency + 1):
            extra = "（首次冷解析 %.0f ms，缓存后 %.1f ms）" % (samples[0], samples[-1])
        if latency >= th["dns_crit_ms"]:
            status, advice = "crit", "解析极慢，检查上游 DNS 与网络丢包"
        elif latency >= th["dns_warn_ms"]:
            status, advice = "warn", "解析偏慢，考虑换本地缓存或更近的 DNS"
        else:
            status, advice = "ok", ""
        items.append(fnd("dns", "base", "DNS 解析", status,
                         "%s 解析成功，耗时 %.1f ms%s" % (dns.get("host"), latency, extra), advice, 4))

    route = ctx.get("route")
    if not route:
        items.append(fnd("route", "base", "默认路由", "skip", "无法读取路由表（非 Linux 或权限不足）", "", 3))
    elif route.get("default_gateway"):
        items.append(fnd("route", "base", "默认路由", "ok", "默认网关 %s" % route.get("iface"), "", 3))
    else:
        items.append(fnd("route", "base", "默认路由", "crit", "没有可用的默认网关", "机器可能出不了网，检查 ip route / NetworkManager 配置", 3))

    zombies = ctx.get("zombies")
    if zombies is None:
        items.append(fnd("zombies", "base", "僵尸进程", "skip", "无法读取 /proc", "", 3))
    elif zombies >= th["zombie_crit"]:
        items.append(fnd("zombies", "base", "僵尸进程", "crit", "%d 个僵尸进程" % zombies,
                         "父进程没回收子进程，找到父进程修逻辑或重启服务", 3))
    elif zombies >= th["zombie_warn"]:
        items.append(fnd("zombies", "base", "僵尸进程", "warn", "%d 个僵尸进程" % zombies,
                         "数量在增长就要查父进程", 3))
    else:
        items.append(fnd("zombies", "base", "僵尸进程", "ok", "%d 个僵尸进程" % zombies, "", 3))
    return items


CHECKS = {
    "resource": check_resource,
    "disk": check_disk,
    "security": check_security,
    "service": check_service,
    "base": check_base,
}


# --------------------------------------------------------------------------
# 评分与报告
# ------------------------------------------------------------------


def compute_score(findings):
    total = sum(f.deduction for f in findings)
    return int(max(0, min(100, round(100 - total)))), round(total, 1)


def compute_coverage(findings):
    """覆盖率 = 真正执行过的检查项分值占比（跳过项不计入）。
    在容器 / 非 Linux 上大量检查会跳过，覆盖率用于避免「虚假满分」。"""
    total = sum(f.points for f in findings)
    covered = sum(f.points for f in findings if f.status != "skip")
    if not total:
        return 100
    return int(round(covered * 100.0 / total))


def category_scores(findings):
    out = []
    for key, name, weight in CATEGORIES:
        relevant = [f for f in findings if f.category == key]
        deducted = sum(f.deduction for f in relevant)
        score = int(max(0, min(100, round(100 - deducted * 100.0 / weight)))) if weight else 100
        counts = {"ok": 0, "warn": 0, "crit": 0, "info": 0, "skip": 0}
        for f in relevant:
            counts[f.status] = counts.get(f.status, 0) + 1
        out.append({
            "key": key,
            "name": name,
            "weight": weight,
            "score": score,
            "deduction": round(deducted, 1),
            "counts": counts,
        })
    return out


def build_facts(ctx):
    facts = []
    facts.append(("主机", ctx.get("hostname") or "-"))
    facts.append(("系统", ctx.get("os") or "-"))
    facts.append(("内核", "%s / %s" % (ctx.get("kernel") or "-", ctx.get("arch") or "-")))
    if ctx.get("cpu_model"):
        facts.append(("CPU", "%s × %d" % (ctx["cpu_model"], ctx.get("cpu_cores") or 1)))
    uptime = ctx.get("uptime")
    if uptime:
        days, rem = divmod(int(uptime), 86400)
        hours = rem // 3600
        facts.append(("运行时长", "%d 天 %d 小时" % (days, hours)))
    listen = ctx.get("listen") or []
    if listen:
        known = {22: "SSH", 80: "HTTP", 443: "HTTPS", 3306: "MySQL", 6379: "Redis", 5432: "PostgreSQL", 27017: "MongoDB"}
        labels = ["%d(%s)" % (p, known[p]) if p in known else str(p) for p in listen[:8]]
        more = "" if len(listen) <= 8 else " 等 %d 个" % len(listen)
        facts.append(("监听端口", "、".join(labels) + more))
    top = ctx.get("top_mem")
    if top:
        facts.append(("内存 TOP", "、".join("%s %s" % (p["comm"], human_bytes(p["rss"])) for p in top)))
    if ctx.get("docker") is not None:
        facts.append(("运行中容器", "%d 个" % ctx["docker"]))
    return [{"label": a, "value": b} for a, b in facts]


def build_report(ctx, thresholds, started_at=None, elapsed=None):
    findings = []
    for key, _, _ in CATEGORIES:
        checker = CHECKS.get(key)
        if not checker:
            continue
        for item in safe(checker, ctx, thresholds, _default=[]) or []:
            findings.append(item)
    score, deducted = compute_score(findings)
    grade, grade_note = grade_of(score)
    coverage = compute_coverage(findings)
    if coverage < 60:
        grade_note = "样本不足 · 仅 %d%% 的检查项可执行" % coverage
    counts = {"ok": 0, "warn": 0, "crit": 0, "info": 0, "skip": 0}
    for f in findings:
        counts[f.status] = counts.get(f.status, 0) + 1

    prioritized = sorted(
        [f for f in findings if f.status in ("crit", "warn")],
        key=lambda f: (0 if f.status == "crit" else 1, -f.points),
    )

    now = dt.datetime.now().astimezone()
    return {
        "tool": APP,
        "version": VERSION,
        "generated_at": (started_at or now).strftime("%Y-%m-%d %H:%M:%S %Z").strip(),
        "elapsed": round(elapsed, 2) if elapsed is not None else None,
        "score": score,
        "deducted": deducted,
        "grade": grade,
        "grade_note": grade_note,
        "coverage": coverage,
        "counts": counts,
        "categories": category_scores(findings),
        "findings": [f.to_dict() for f in findings],
        "priority": [f.to_dict() for f in prioritized[:5]],
        "facts": build_facts(ctx),
        "host": {
            "hostname": ctx.get("hostname"),
            "os": ctx.get("os"),
            "kernel": ctx.get("kernel"),
            "arch": ctx.get("arch"),
            "python": ctx.get("python"),
        },
    }


def filter_report(report, only=None, skip=None):
    """按分类过滤 findings/categories，分数按剩余项重算（权重归一）。"""
    if not only and not skip:
        return report
    allowed = set()
    for key, _, _ in CATEGORIES:
        if only and key not in only and CATEGORY_NAME[key] not in only:
            continue
        if skip and (key in skip or CATEGORY_NAME[key] in skip):
            continue
        allowed.add(key)
    if not allowed:
        return report
    findings = [f for f in report["findings"] if f["category"] in allowed]
    total = sum(f["points"] for f in findings)
    raw = sum(f["deduction"] for f in findings)
    score = int(max(0, min(100, round(100 - (raw * 100.0 / total if total else 0)))))
    grade, note = grade_of(score)
    counts = {"ok": 0, "warn": 0, "crit": 0, "info": 0, "skip": 0}
    for f in findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    cats = []
    for item in report["categories"]:
        if item["key"] in allowed:
            deducted = sum(f["deduction"] for f in findings if f["category"] == item["key"])
            item = dict(item)
            item["score"] = int(max(0, min(100, round(100 - deducted * 100.0 / item["weight"]))))
            cats.append(item)
    out = dict(report)
    out.update({"findings": findings, "categories": cats, "score": score,
                "grade": grade, "grade_note": note, "counts": counts})
    total_pts = sum(f["points"] for f in findings)
    covered_pts = sum(f["points"] for f in findings if f["status"] != "skip")
    out["coverage"] = int(round(covered_pts * 100.0 / total_pts)) if total_pts else 100
    if out["coverage"] < 60:
        out["grade_note"] = "样本不足 · 仅 %d%% 的检查项可执行" % out["coverage"]
    out["priority"] = sorted([f for f in findings if f["status"] in ("crit", "warn")],
                             key=lambda f: (0 if f["status"] == "crit" else 1, -f["points"]))[:5]
    return out


# --------------------------------------------------------------------------
# 渲染：终端
# ------------------------------------------------------------------


def _c(text, code, use_color):
    if not use_color or not code:
        return text
    return "\033[%sm%s\033[0m" % (code, text)


def render_console(report, use_color=True):
    lines = []
    dim = "90"
    lines.append("")
    lines.append(" " + _c("%s %s" % (APP, report["version"]), "1;36", use_color)
                 + _c("  ·  " + TAGLINE, dim, use_color))
    host = report.get("host") or {}
    lines.append(" " + _c("%s  ·  %s  ·  内核 %s  ·  %s" % (
        host.get("hostname") or "-", host.get("os") or "-",
        host.get("kernel") or "-", host.get("arch") or "-"), dim, use_color))
    lines.append(" " + _c("采样时间 %s   耗时 %s s" % (
        report["generated_at"], report["elapsed"] if report["elapsed"] is not None else "-"), dim, use_color))
    lines.append("")

    score = report["score"]
    color = "32" if score >= 90 else "36" if score >= 80 else "33" if score >= 70 else "31"
    bar_len = 28
    filled = int(round(bar_len * score / 100.0))
    bar = "█" * filled + "░" * (bar_len - filled)
    lines.append("   " + _c(bar, color, use_color) + "  "
                 + _c("%d / 100" % score, "1;%s" % color, use_color)
                 + _c("   等级 %s · %s" % (report["grade"], report["grade_note"]), "1", use_color))
    if report.get("coverage", 100) < 80:
        lines.append("   " + _c("覆盖率 %d%% · %d 项检查在当前环境不可测，评分仅供参考"
                                % (report["coverage"], report["counts"]["skip"]), "33", use_color))
    lines.append("")

    for cat in report["categories"]:
        status = "ok" if cat["score"] >= 85 else "warn" if cat["score"] >= 65 else "crit"
        lines.append("   " + _c(STATUS_ICON[status], ANSI[status], use_color) + " "
                     + _pad(cat["name"], 6)
                     + _c(_pad("%d" % cat["score"], 4), "1;%s" % ANSI[status], use_color)
                     + _c("/ 100 · 占 %d 分" % cat["weight"], dim, use_color))
    lines.append("")

    current = None
    for item in report["findings"]:
        if item["category"] != current:
            current = item["category"]
            lines.append("  " + _c(CATEGORY_NAME.get(current, current), "1;36", use_color))
        icon = STATUS_ICON[item["status"]]
        head = "    %s %s" % (_c(icon, ANSI[item["status"]], use_color), _pad(item["title"], 18))
        lines.append(head + _c(item["detail"], "" if item["status"] in ("ok",) else dim, use_color))
        if item["advice"] and item["status"] in ("warn", "crit"):
            lines.append("      " + _c("→ " + item["advice"], "33" if item["status"] == "warn" else "31", use_color))
    lines.append("")

    if report["priority"]:
        lines.append("  " + _c("优先处理", "1;33", use_color))
        for idx, item in enumerate(report["priority"], 1):
            lines.append("    %d. %s %s" % (idx, _c(item["title"], "1", use_color),
                                            _c(item["detail"], dim, use_color)))
        lines.append("")

    counts = report["counts"]
    summary = "  共 %d 项检查：正常 %d · 提示 %d · 严重 %d · 跳过 %d" % (
        len(report["findings"]), counts["ok"], counts["warn"], counts["crit"], counts["skip"])
    lines.append(_c(summary, dim, use_color))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 渲染：Markdown / JSON
# ------------------------------------------------------------------


def render_markdown(report):
    lines = []
    lines.append("# %s 体检报告" % APP)
    lines.append("")
    lines.append("**评分：%d / 100（%s · %s）**" % (report["score"], report["grade"], report["grade_note"]))
    lines.append("")
    lines.append("- 生成时间：%s" % report["generated_at"])
    host = report.get("host") or {}
    lines.append("- 主机：%s · %s · 内核 %s" % (host.get("hostname"), host.get("os"), host.get("kernel")))
    lines.append("")
    lines.append("## 分类得分")
    lines.append("")
    lines.append("| 分类 | 得分 | 扣分 | 正常 | 提示 | 严重 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for cat in report["categories"]:
        lines.append("| %s | %d | %.1f | %d | %d | %d |" % (
            cat["name"], cat["score"], cat["deduction"],
            cat["counts"]["ok"], cat["counts"]["warn"], cat["counts"]["crit"]))
    lines.append("")
    lines.append("## 问题清单")
    lines.append("")
    for status, title in (("crit", "严重"), ("warn", "提示")):
        rows = [f for f in report["findings"] if f["status"] == status]
        if not rows:
            continue
        lines.append("### %s" % title)
        lines.append("")
        for item in rows:
            lines.append("- **[%s] %s** —— %s" % (item["category_name"], item["title"], item["detail"]))
            if item["advice"]:
                lines.append("  - 建议：%s" % item["advice"])
        lines.append("")
    if not any(f["status"] in ("crit", "warn") for f in report["findings"]):
        lines.append("未发现需要处理的问题。")
        lines.append("")
    lines.append("> 由 %s v%s 生成，检查在本机离线执行。" % (APP, report["version"]))
    return "\n".join(lines)


def render_json(report):
    return json.dumps(report, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 渲染：HTML
# ------------------------------------------------------------------


_CSS = """
:root{--bg:#070b12;--card:rgba(255,255,255,.045);--line:rgba(255,255,255,.09);
--txt:#e9eff7;--dim:#8d9aab;--ok:#2ee6a8;--warn:#f9b83c;--crit:#ff5f6d;--accent:#4fd1c5;}
*{box-sizing:border-box;}
body{margin:0;padding:48px 24px 64px;color:var(--txt);
background:radial-gradient(1200px 700px at 12% -12%,#12314a 0%,#0a111b 46%,#06090f 100%);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased;}
.wrap{max-width:1040px;margin:0 auto;}
.top{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:28px;flex-wrap:wrap;}
.brand{display:flex;align-items:center;gap:12px;font-size:20px;font-weight:700;letter-spacing:.5px;}
.logo{width:34px;height:34px;border-radius:12px;display:grid;place-items:center;
background:linear-gradient(135deg,#2ee6a8,#4fd1c5);color:#04120f;font-size:18px;font-weight:800;}
.ver{font-size:12px;font-weight:500;color:var(--dim);border:1px solid var(--line);padding:2px 8px;border-radius:999px;}
.meta{text-align:right;font-size:13px;color:var(--dim);line-height:1.7;}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:26px;
backdrop-filter:blur(6px);}
.hero{display:grid;grid-template-columns:220px 1fr;gap:28px;align-items:center;margin-bottom:20px;}
.gauge{display:flex;justify-content:center;}
.score-num{font-size:15px;fill:var(--txt);font-weight:700;}
.score-sub{font-size:11px;fill:var(--dim);}
.grade{font-size:34px;font-weight:800;letter-spacing:.5px;}
.grade-note{font-size:15px;color:var(--dim);margin-top:6px;}
.hero-desc{font-size:14px;color:var(--dim);margin-top:14px;line-height:1.8;}
.cats{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin:20px 0;}
.cat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;}
.cat-name{font-size:13px;color:var(--dim);display:flex;justify-content:space-between;}
.cat-score{font-size:26px;font-weight:700;margin:6px 0 10px;}
.bar{height:6px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;}
.bar i{display:block;height:100%;border-radius:999px;transform-origin:left;animation:g .9s cubic-bezier(.2,.8,.2,1);}
@keyframes g{from{transform:scaleX(0);}to{transform:scaleX(1);}}
.schema{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 26px;}
.pill{font-size:12px;color:var(--dim);border:1px solid var(--line);border-radius:999px;padding:5px 12px;background:rgba(255,255,255,.02);}
.pill b{color:var(--txt);font-weight:600;}
.sec{margin-top:26px;}
.sec-h{display:flex;align-items:baseline;gap:12px;margin-bottom:12px;}
.sec-t{font-size:17px;font-weight:700;}
.sec-d{font-size:12px;color:var(--dim);}
.rows{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;}
.row{display:grid;grid-template-columns:34px 1fr auto;gap:14px;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,.05);align-items:start;}
.row:last-child{border-bottom:0;}
.chip{width:26px;height:26px;border-radius:9px;display:grid;place-items:center;font-size:13px;font-weight:700;}
.chip.ok{background:rgba(46,230,168,.12);color:var(--ok);}
.chip.warn{background:rgba(249,184,60,.12);color:var(--warn);}
.chip.crit{background:rgba(255,95,109,.14);color:var(--crit);}
.chip.info,.chip.skip{background:rgba(255,255,255,.06);color:var(--dim);}
.t{font-size:14px;font-weight:600;}
.d{font-size:13px;color:var(--dim);margin-top:4px;line-height:1.7;}
.adv{margin-top:8px;font-size:13px;line-height:1.7;color:var(--warn);}
.adv.crit{color:var(--crit);}
.pts{font-size:12px;color:var(--dim);white-space:nowrap;padding-top:4px;}
.pri{display:grid;gap:10px;}
.pri-row{display:flex;gap:12px;align-items:baseline;font-size:14px;}
.pri-n{color:var(--dim);font-size:12px;min-width:18px;}
.foot{margin-top:34px;font-size:12px;color:var(--dim);text-align:center;line-height:1.9;}
@media(max-width:860px){.hero{grid-template-columns:1fr;}.cats{grid-template-columns:repeat(2,1fr);}.meta{text-align:left;}}
@media print{body{background:#fff;color:#111;}.card,.rows,.cat{border-color:#ddd;background:#fff;}}
"""


def _gauge_svg(score, color):
    radius = 88.0
    cx, cy = 110.0, 110.0
    angle = math.radians(180.0 - 180.0 * max(0, min(100, score)) / 100.0)
    x = cx + radius * math.cos(angle)
    y = cy - radius * math.sin(angle)
    left = cx - radius
    right = cx + radius
    return (
        '<svg viewBox="0 0 220 132" width="220" height="132" role="img">'
        '<path d="M %0.1f %0.1f A %0.1f %0.1f 0 0 1 %0.1f %0.1f" stroke="rgba(255,255,255,.08)" '
        'stroke-width="14" stroke-linecap="round" fill="none"/>'
        '<path d="M %0.1f %0.1f A %0.1f %0.1f 0 0 1 %0.1f %0.1f" stroke="%s" stroke-width="14" '
        'stroke-linecap="round" fill="none"/>'
        '<text class="score-num" x="110" y="102" text-anchor="middle">%d</text>'
        '<text class="score-sub" x="110" y="120" text-anchor="middle">/ 100</text>'
        "</svg>"
    ) % (left, cy, radius, radius, right, cy, left, cy, radius, radius, x, y, color, score)


def _status_color(status):
    return {"ok": "#2ee6a8", "warn": "#f9b83c", "crit": "#ff5f6d"}.get(status, "#7d8b9d")


def render_html(report):
    esc = _html.escape
    score = report["score"]
    color = _status_color("ok" if score >= 85 else "warn" if score >= 70 else "crit")
    host = report.get("host") or {}

    cat_cards = []
    coverage = report.get("coverage", 100)
    cov_note = ""
    if coverage < 60:
        cov_note = ('<br/><span style="color:#f9b83c">覆盖率仅 %d%%，多数检查项在当前环境不可测，'
                    "本分数仅供参考。</span>" % coverage)
    for cat in report["categories"]:
        st = "ok" if cat["score"] >= 85 else "warn" if cat["score"] >= 65 else "crit"
        cat_cards.append(
            '<div class="cat"><div class="cat-name"><span>%s</span><span>占 %d 分</span></div>'
            '<div class="cat-score" style="color:%s">%d</div>'
            '<div class="bar"><i style="width:%d%%;background:%s"></i></div></div>'
            % (esc(cat["name"]), cat["weight"], _status_color(st), cat["score"], cat["score"], _status_color(st))
        )

    pills = []
    counts = report["counts"]
    pills.append('<span class="pill">检查项 <b>%d</b></span>' % len(report["findings"]))
    pills.append('<span class="pill">正常 <b>%d</b></span>' % counts["ok"])
    pills.append('<span class="pill">提示 <b>%d</b></span>' % counts["warn"])
    pills.append('<span class="pill">严重 <b>%d</b></span>' % counts["crit"])
    pills.append('<span class="pill">覆盖率 <b>%d%%</b></span>' % report.get("coverage", 100))
    if counts["skip"]:
        pills.append('<span class="pill">跳过 <b>%d</b></span>' % counts["skip"])
    for fact in report["facts"]:
        pills.append('<span class="pill">%s <b>%s</b></span>' % (esc(fact["label"]), esc(str(fact["value"]))))

    pri_html = ""
    if report["priority"]:
        rows = []
        for idx, item in enumerate(report["priority"], 1):
            rows.append(
                '<div class="pri-row"><span class="pri-n">%d</span><span class="chip %s">%s</span>'
                "<span><b>%s</b>　<span class=\"d\" style=\"margin:0\">%s</span></span></div>"
                % (idx, item["status"], STATUS_ICON[item["status"]], esc(item["title"]), esc(item["detail"]))
            )
        pri_html = ('<div class="sec"><div class="sec-h"><span class="sec-t">优先处理</span>'
                    '<span class="sec-d">按严重程度排序</span></div><div class="card pri">%s</div></div>'
                    % "".join(rows))

    sections = []
    for cat in report["categories"]:
        items = [f for f in report["findings"] if f["category"] == cat["key"]]
        if not items:
            continue
        rows = []
        for item in items:
            advice = ""
            if item["advice"] and item["status"] in ("warn", "crit"):
                advice = ('<div class="adv %s">→ %s</div>' % (item["status"], esc(item["advice"])))
            rows.append(
                '<div class="row"><span class="chip %s">%s</span><div><div class="t">%s</div>'
                '<div class="d">%s</div>%s</div><span class="pts">%s</span></div>'
                % (item["status"], STATUS_ICON[item["status"]], esc(item["title"]),
                   esc(item["detail"]), advice,
                   ("-%g" % item["deduction"]) if item["deduction"] else "")
            )
        sections.append(
            '<div class="sec"><div class="sec-h"><span class="sec-t">%s</span>'
            '<span class="sec-d">%d 分 · 占 %d 分</span></div><div class="rows">%s</div></div>'
            % (esc(cat["name"]), cat["score"], cat["weight"], "".join(rows))
        )

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>%(app)s 体检报告 · %(hostname)s</title>
<style>%(css)s</style></head>
<body><div class="wrap">
<div class="top">
  <div class="brand"><span class="logo">◎</span><span>%(app)s</span><span class="ver">v%(ver)s</span></div>
  <div class="meta">%(hostname)s · %(os)s<br/>内核 %(kernel_k)s · %(arch)s · %(time)s</div>
</div>
<div class="card hero">
  <div class="gauge">%(gauge)s</div>
  <div>
    <div class="grade" style="color:%(color)s">%(grade)s · %(grade_note)s</div>
    <div class="hero-desc">五类共 %(total)d 项检查，扣分 %(deducted)s 分。%(cov_note)s<br/>
    本报告由 %(app)s 在本机离线生成，未上传任何数据。</div>
  </div>
</div>
<div class="cats">%(cats)s</div>
<div class="schema">%(pills)s</div>
%(priority)s
%(sections)s
<div class="foot">%(app)s v%(ver)s · 生成于 %(time)s · 零依赖单文件巡检工具<br/>
建议纳入日常巡检：opslens --html report.html --fail-under 80</div>
</div></body></html>""" % {
        "app": APP,
        "ver": VERSION,
        "css": _CSS,
        "hostname": esc(str(host.get("hostname") or "-")),
        "os": esc(str(host.get("os") or "-")),
        "kernel_k": esc(str(host.get("kernel") or "-")),
        "arch": esc(str(host.get("arch") or "-")),
        "time": esc(report["generated_at"]),
        "gauge": _gauge_svg(score, color),
        "color": color,
        "grade": esc(report["grade"]),
        "grade_note": esc(report["grade_note"]),
        "total": len(report["findings"]),
        "deducted": ("%g" % report["deducted"]),
        "cov_note": cov_note,
        "cats": "".join(cat_cards),
        "pills": "".join(pills),
        "priority": pri_html,
        "sections": "".join(sections),
    }


# --------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def load_thresholds(path):
    values = dict(DEFAULT_THRESHOLDS)
    if not path:
        return values
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        raise SystemExit("无法读取阈值文件 %s：%s" % (path, exc))
    if not isinstance(data, dict):
        raise SystemExit("阈值文件必须是 JSON 对象")
    for key, val in data.items():
        if key not in DEFAULT_THRESHOLDS:
            continue
        if isinstance(val, (int, float)):
            values[key] = float(val)
    return values


def run_audit(args):
    thresholds = load_thresholds(args.rules)
    started_wall = dt.datetime.now().astimezone()
    t0 = time.time()
    ctx = demo_context() if getattr(args, "demo", False) else collect_context(sample=args.sample)
    report = build_report(ctx, thresholds, started_at=started_wall, elapsed=time.time() - t0)
    report = filter_report(report, only=args.only, skip=args.skip)
    return report


def build_parser():
    parser = argparse.ArgumentParser(
        prog=APP,
        description="%s · %s —— 一条命令给服务器做全身 CT" % (APP, TAGLINE),
        epilog="示例：opslens --html report.html  |  opslens --json | opslens --only 安全,磁盘",
    )
    parser.add_argument("cmd", nargs="?", default="audit", choices=["audit", "version"], help="audit（默认）或 version")
    parser.add_argument("-j", "--json", action="store_true", help="输出 JSON")
    parser.add_argument("--html", metavar="PATH", help="生成自包含 HTML 报告")
    parser.add_argument("--md", metavar="PATH", help="生成 Markdown 报告")
    parser.add_argument("--no-color", action="store_true", help="关闭终端颜色")
    parser.add_argument("--only", help="只检查指定分类，逗号分隔：资源,磁盘,安全,服务,基础")
    parser.add_argument("--skip", help="跳过指定分类，逗号分隔")
    parser.add_argument("--rules", metavar="FILE", help="自定义阈值 JSON")
    parser.add_argument("--sample", type=float, default=0.6, metavar="SEC", help="CPU 采样时长（秒），默认 0.6")
    parser.add_argument("--demo", action="store_true", help="用内置演示数据生成报告（预览样式用，不读取本机）")
    parser.add_argument("--watch", type=float, default=0, metavar="SEC", help="循环巡检间隔（秒）")
    parser.add_argument("--fail-under", type=int, default=0, metavar="N", help="分数低于 N 时以退出码 1 结束")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出一行总分")
    parser.add_argument("-v", "--version", action="store_true", help="打印版本")
    return parser


def _split(value):
    if not value:
        return None
    return set(part.strip() for part in value.replace("，", ",").split(",") if part.strip())


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version or args.cmd == "version":
        print("%s %s" % (APP, VERSION))
        return 0

    args.only = _split(args.only)
    args.skip = _split(args.skip)

    while True:
        report = run_audit(args)
        use_color = (not args.no_color) and sys.stdout.isatty() and not args.json

        if args.json:
            print(render_json(report))
        elif args.quiet:
            print("%d/100 %s" % (report["score"], report["grade"]))
        else:
            print(render_console(report, use_color=use_color))

        if args.html:
            try:
                with open(args.html, "w", encoding="utf-8") as fh:
                    fh.write(render_html(report))
                if not args.quiet and not args.json:
                    print("  HTML 报告已写入：%s" % args.html)
            except Exception as exc:
                print("  写入 HTML 失败：%s" % exc, file=sys.stderr)

        if args.md:
            try:
                with open(args.md, "w", encoding="utf-8") as fh:
                    fh.write(render_markdown(report))
                if not args.quiet and not args.json:
                    print("  Markdown 报告已写入：%s" % args.md)
            except Exception as exc:
                print("  写入 Markdown 失败：%s" % exc, file=sys.stderr)

        if args.fail_under and report["score"] < args.fail_under:
            return 1

        if not args.watch:
            return 0
        time.sleep(max(1.0, args.watch))
        if not args.quiet:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
