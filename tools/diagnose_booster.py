# -*- coding: utf-8 -*-
"""booster「关闭定时任务后仍在自动刷量」生产环境自诊断脚本（只读，不做任何修改）。

用法：在本仓库任意检出目录内执行
    python tools/diagnose_booster.py
把完整输出发给开发者即可定位问题。

它随仓库 git 分发，因此有一个重要性质：如果生产机的 pyappify 数据目录里能找到本文件，
说明代码已经更新到位；找不到就说明更新根本没落地。

检查项：
  1. 本目录安装的代码是否包含 v1.4.0 修复标记（单进程 + 启动清场）
  2. 全机 python 进程扫描：谁在跑本应用的 app.py、来自哪个目录、启动时间
  3. 5678 端口归属：监听进程是否早于当前磁盘上的代码（典型孤儿/未重启特征）
  4. 直接询问运行中的实例：定时任务状态 / 任务列表来源 / webhook 监听
  5. 给出判定与建议
"""
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PORT = 5678
V14_MARKERS = ("debug=False", "_kill_stale_app_instances")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent


def line(ch="-", n=64):
    print(ch * n)


def _run_text(cmd, timeout=15):
    """执行命令并以容错方式解码输出（Windows 中文系统 netstat/tasklist 输出为 GBK，git 为 UTF-8）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return ""
    for enc in ("utf-8", "mbcs"):
        try:
            return r.stdout.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return r.stdout.decode("utf-8", "replace")


def ts(sec):
    try:
        return datetime.fromtimestamp(sec).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(sec)


def section(title):
    print()
    line("=")
    print(title)
    line("=")


# ── 1. 安装代码版本检查 ────────────────────────────────────────────────
def check_installed_code():
    section("1) 安装代码版本检查（本目录）")
    app_py = ROOT / "app.py"
    print(f"仓库目录: {ROOT}")
    if not app_py.exists():
        print("[!!] 本目录没有 app.py —— 请在应用的数据目录（pyappify 克隆的仓库）里运行本脚本")
        return None
    text = app_py.read_text(encoding="utf-8", errors="replace")
    missing = [m for m in V14_MARKERS if m not in text]
    if missing:
        print(f"[X] 安装的代码缺少 v1.4.0 修复标记: {missing}")
        print("    => 结论: pyappify 更新未生效，生产仍在跑旧代码（旧代码存在停止竞态与孤儿进程两个缺陷）。")
        print("    => 处置: 在 pyappify 中重新执行更新/重装，然后重启应用，再跑一次本脚本。")
        return False
    print("[OK] v1.4.0 修复标记齐全（单进程运行 + 启动清场逻辑已在磁盘代码中）")
    try:
        head = _run_text(["git", "-C", str(ROOT), "log", "-1", "--format=%h %ad %s", "--date=iso"], timeout=10)
        if head.strip():
            print(f"     检出版本: {head.strip()}")
    except Exception:
        pass
    mtime = app_py.stat().st_mtime
    print(f"     app.py 修改时间: {ts(mtime)}")
    return mtime


# ── 2/3. 进程与端口扫描 ───────────────────────────────────────────────
def _procs_psutil():
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline", "cwd", "create_time"]):
        try:
            i = p.info
            if "python" not in (i["name"] or "").lower():
                continue
            args = [str(a) for a in (i["cmdline"] or [])]
            if not any(a.lower().endswith("app.py") for a in args):
                continue
            out.append({
                "pid": i["pid"], "ppid": i["ppid"], "name": i["name"],
                "cmdline": " ".join(args), "cwd": i.get("cwd") or "",
                "create_time": i.get("create_time") or 0,
            })
        except Exception:
            continue
    return out


def _procs_netstat_fallback():
    """psutil 缺失时的降级方案：netstat 找 5678 监听 PID + tasklist 补 cmdline。"""
    out = []
    r = _run_text(["netstat", "-ano"])
    for ln in r.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3] == "LISTENING":
            pid = int(parts[-1])
            t = _run_text(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=10)
            name = (t.strip().split('","')[0].strip('"') if t.strip() else "unknown")
            out.append({"pid": pid, "ppid": 0, "name": name,
                        "cmdline": name, "cwd": "", "create_time": 0, "port_only": True})
    return out


def check_processes(app_mtime):
    section(f"2) python 进程扫描与 {PORT} 端口归属")
    try:
        import psutil  # noqa: F401
        procs = _procs_psutil()
    except ImportError:
        print("[!!] 未安装 psutil，降级为 netstat/tasklist（信息较少，建议在应用 venv 里运行）")
        procs = _procs_netstat_fallback()

    if not procs:
        print(f"[OK] 当前没有 python 进程在跑本应用的 app.py，{PORT} 端口监听者待下方确认")
    for p in procs:
        src = " (来自 netstat)" if p.get("port_only") else ""
        print(f"PID {p['pid']}  启动 {ts(p['create_time'])}  cwd={p['cwd'] or '未知'}{src}")
        print(f"      cmdline: {p['cmdline'][:160]}")
        if app_mtime and p["create_time"] and p["create_time"] < app_mtime:
            print("      [!!] 该进程启动时间早于磁盘上的 app.py —— 它在跑旧代码（典型孤儿进程特征）")

    listener_pid = None
    r = _run_text(["netstat", "-ano"])
    for ln in r.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3] == "LISTENING":
            listener_pid = int(parts[-1])
    if listener_pid:
        print(f"[OK] {PORT} 端口监听者 PID = {listener_pid}")
        match = [p for p in procs if p["pid"] == listener_pid]
        if match and match[0]["cwd"] and Path(match[0]["cwd"]).resolve() != ROOT:
            print(f"[!!] 监听实例来自其它目录: {match[0]['cwd']}（本脚本所在目录是 {ROOT}）")
            print("     => 机器上可能装有多个副本/多个 pyappify profile，互相抢 5678。")
    else:
        print(f"[--] {PORT} 端口当前无监听（应用可能没在运行）")
    return listener_pid, procs


# ── 4. 询问运行中的实例 ───────────────────────────────────────────────
def _get(path):
    url = f"http://127.0.0.1:{PORT}{path}"
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"__error__": str(e)}


def check_running_state():
    section("3) 运行中实例的自述（直接读它的接口）")
    sched = _get("/api/booster/schedule/status")
    if "__error__" in sched:
        print(f"[X] 无法访问 http://127.0.0.1:{PORT} （{sched['__error__']}）")
        print("    => 应用没在运行；若第 2 步发现监听进程，说明该进程没有 HTTP 响应（异常状态），截图发开发者。")
        return
    print(f"定时任务: running={sched.get('running')}  run_count={sched.get('run_count')}  "
          f"last_run={ts(sched['last_run']) if sched.get('last_run') else '无'}")
    at = sched.get("active_tasks") or []
    print(f"定时活跃子任务: {len(at)} 个")
    for t in at[:10]:
        print(f"    {t.get('bv')}  {t.get('status')}  {t.get('title', '')[:30]}")

    tasks = _get("/api/booster/tasks")
    if "__error__" not in tasks and isinstance(tasks, dict):
        alive = {k: v for k, v in tasks.items() if v.get("status") not in ("completed", "error", "cancelled")}
        sched_src = {k: v for k, v in alive.items() if v.get("log_target") == "booster-schedule"}
        manual_src = {k: v for k, v in alive.items() if not v.get("log_target")}
        print(f"未结束的 booster 任务: {len(alive)} 个（定时来源 {len(sched_src)}，手动/webhook 来源 {len(manual_src)}）")
        now = time.time()
        for k, v in alive.items():
            age = f"{int(now - v['start'])}s 前" if v.get("start") else "?"
            print(f"    [{k}] {v.get('status')}  start={age}  bv={v.get('bv')}  "
                  f"来源={'定时' if v.get('log_target') else '手动/webhook'}")
        if alive and not sched.get("running"):
            newest = max((v.get("start") or 0) for v in alive.values())
            if now - newest < 300:
                print("[X] 定时显示未运行，但 5 分钟内仍有新任务产生 —— 若实例是旧代码即为已知竞态；")
                print("    若确认代码已更新到 v1.4.0 且只有一个进程，请把本输出发开发者。")

    wh = _get("/api/booster/webhook/state")
    if "__error__" not in wh:
        print(f"webhook 监听: {'开启（有外部推送来源时注意）' if wh.get('enabled') else '关闭'}")


def verdict():
    section("4) 结论与建议（按顺序执行）")
    print("1. 若第 1 步显示缺 v1.4.0 标记 → 在 pyappify 里重新更新到 v1.4.0 并重启应用。")
    print("2. 若第 2 步发现多个 app.py 进程或监听进程早于磁盘代码 → 结束所有相关 python 进程")
    print("   （管理员 PowerShell：Stop-Process -Id <PID> -Force；或直接重启电脑），")
    print("   然后重新启动应用，再跑一次本脚本确认只剩一个进程。")
    print("3. 若定时确实在跑（running=true）→ 在界面里关闭；关闭后若仍有新任务，重复第 2 步。")
    print("4. 播放量上涨也可能来自「模拟播放」模块或 webhook 推送，注意区分来源。")
    print("把本脚本完整输出反馈给开发者可获得精确定位。")


def main():
    print("booster 自动刷量问题 · 生产环境自诊断")
    print(f"时间: {ts(time.time())}")
    mtime = check_installed_code()
    _, _ = check_processes(mtime)
    check_running_state()
    verdict()


if __name__ == "__main__":
    main()
