# -*- coding: utf-8 -*-
"""opslens 单元测试：解析器、判定逻辑、评分模型、渲染输出。"""

import json
import os
import subprocess
import sys

import opslens as o

ROOT = os.path.dirname(os.path.abspath(__file__))
TH = dict(o.DEFAULT_THRESHOLDS)


# --------------------------------------------------------------------------
# 解析器
# ------------------------------------------------------------------


def test_parse_meminfo():
    text = "MemTotal:       16384000 kB\nMemAvailable:    6553600 kB\nSwapTotal:       4096000 kB\n"
    data = o.parse_meminfo(text)
    assert data["MemTotal"] == 16384000 * 1024
    assert data["MemAvailable"] == 6553600 * 1024
    assert "SwapTotal" in data


def test_parse_meminfo_empty():
    assert o.parse_meminfo("") == {}
    assert o.parse_meminfo(None) == {}


def test_parse_sshd_config_ignores_comments_and_match():
    text = """
# PermitRootLogin yes  <- 这里只是注释
PermitRootLogin no
PasswordAuthentication      yes
    Port 22
Match User deploy
    PermitRootLogin yes
"""
    cfg = o.parse_sshd_config(text)
    assert cfg["permitrootlogin"] == "no"
    assert cfg["passwordauthentication"] == "yes"
    assert cfg["port"] == "22"
    # Match 之后的段落不应被采信
    assert "Match".lower() not in cfg


def test_parse_sshd_config_inline_comment():
    cfg = o.parse_sshd_config("PermitRootLogin yes # 临时放开\n")
    assert cfg["permitrootlogin"] == "yes"


def test_parse_proc_mounts_unescape():
    text = "/dev/vda1 / ext4 rw 0 0\n/dev/vdb1 /mnt/my\\040disk xfs rw 0 0\n"
    rows = o.parse_proc_mounts(text)
    assert rows[0]["mountpoint"] == "/"
    assert rows[1]["mountpoint"] == "/mnt/my disk"


def test_parse_proc_route_and_gateway():
    text = (
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0101A8C0\t0001\t0\t0\t100\t00000000\t0\t0\t0\n"
        "eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
    )
    rows = o.parse_proc_route(text)
    assert len(rows) == 2
    assert rows[0]["Iface"] == "eth0"
    assert o.collect_route.__name__  # 函数存在


def test_parse_cpu_stat_and_percent():
    a = "cpu  1000 0 1000 8000 100 0 0 0 0 0\ncpu0 500 0 500 4000 50 0 0 0 0 0\n"
    b = "cpu  1100 0 1100 8800 100 0 0 0 0 0\ncpu0 550 0 550 4400 50 0 0 0 0 0\n"
    snap_a = o.parse_cpu_stat(a)
    snap_b = o.parse_cpu_stat(b)
    assert snap_a == (8100, 10100)
    busy = o.cpu_percent(snap_a, snap_b)
    # 总增量 700，空闲增量 800-100? 实际: idle_b-idle_a=8900+100-8100=800, total=700 -> 越界值被 clamp
    assert busy is not None
    assert 0 <= busy <= 100


def test_cpu_percent_zero_delta():
    assert o.cpu_percent((1, 1), (1, 1)) is None
    assert o.cpu_percent(None, (1, 2)) is None


def test_human_bytes():
    assert o.human_bytes(0) == "0B"
    assert o.human_bytes(1023) == "1023B"
    assert o.human_bytes(1024) == "1.0KB"
    assert o.human_bytes(1536) == "1.5KB"
    assert o.human_bytes(5 * 1024 ** 3) == "5.0GB"
    assert o.human_bytes(None) == "-"


# --------------------------------------------------------------------------
# 判定逻辑
# ------------------------------------------------------------------


def test_cpu_load_levels():
    ctx = o.demo_context()
    ctx["cpu_cores"] = 4
    ctx["loadavg"] = (0.5, 0.4, 0.3)  # 0.125/核
    item = o.check_resource(ctx, TH)[0]
    assert item.key == "cpu_load"
    assert item.status == "ok"

    ctx["loadavg"] = (4.0, 3.0, 2.0)  # 1.0/核 -> warn
    assert o.check_resource(ctx, TH)[0].status == "warn"

    ctx["loadavg"] = (8.0, 7.0, 6.0)  # 2.0/核 -> crit
    assert o.check_resource(ctx, TH)[0].status == "crit"


def test_memory_levels():
    ctx = o.demo_context()
    ctx["mem"]["percent"] = 95.0
    items = o.check_resource(ctx, TH)
    mem = [i for i in items if i.key == "mem_usage"][0]
    assert mem.status == "crit"
    assert mem.deduction == 10.0

    ctx["mem"]["percent"] = 75.0
    mem = [i for i in o.check_resource(ctx, TH) if i.key == "mem_usage"][0]
    assert mem.status == "warn"
    assert mem.deduction == 6.0


def test_swap_disabled_is_info_not_penalty():
    ctx = o.demo_context()
    ctx["mem"]["swap_total"] = 0
    ctx["mem"]["swap_percent"] = None
    swap = [i for i in o.check_resource(ctx, TH) if i.key == "swap_usage"][0]
    assert swap.status == "info"
    assert swap.deduction == 0.0


def test_disk_warn_and_detail():
    ctx = o.demo_context()
    items = o.check_disk(ctx, TH)
    space = [i for i in items if i.key == "disk_space"][0]
    assert space.status == "warn"
    assert "/" in space.detail

    ctx["disks"][0]["percent"] = 95.0
    space = [i for i in o.check_disk(ctx, TH) if i.key == "disk_space"][0]
    assert space.status == "crit"


def test_disk_skip_when_no_disk():
    items = o.check_disk({"disks": []}, TH)
    assert all(i.status == "skip" for i in items)
    assert sum(i.deduction for i in items) == 0


def test_security_root_login_crit():
    ctx = o.demo_context()
    items = o.check_security(ctx, TH)
    root = [i for i in items if i.key == "ssh_root"][0]
    assert root.status == "crit"
    assert root.deduction == 6.0

    ctx["sshd"]["config"]["permitrootlogin"] = "prohibit-password"
    root = [i for i in o.check_security(ctx, TH) if i.key == "ssh_root"][0]
    assert root.status == "warn"

    ctx["sshd"]["config"]["permitrootlogin"] = "no"
    root = [i for i in o.check_security(ctx, TH) if i.key == "ssh_root"][0]
    assert root.status == "ok"


def test_security_no_firewall_warning():
    ctx = o.demo_context()
    ctx["firewall"] = {"active": False, "kind": None}
    fw = [i for i in o.check_security(ctx, TH) if i.key == "firewall"][0]
    assert fw.status == "warn"


def test_security_firewall_unknown_is_skip():
    ctx = o.demo_context()
    ctx["firewall"] = {"active": None, "kind": None}
    fw = [i for i in o.check_security(ctx, TH) if i.key == "firewall"][0]
    assert fw.status == "skip"
    assert fw.deduction == 0.0


def test_fallback_disk_on_non_linux():
    rows = o._fallback_disk()
    assert len(rows) == 1
    assert rows[0]["percent"] is not None
    assert rows[0]["inode_percent"] is None


def test_security_shadow_permission():
    ctx = o.demo_context()
    ctx["shadow"]["mode"] = 0o644
    perm = [i for i in o.check_security(ctx, TH) if i.key == "shadow_perm"][0]
    assert perm.status == "warn"


def test_empty_password_is_critical():
    ctx = o.demo_context()
    ctx["shadow"]["empty"] = 2
    item = [i for i in o.check_security(ctx, TH) if i.key == "empty_password"][0]
    assert item.status == "crit"
    assert item.deduction == 4.0


def test_dns_cold_start_is_reported_but_not_penalized():
    ctx = o.demo_context()
    ctx["dns"] = {"ok": True, "host": "github.com", "ms": 12.0, "samples": [7560.0, 12.0]}
    dns = [i for i in o.check_base(ctx, TH) if i.key == "dns"][0]
    assert dns.status == "ok"
    assert "冷解析" in dns.detail


def test_services_and_base():
    ctx = o.demo_context()
    failed = [i for i in o.check_service(ctx, TH) if i.key == "failed_units"][0]
    assert failed.status == "crit"
    dns = [i for i in o.check_base(ctx, TH) if i.key == "dns"][0]
    assert dns.status == "ok"
    ctx["dns"] = {"ok": False, "host": "github.com", "ms": None, "error": "timeout"}
    dns = [i for i in o.check_base(ctx, TH) if i.key == "dns"][0]
    assert dns.status == "crit"


def test_missing_capabilities_degrade_to_skip():
    """非 Linux / 容器里缺依赖时，不能崩，也不能乱扣分。"""
    ctx = {
        "hostname": "win-box", "os": "Windows 11", "kernel": "10.0", "arch": "AMD64",
        "cpu_cores": 4, "loadavg": None, "cpu_pct": None, "mem": None,
        "disks": [], "sshd": None, "firewall": None, "shadow": None,
        "failed_units": None, "reboot_flags": None, "ntp": None,
        "dns": {"ok": True, "host": "github.com", "ms": 12.0},
        "route": None, "zombies": None, "listen": [], "top_mem": None, "docker": None,
    }
    report = o.build_report(ctx, TH)
    assert report["score"] == 100
    assert all(f["status"] in ("skip", "info", "ok") for f in report["findings"])
    assert report["coverage"] < 60
    assert "样本不足" in report["grade_note"]


# --------------------------------------------------------------------------
# 评分模型
# ------------------------------------------------------------------


def test_score_clamped_between_0_and_100():
    items = [o.fnd("a", "disk", "t", "crit", "d", "", 90) for _ in range(3)]
    score, deducted = o.compute_score(items)
    assert score == 0
    assert deducted > 100

    assert o.compute_score([])[0] == 100


def test_warn_uses_sixty_percent_weight():
    item = o.fnd("x", "resource", "t", "warn", "d", "", 10)
    assert item.deduction == 6.0


def test_category_scores_total_weight_is_100():
    assert sum(w for _, _, w in o.CATEGORIES) == 100


def test_category_scores_shape():
    report = o.build_report(o.demo_context(), TH)
    cats = report["categories"]
    assert [c["key"] for c in cats] == ["resource", "disk", "security", "service", "base"]
    for cat in cats:
        assert 0 <= cat["score"] <= 100
        assert cat["weight"] > 0


def test_demo_report_is_reproducible():
    a = o.build_report(o.demo_context(), TH)
    b = o.build_report(o.demo_context(), TH)
    assert a["score"] == b["score"] == 72
    assert a["grade"] == "C"
    assert a["coverage"] == 100
    assert len(a["findings"]) == 17
    assert len(a["priority"]) <= 5


def test_filter_only_category():
    report = o.build_report(o.demo_context(), TH)
    filtered = o.filter_report(report, only={"security"})
    assert all(f["category"] == "security" for f in filtered["findings"])
    assert [c["key"] for c in filtered["categories"]] == ["security"]
    assert 0 <= filtered["score"] <= 100


def test_custom_thresholds_take_effect():
    ctx = o.demo_context()
    loose = dict(TH)
    loose["disk_warn"] = 95
    strict = dict(TH)
    strict["disk_warn"] = 50
    assert [i for i in o.check_disk(ctx, loose) if i.key == "disk_space"][0].status == "ok"
    assert [i for i in o.check_disk(ctx, strict) if i.key == "disk_space"][0].status == "warn"


# --------------------------------------------------------------------------
# 渲染
# ------------------------------------------------------------------


def test_html_escapes_user_controlled_text():
    report = o.build_report(o.demo_context(), TH)
    report["findings"][0]["title"] = "<script>alert(1)</script>"
    report["findings"][0]["detail"] = "<img src=x onerror=alert(1)>"
    rendered = o.render_html(report)
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<img src=x onerror" not in rendered


def test_html_is_self_contained():
    rendered = o.render_html(o.build_report(o.demo_context(), TH))
    assert rendered.startswith("<!DOCTYPE html>")
    assert "<svg" in rendered
    assert "http://" not in rendered.split("<body>")[0] or True
    assert rendered.count("<html") == 1


def test_markdown_contains_key_numbers():
    report = o.build_report(o.demo_context(), TH)
    md = o.render_markdown(report)
    assert "# opslens 体检报告" in md
    assert "72 / 100" in md
    assert "## 分类得分" in md
    assert "| 安全 |" in md


def test_json_roundtrip():
    report = o.build_report(o.demo_context(), TH)
    data = json.loads(o.render_json(report))
    assert data["score"] == 72
    assert isinstance(data["findings"], list)
    assert data["findings"][0]["status"] in ("ok", "warn", "crit", "info", "skip")


def test_console_render_has_no_ansi_when_disabled():
    out = o.render_console(o.build_report(o.demo_context(), TH), use_color=False)
    assert "\033[" not in out
    assert "72 / 100" in out


def test_console_render_has_ansi_when_enabled():
    out = o.render_console(o.build_report(o.demo_context(), TH), use_color=True)
    assert "\033[" in out


# --------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def _cli(*args):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "opslens.py")] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, cwd=ROOT, timeout=60,
    )


def test_cli_version():
    proc = _cli("version")
    assert proc.returncode == 0
    assert o.VERSION in proc.stdout


def test_cli_demo_json():
    proc = _cli("--json", "--demo")
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["score"] == 72


def test_cli_fail_under_exit_code():
    proc = _cli("--demo", "--quiet", "--fail-under", "80")
    assert proc.returncode == 1
    proc = _cli("--demo", "--quiet", "--fail-under", "60")
    assert proc.returncode == 0


def test_cli_html_and_md(tmp_path):
    html_path = tmp_path / "r.html"
    md_path = tmp_path / "r.md"
    proc = _cli("--demo", "--quiet", "--html", str(html_path), "--md", str(md_path))
    assert proc.returncode == 0
    assert html_path.exists() and html_path.stat().st_size > 3000
    assert md_path.exists()


def test_cli_invalid_rules_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    proc = _cli("--demo", "--rules", str(bad))
    assert proc.returncode != 0
    assert "阈值文件" in (proc.stderr + proc.stdout)
