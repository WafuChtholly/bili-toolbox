"""
B站工具箱 — 统一 WebUI
整合五大场景：自动互动 / 播放量提升(proxy) / 车机长线 / 直播间红包助手 / 话题助手
"""
# 兼容 Python 3.8 (Win7)：list[str] / X | None 等注解语法延迟求值
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

# ---------------------------------------------------------------------------
# 路径 & 导入设置
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
# 注意：bili-booster 目录不能直接加到 sys.path，否则其 app.py 会与本文件冲突
# booster 模块在启动时通过 importlib 一次性加载（见 booster 章节）
sys.path.insert(0, str(ROOT / "bili-auto"))
sys.path.insert(0, str(ROOT / "bili-redpocket"))
sys.path.insert(0, str(ROOT / "bili-medal"))

# Windows 控制台安全输出：避免 print() 因编码/fd 问题抛 [Errno 22] 导致业务中断
class _SafeStream:
    """包装 sys.stdout/stderr，write/flush 失败时静默忽略，不影响业务逻辑。"""
    def __init__(self, original):
        self._orig = original
    def write(self, s):
        try:
            self._orig.write(s)
        except Exception:
            pass
    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass
    def reconfigure(self, *a, **kw):
        try:
            self._orig.reconfigure(*a, **kw)
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self._orig, name)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.stdout = _SafeStream(sys.stdout)
    sys.stderr = _SafeStream(sys.stderr)

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)


# API 路由统一返回 JSON 错误，避免前端收到 HTML 页面导致 JSON 解析失败
@app.errorhandler(404)
def _api_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "message": f"接口不存在: {request.path}，请更新到最新版本"}), 404
    return e


@app.errorhandler(500)
def _api_500(e):
    if request.path.startswith("/api/"):
        return jsonify({"success": False, "message": f"服务器内部错误: {e}"}), 500
    return e


# =========================================================================
#  一、通用工具
# =========================================================================

# 简单的内存日志存储，前端轮询拉取
log_buffers: dict[str, list[str]] = {}
log_lock = threading.Lock()
# 每个任务日志最多保留的行数，超出丢弃最早的，防止长时间任务内存无限增长
LOG_MAX_LINES = 2000


def _append_log(task_id: str, line: str):
    with log_lock:
        buf = log_buffers.setdefault(task_id, [])
        buf.append(line)
        if len(buf) > LOG_MAX_LINES:
            del buf[:len(buf) - LOG_MAX_LINES]


def _get_log(task_id: str) -> str:
    with log_lock:
        return "\n".join(line.rstrip('\n') for line in log_buffers.get(task_id, []))


class TaskLogHandler(logging.Handler):
    """线程安全的日志 Handler，直接写入 log_buffers 而非 sys.stdout。
    解决多线程任务 sys.stdout 全局共享导致日志串台的问题。
    """
    def __init__(self, task_id: str, prefix: str = ""):
        super().__init__()
        self.task_id = task_id
        self.prefix = prefix

    def emit(self, record):
        try:
            msg = self.format(record)
            _append_log(self.task_id, msg)
        except Exception:
            pass


# =========================================================================
#  二、B站自动互动 (bili-auto)
# =========================================================================

auto_task_status = {}   # task_id -> {"status": ..., "start": ..., "end": ...}
auto_stop_events = {}   # task_id -> threading.Event

# 定时任务状态
_auto_schedule = {
    "running": False,
    "thread": None,
    "stop_event": threading.Event(),
    "last_run": None,
    "next_run": None,
    "run_count": 0,
}
_auto_run_lock = threading.Lock()  # 防止 run_once 并发执行

AUTO_CONFIG_DIR = ROOT / "data"
AUTO_CONFIG_FILE = AUTO_CONFIG_DIR / "auto_config.yaml"

# redpocket 配置统一放到 data 目录，避免 cookie 泄露在源码目录
CONFIG_DIR = ROOT / "data"
REDPOCKET_CONFIG = CONFIG_DIR / "redpocket_config.yaml"
REDPOCKET_OLD_CONFIG = ROOT / "bili-redpocket" / "config.yaml"


def _ensure_redpocket_config():
    """迁移 redpocket 旧配置到 data 目录"""
    if not REDPOCKET_CONFIG.exists() and REDPOCKET_OLD_CONFIG.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(REDPOCKET_OLD_CONFIG), str(REDPOCKET_CONFIG))
        print(f"[REDPOCKET] 已迁移旧配置到: {REDPOCKET_CONFIG}")


def _load_auto_config():
    import yaml
    if AUTO_CONFIG_FILE.exists():
        with open(AUTO_CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    # 迁移：旧配置存在时自动复制到新位置
    old_cfg = ROOT / "bili-auto" / "config.yaml"
    if old_cfg.exists():
        import shutil
        AUTO_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_cfg, AUTO_CONFIG_FILE)
        print(f"[AUTO] 已迁移旧配置到: {AUTO_CONFIG_FILE}")
        with open(AUTO_CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_auto_config(cfg):
    import yaml
    AUTO_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUTO_CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


def _run_auto_task(task_id: str, stop_event: threading.Event):
    """在后台线程中运行 bili-auto.run_once()，通过 TaskLogHandler 直接写日志缓冲区。"""
    # 尝试获取锁，防止并发执行
    if not _auto_run_lock.acquire(blocking=False):
        auto_task_status[task_id]["status"] = "error"
        _append_log(task_id, "[SYSTEM] 已有任务在运行，请等待完成后再试")
        auto_task_status[task_id]["end"] = time.time()
        return

    auto_task_status[task_id]["status"] = "running"

    # 使用 TaskLogHandler 直接写入 log_buffers，不再重定向 sys.stdout
    task_handler = TaskLogHandler(task_id)
    task_handler.setFormatter(logging.Formatter("[%(asctime)s] [AUTO] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    interacted_bvids = []

    try:
        from core import run_once

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_once(
                stop_event,
                on_interact=lambda bv: interacted_bvids.append(bv),
                extra_handler=task_handler,
            ))
        finally:
            loop.close()

        # 互动完成后，在状态变 completed 之前播放（保持前端轮询）
        if not stop_event.is_set() and interacted_bvids:
            cfg = _load_auto_config()
            if cfg.get("actions", {}).get("play_once"):
                _append_log(task_id, f"[SYSTEM] 播放一次已启用，将播放 {len(interacted_bvids)} 个视频...")
                _try_play_bvids(task_id, stop_event, interacted_bvids)

        auto_task_status[task_id]["status"] = "completed"

    except Exception as e:
        _append_log(task_id, f"[ERROR] {e}")
        auto_task_status[task_id]["status"] = "error"
    finally:
        auto_task_status[task_id]["end"] = time.time()
        _auto_run_lock.release()


def _try_play_bvids(task_id: str, stop_event: threading.Event, bvids: list[str]):
    """用匿名车机通道逐个播放给定的 BV 列表（每次一轮）。"""
    if not bvids:
        _append_log(task_id, "[SYSTEM] 无 BV 可播放，跳过")
        return

    for i, bvid in enumerate(bvids):
        if stop_event.is_set():
            _append_log(task_id, "[SYSTEM] 收到停止信号，停止播放")
            break
        _append_log(task_id, f"[SYSTEM] 车机播放 ({i+1}/{len(bvids)}): {bvid}")
        try:
            info = car_channel.fetch_video_info(bvid)
            before = int(info["stat"]["view"])
            r = car_channel.run_session(info, stop_event=stop_event)
            after = None
            try:
                after = car_channel.fetch_views(bvid)
            except Exception:
                pass
            delta = f'{after - before:+d}' if after is not None else '?'
            _append_log(task_id, f"[CAR] {bvid} click={r['click'][0]}/{r['click'][1]} "
                                  f"hb={'/'.join(str(x) for x in r['hb'])} views={delta}")
        except Exception as e:
            _append_log(task_id, f"[SYSTEM] 播放 {bvid} 失败: {type(e).__name__}: {e}")
        if not stop_event.is_set() and i < len(bvids) - 1:
            wait = 3
            _append_log(task_id, f"[SYSTEM] 等待 {wait} 秒后播放下一个...")
            if stop_event.wait(wait):
                break

    _append_log(task_id, "[SYSTEM] 播放完成")


def _run_auto_once(stop_event: threading.Event, log_target: str = None) -> tuple:
    """Execute one run_once cycle. log_target: also append output to this buffer."""
    # 尝试获取锁，防止并发执行
    if not _auto_run_lock.acquire(blocking=False):
        _append_log(log_target or "schedule", "[SYSTEM] 已有任务在运行，跳过本次执行")
        return None, []

    tid = str(uuid.uuid4())[:8]
    auto_task_status[tid] = {"status": "running", "start": time.time(), "end": None}
    with log_lock:
        log_buffers[tid] = []

    # 使用 TaskLogHandler 直接写入 log_buffers
    task_handler = TaskLogHandler(tid)
    task_handler.setFormatter(logging.Formatter("[%(asctime)s] [AUTO] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    # 如果有 log_target，额外写一份到目标缓冲区
    target_handler = None
    if log_target and log_target != tid:
        target_handler = TaskLogHandler(log_target)
        target_handler.setFormatter(logging.Formatter("[%(asctime)s] [AUTO] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    try:
        from core import run_once as _run_once
        interacted_bvids = []
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        extra_handlers = [task_handler] + ([target_handler] if target_handler else [])
        try:
            loop.run_until_complete(_run_once(
                stop_event,
                on_interact=lambda bv: interacted_bvids.append(bv),
                extra_handler=extra_handlers,
            ))
        finally:
            loop.close()
        auto_task_status[tid]["status"] = "completed"
        return tid, interacted_bvids
    except Exception as e:
        _append_log(tid, f"[ERROR] {e}")
        if log_target:
            _append_log(log_target, f"[ERROR] {e}")
        auto_task_status[tid]["status"] = "error"
        return tid, []
    finally:
        auto_task_status[tid]["end"] = time.time()
        _auto_run_lock.release()

    return tid, []


def _schedule_loop():
    """定时任务主循环"""
    while not _auto_schedule["stop_event"].is_set():
        cfg = _load_auto_config()
        interval = cfg.get("schedule", {}).get("interval_minutes", 30) * 60

        _append_log("schedule", f"=== 定时任务执行 (第{_auto_schedule['run_count']+1}次) ===")
        run_tid, schedule_interacted_bvids = _run_auto_once(_auto_schedule["stop_event"], log_target="schedule")
        # 定时任务也检查 play_once
        if not _auto_schedule["stop_event"].is_set() and schedule_interacted_bvids:
            cfg2 = _load_auto_config()
            if cfg2.get("actions", {}).get("play_once"):
                _append_log("schedule", f"[SYSTEM] 播放一次已启用，将播放 {len(schedule_interacted_bvids)} 个视频...")
                _try_play_bvids("schedule", _auto_schedule["stop_event"], schedule_interacted_bvids)
        _auto_schedule["run_count"] += 1
        _auto_schedule["last_run"] = time.time()
        _auto_schedule["next_run"] = time.time() + interval

        _append_log("schedule", f"=== 等待 {interval//60} 分钟后执行下一次 ===")

        # 分段等待，以便能快速响应停止
        for _ in range(interval):
            if _auto_schedule["stop_event"].is_set():
                break
            time.sleep(1)

    _auto_schedule["running"] = False
    _append_log("schedule", "定时任务已停止")


# ---- Auto Login API ----
AUTO_CRED_FILE = ROOT / "data" / "bili-auto" / "credential.json"


def _read_auto_cred():
    """读取 auto 凭证。"""
    if not AUTO_CRED_FILE.exists():
        return {}
    try:
        data = json.loads(AUTO_CRED_FILE.read_text(encoding="utf-8"))
        if data.get("sessdata"):
            return data
    except Exception:
        pass
    return {}


def _booster_view_cookie(cred) -> str:
    """把账号凭证拼成 view 接口用的 Cookie 头：机房 IP 带登录态可降低被 WAF 判 412 的概率。"""
    cred = cred or {}
    sessdata = cred.get("sessdata") or ""
    if not sessdata:
        return ""
    parts = [f"SESSDATA={sessdata}"]
    if cred.get("bili_jct"):
        parts.append(f"bili_jct={cred['bili_jct']}")
    uid = cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or ""
    if uid:
        parts.append(f"DedeUserID={uid}")
    return "; ".join(parts)


# ---- B站用户名查询（内存缓存 10 分钟） ----
_UNAME_CACHE: dict = {}

# ---- 监控 UID 昵称/头像缓存（uid -> (name, face, 时间戳)，6 小时） ----
_WATCH_USER_CACHE: dict = {}


def _fetch_bili_uname(sessdata: str, uid) -> str:
    """通过 nav 接口查询 UID 对应用户名；失败或无凭证返回空串。"""
    uid = str(uid or "")
    if not uid or not sessdata:
        return ""
    now = time.time()
    hit = _UNAME_CACHE.get(uid)
    if hit and now - hit[1] < 600:
        return hit[0]
    uname = ""
    try:
        import httpx
        resp = httpx.get(
            "https://api.bilibili.com/x/web-interface/nav",
            cookies={"SESSDATA": sessdata},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com/",
            },
            timeout=8,
        )
        uname = str((resp.json().get("data") or {}).get("uname") or "")
    except Exception:
        uname = ""
    if uname:
        _UNAME_CACHE[uid] = (uname, now)
    return uname


# ---- Auto 多账号管理 API ----

@app.route("/api/auto/accounts")
def auto_accounts_list():
    """列出所有已登录账号（主账号在前）。"""
    try:
        from core import list_accounts, load_enabled_credentials, update_account_name
        accounts = list_accounts()
        # 昵称为空的账号按需回填（登录时未写入的场景）
        missing = [a for a in accounts if not a.get("name")]
        if missing:
            cred_map = {uid: cred for uid, cred, _m in load_enabled_credentials()}
            for a in missing:
                cred = cred_map.get(a["uid"])
                if not cred:
                    continue
                try:
                    name = _fetch_bili_uname(cred.sessdata, a["uid"])
                except Exception:
                    name = ""
                if name:
                    update_account_name(a["uid"], name)
                    a["name"] = name
        return jsonify({"success": True, "accounts": accounts})
    except Exception as e:
        return jsonify({"success": False, "message": str(e), "accounts": []})


@app.route("/api/auto/accounts/toggle", methods=["POST"])
def auto_accounts_toggle():
    """启用/停用指定账号。"""
    data = request.json or {}
    try:
        from core import set_account_enabled
        ok = set_account_enabled(str(data.get("uid", "")), bool(data.get("enabled", True)))
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/accounts/remove", methods=["POST"])
def auto_accounts_remove():
    """删除指定账号。"""
    data = request.json or {}
    try:
        from core import remove_account
        ok = remove_account(str(data.get("uid", "")))
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/accounts/primary", methods=["POST"])
def auto_accounts_primary():
    """设置主账号（播放等复用单凭证的功能使用主账号）。"""
    data = request.json or {}
    try:
        from core import set_primary_account
        ok = set_primary_account(str(data.get("uid", "")))
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/login/config")
def auto_login_config():
    """检查 auto 模块登录状态"""
    try:
        data = _read_auto_cred()
        if data.get("sessdata"):
            uid = data.get("mid") or data.get("login_uid") or data.get("dedeuserid") or data.get("uid", "")
            return jsonify({"login_uid": str(uid) if uid else "",
                            "uname": _fetch_bili_uname(data.get("sessdata", ""), uid)})
    except Exception:
        pass
    return jsonify({"login_uid": "", "uname": ""})


@app.route("/api/auto/login/qrcode")
def auto_login_qrcode():
    """生成 B站扫码登录二维码（调用 core.py QR API）。"""
    try:
        from core import qr_generate
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(qr_generate())
        finally:
            loop.close()
        return jsonify({"success": True, **result})
    except Exception as e:
        return jsonify({"success": False, "message": f"生成二维码失败: {e}"})


@app.route("/api/auto/login/poll/<session_id>")
def auto_login_poll(session_id):
    """轮询 QR 登录状态（调用 core.py QR API）。"""
    try:
        from core import qr_poll
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(qr_poll(session_id))
        finally:
            loop.close()
        status = result.get("status", "error")
        if status == "success":
            return jsonify({"success": True, "login_uid": result.get("login_uid", "")})
        elif status == "expired":
            return jsonify({"success": False, "status": "expired", "message": result.get("message", "二维码已过期")})
        elif status == "scanned":
            # CONF 状态：已扫码待确认 → 前端 waiting 显示“已扫码，请在手机上确认”
            return jsonify({"success": False, "status": "waiting", "message": result.get("message", "已扫码，请确认")})
        elif status == "waiting":
            # SCAN 状态：未扫码 → 前端默认显示消息
            return jsonify({"success": False, "message": result.get("message", "等待扫码")})
        else:
            return jsonify({"success": False, "message": result.get("message", "未知错误")})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/run", methods=["POST"])
def auto_run():
    # 检查是否有任务正在运行
    for st in auto_task_status.values():
        if st["status"] == "running":
            return jsonify({"error": "已有任务在运行，请等待完成后再试"}), 409

    tid = str(uuid.uuid4())[:8]
    auto_task_status[tid] = {"status": "queued", "start": time.time(), "end": None}
    stop_event = threading.Event()
    auto_stop_events[tid] = stop_event
    with log_lock:
        log_buffers[tid] = []
    t = threading.Thread(target=_run_auto_task, args=(tid, stop_event), daemon=True)
    t.start()
    return jsonify({"task_id": tid})


@app.route("/api/auto/status/<task_id>")
def auto_status(task_id):
    st = auto_task_status.get(task_id)
    if not st:
        return jsonify({"error": "not found"}), 404
    return jsonify({**st, "output": _get_log(task_id)})


@app.route("/api/auto/stop", methods=["POST"])
def auto_stop():
    data = request.json or {}
    task_id = data.get("task_id")
    if task_id and task_id in auto_stop_events:
        auto_stop_events[task_id].set()
        _append_log(task_id, "[SYSTEM] 正在停止...")
        return jsonify({"success": True})
    # 停止所有运行中的任务
    stopped = 0
    for tid, st in auto_task_status.items():
        if st["status"] == "running" and tid in auto_stop_events:
            auto_stop_events[tid].set()
            _append_log(tid, "[SYSTEM] 正在停止...")
            stopped += 1
    return jsonify({"success": True, "stopped": stopped})


@app.route("/api/auto/config", methods=["GET"])
def auto_config_get():
    cfg = _load_auto_config()
    return jsonify(cfg)


@app.route("/api/auto/config", methods=["POST"])
def auto_config_save():
    data = request.json or {}
    cfg = _load_auto_config()
    cfg.update(data)
    _save_auto_config(cfg)
    return jsonify({"success": True})


@app.route("/api/auto/reply-texts", methods=["GET"])
def auto_reply_texts_get():
    """获取嘲讽/栅栏语录配置，优先读取自定义配置，否则返回内置默认。"""
    from core import (_WITTY_OPENS, _WITTY_BODIES, _WITTY_CLOSES,
                      _ZALAN_OPENS, _ZALAN_BODIES, _ZALAN_CLOSES)
    cfg = _load_auto_config()
    custom = cfg.get("reply_texts", {})
    return jsonify({
        "witty_opens": custom.get("witty_opens", _WITTY_OPENS),
        "witty_bodies": custom.get("witty_bodies", _WITTY_BODIES),
        "witty_closes": custom.get("witty_closes", _WITTY_CLOSES),
        "zalan_opens": custom.get("zalan_opens", _ZALAN_OPENS),
        "zalan_bodies": custom.get("zalan_bodies", _ZALAN_BODIES),
        "zalan_closes": custom.get("zalan_closes", _ZALAN_CLOSES),
        "has_custom": bool(custom and any(custom.get(k) for k in ("witty_opens", "witty_bodies", "witty_closes", "zalan_opens", "zalan_bodies", "zalan_closes"))),
    })


@app.route("/api/auto/reply-texts", methods=["POST"])
def auto_reply_texts_save():
    """保存自定义语录到配置文件。"""
    data = request.json or {}
    cfg = _load_auto_config()
    # 保存自定义语录
    reply_texts = {}
    for key in ("witty_opens", "witty_bodies", "witty_closes", "zalan_opens", "zalan_bodies", "zalan_closes"):
        val = data.get(key)
        if val is not None and isinstance(val, list):
            reply_texts[key] = val
    cfg["reply_texts"] = reply_texts
    _save_auto_config(cfg)
    return jsonify({"success": True})


@app.route("/api/auto/reply-texts/reset", methods=["POST"])
def auto_reply_texts_reset():
    """重置语录为内置默认（删除自定义配置）。"""
    cfg = _load_auto_config()
    cfg.pop("reply_texts", None)
    _save_auto_config(cfg)
    return jsonify({"success": True})


@app.route("/api/auto/following-list")
def auto_following_list():
    """拉取登录账号的关注列表，用于白名单勾选。
    返回: {success, followings: [{uid, name, face}, ...]}
    """
    cred_data = _read_auto_cred()
    sessdata = cred_data.get("sessdata", "")
    mid = cred_data.get("dedeuserid") or cred_data.get("mid") or cred_data.get("login_uid") or ""
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先扫码登录"})

    try:
        from bilibili_api import Credential
        from core import get_following_list

        credential = Credential(
            sessdata=sessdata,
            bili_jct=cred_data.get("bili_jct", ""),
            dedeuserid=str(mid),
            ac_time_value=cred_data.get("ac_time_value", ""),
        )
        loop = asyncio.new_event_loop()
        try:
            followings = loop.run_until_complete(get_following_list(credential, int(mid)))
        finally:
            loop.close()
        return jsonify({"success": True, "followings": followings})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/following-groups")
def auto_following_groups():
    """拉取登录账号的关注分组列表。
    返回: {success, groups: [{tagid, name, count}, ...]}
    """
    cred_data = _read_auto_cred()
    sessdata = cred_data.get("sessdata", "")
    if not sessdata:
        return jsonify({"success": False, "message": "请先扫码登录"})

    try:
        from core import get_following_groups
        return jsonify({"success": True, "groups": get_following_groups(sessdata)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/following-groups/members")
def auto_following_groups_members():
    """拉取指定关注分组的成员。query: tagid
    返回: {success, members: [{uid, name, face}, ...]}
    """
    cred_data = _read_auto_cred()
    sessdata = cred_data.get("sessdata", "")
    if not sessdata:
        return jsonify({"success": False, "message": "请先扫码登录"})

    tagid = request.args.get("tagid", "")
    if tagid == "":
        return jsonify({"success": False, "message": "缺少 tagid"})

    try:
        from core import get_group_members
        members = get_group_members(sessdata, int(tagid))
        return jsonify({"success": True, "members": members})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/schedule/start", methods=["POST"])
def auto_schedule_start():
    if _auto_schedule["running"]:
        return jsonify({"success": False, "message": "定时任务已在运行"})

    _auto_schedule["stop_event"] = threading.Event()
    _auto_schedule["running"] = True
    _auto_schedule["run_count"] = 0
    _auto_schedule["last_run"] = None
    with log_lock:
        log_buffers["schedule"] = []
    _append_log("schedule", "定时任务已启动")

    t = threading.Thread(target=_schedule_loop, daemon=True)
    t.start()
    return jsonify({"success": True})


@app.route("/api/auto/schedule/stop", methods=["POST"])
def auto_schedule_stop():
    if not _auto_schedule["running"]:
        return jsonify({"success": True, "message": "未在运行"})
    _auto_schedule["stop_event"].set()
    return jsonify({"success": True})


@app.route("/api/auto/schedule/status")
def auto_schedule_status():
    cfg = _load_auto_config()
    schedule_cfg = cfg.get("schedule", {})
    return jsonify({
        "running": _auto_schedule["running"],
        "interval_minutes": schedule_cfg.get("interval_minutes", 30),
        "last_run": _auto_schedule["last_run"],
        "run_count": _auto_schedule["run_count"],
        "log": _get_log("schedule"),
    })


# ---- 历史投稿互动模式 ----

history_task_status = {}   # task_id -> {"status": ..., "start": ..., "end": ...}
history_stop_events = {}   # task_id -> threading.Event


def _run_history_task(task_id: str, stop_event: threading.Event, target_uids: list[int], days: int):
    """在后台线程中运行历史投稿互动任务。"""
    history_task_status[task_id]["status"] = "running"

    task_handler = TaskLogHandler(task_id)
    task_handler.setFormatter(logging.Formatter("[%(asctime)s] [AUTO-HISTORY] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    interacted_bvids = []

    try:
        from core import run_history_interact

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_history_interact(
                target_uids=target_uids,
                days=days,
                stop_event=stop_event,
                on_interact=lambda bv: interacted_bvids.append(bv),
                extra_handler=task_handler,
            ))
        finally:
            loop.close()

        # 播放一次
        if not stop_event.is_set() and interacted_bvids:
            cfg = _load_auto_config()
            hist_actions = cfg.get("history_actions", cfg.get("actions", {}))
            if hist_actions.get("play_once"):
                _append_log(task_id, f"[SYSTEM] 播放一次已启用，将播放 {len(interacted_bvids)} 个视频...")
                _try_play_bvids(task_id, stop_event, interacted_bvids)

        history_task_status[task_id]["status"] = "completed"

    except Exception as e:
        _append_log(task_id, f"[ERROR] {e}")
        history_task_status[task_id]["status"] = "error"
    finally:
        history_task_status[task_id]["end"] = time.time()


@app.route("/api/auto/history/preview")
def auto_history_preview():
    """预览指定用户在时间范围内的投稿。参数: uids (逗号分隔), days"""
    uids_str = request.args.get("uids", "")
    days = int(request.args.get("days", 30))
    if not uids_str:
        return jsonify({"success": False, "message": "请提供用户 UID"})

    cred_data = _read_auto_cred()
    sessdata = cred_data.get("sessdata", "")
    mid = cred_data.get("dedeuserid") or cred_data.get("mid") or cred_data.get("login_uid") or ""
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先扫码登录"})

    try:
        from bilibili_api import Credential
        from core import get_user_videos_in_range

        credential = Credential(
            sessdata=sessdata,
            bili_jct=cred_data.get("bili_jct", ""),
            dedeuserid=str(mid),
            ac_time_value=cred_data.get("ac_time_value", ""),
        )

        uids = [int(u.strip()) for u in uids_str.split(",") if u.strip().isdigit()]
        if not uids:
            return jsonify({"success": False, "message": "UID 格式错误"})

        loop = asyncio.new_event_loop()
        all_videos = []
        try:
            for uid in uids:
                videos = loop.run_until_complete(get_user_videos_in_range(uid, credential, days))
                all_videos.extend(videos)
        finally:
            loop.close()

        # 加载已互动记录，标记哪些已互动过
        today = time.strftime("%Y-%m-%d")
        interacted_bvids = set()
        from pathlib import Path as _Path
        _ifile = _Path(__file__).resolve().parent / "data" / "bili-auto" / "interacted_bvids.json"
        if _ifile.exists():
            import json as _json
            _idata = _json.loads(_ifile.read_text(encoding="utf-8"))
            if _idata.get("_date") == today:
                interacted_bvids = set(_idata.get("bvids", []))

        for v in all_videos:
            v["interacted"] = v.get("bvid", "") in interacted_bvids

        return jsonify({"success": True, "videos": all_videos, "total": len(all_videos)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/auto/history/run", methods=["POST"])
def auto_history_run():
    """启动历史投稿互动任务。参数: {uids: [int], days: int}"""
    data = request.json or {}
    uids = data.get("uids", [])
    days = int(data.get("days", 30))
    if not uids:
        return jsonify({"error": "请提供目标用户 UID"}), 400

    tid = str(uuid.uuid4())[:8]
    history_task_status[tid] = {"status": "queued", "start": time.time(), "end": None}
    stop_event = threading.Event()
    history_stop_events[tid] = stop_event
    with log_lock:
        log_buffers[tid] = []
    t = threading.Thread(target=_run_history_task, args=(tid, stop_event, uids, days), daemon=True)
    t.start()
    return jsonify({"task_id": tid})


@app.route("/api/auto/history/status/<task_id>")
def auto_history_status(task_id):
    st = history_task_status.get(task_id)
    if not st:
        return jsonify({"error": "not found"}), 404
    return jsonify({**st, "output": _get_log(task_id)})


@app.route("/api/auto/history/stop", methods=["POST"])
def auto_history_stop():
    data = request.json or {}
    task_id = data.get("task_id")
    if task_id and task_id in history_stop_events:
        history_stop_events[task_id].set()
        _append_log(task_id, "[SYSTEM] 正在停止...")
        return jsonify({"success": True})
    # 停止所有运行中的历史任务
    stopped = 0
    for tid, st in history_task_status.items():
        if st["status"] == "running" and tid in history_stop_events:
            history_stop_events[tid].set()
            _append_log(tid, "[SYSTEM] 正在停止...")
            stopped += 1
    return jsonify({"success": True, "stopped": stopped})


# =========================================================================
#  三、B站播放量提升 (bili-booster)
# =========================================================================

# 启动时一次性加载 booster 模块，避免每次任务都 importlib 重载（非常慢）
import importlib.util as _ilu
_booster_spec = _ilu.spec_from_file_location("booster", str(ROOT / "bili-booster" / "booster.py"))
booster = _ilu.module_from_spec(_booster_spec)
_booster_spec.loader.exec_module(booster)

booster_tasks = {}
booster_lock = threading.Lock()

# ── 并发任务限流：最多同时跑 max_concurrent_boost_tasks 个任务，其余排队；上限可在配置页运行中调整 ──
DEFAULT_MAX_CONCURRENT_BOOST_TASKS = 3
MAX_CONCURRENT_BOOST_TASKS_CAP = 20


class _BoosterGate:
    """可动态调整上限的并发闸门（替代固定 Semaphore，支持运行中改并发数）。"""

    def __init__(self, limit: int):
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self._active = 0

    def set_limit(self, limit: int):
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()

    def get_limit(self) -> int:
        with self._cond:
            return self._limit

    def acquire(self):
        with self._cond:
            while self._active >= self._limit:
                self._cond.wait()
            self._active += 1

    def release(self):
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify()


_booster_gate = _BoosterGate(DEFAULT_MAX_CONCURRENT_BOOST_TASKS)

# ── Webhook（活动助手推送） ──
booster_webhook_enabled = False
booster_webhook_queue: list[str] = []   # 存放收到的 BV号
booster_webhook_lock = threading.Lock()


@app.route("/booster", methods=["POST"])
def booster_webhook():
    """接收外部推送的 BV号（活动助手等），格式：
    {"bvid": "BVxxx"} 或 {"bv": "BVxxx"} 或 {"bvid": ["BV1", "BV2"]}
    也支持纯字符串 "BVxxx"。
    """
    if not booster_webhook_enabled:
        return jsonify({"error": "webhook 未开启"}), 403

    data = request.json
    if data is None:
        # 尝试纯文本
        raw = request.data.decode("utf-8", errors="ignore").strip()
        if raw:
            bv_list = [raw]
        else:
            return jsonify({"error": "空数据"}), 400
    else:
        # 支持多种字段名
        raw_val = data.get("bvid") or data.get("bv") or data.get("bvids") or data.get("video_id") or data.get("videoId") or ""
        if isinstance(raw_val, list):
            bv_list = [str(v).strip() for v in raw_val if str(v).strip()]
        elif isinstance(raw_val, str):
            # 逗号分隔也支持
            bv_list = [v.strip() for v in raw_val.replace("，", ",").split(",") if v.strip()]
        else:
            bv_list = [str(raw_val).strip()]

    if not bv_list:
        return jsonify({"error": "未解析到 BV号"}), 400

    with booster_webhook_lock:
        for bv in bv_list:
            if bv not in booster_webhook_queue:
                booster_webhook_queue.append(bv)

    print(f"[WEBHOOK] 收到 BV号: {bv_list}")
    return jsonify({"success": True, "received": bv_list, "total_queued": len(booster_webhook_queue)})


@app.route("/api/booster/webhook/start", methods=["POST"])
def booster_webhook_start():
    global booster_webhook_enabled
    with booster_webhook_lock:
        booster_webhook_enabled = True
        booster_webhook_queue.clear()
    return jsonify({"success": True, "url": "/booster"})


@app.route("/api/booster/webhook/stop", methods=["POST"])
def booster_webhook_stop():
    global booster_webhook_enabled
    with booster_webhook_lock:
        booster_webhook_enabled = False
    return jsonify({"success": True})


@app.route("/api/booster/webhook/poll")
def booster_webhook_poll():
    """拉取 webhook 收到的 BV号（消费式：取完即清空队列）"""
    with booster_webhook_lock:
        bvs = list(booster_webhook_queue)
        booster_webhook_queue.clear()
    return jsonify({"bvs": bvs, "enabled": booster_webhook_enabled})


@app.route("/api/booster/webhook/state")
def booster_webhook_state():
    """查询 webhook 监听状态（不消费推送队列），供页面刷新后同步 UI。"""
    return jsonify({"enabled": booster_webhook_enabled})


# 镜像到 schedule 日志（定时任务页）时的降噪规则：
# 代理探测、分批点击、进度条这类过程细节只保留在单任务日志里；
# 多稿件并发时它们会以十几倍体量淹没真正有用的里程碑（轮次 / 结果 / 完成 / 错误）。
_BOOSTER_MIRROR_SKIP = (
    'filtering ',
    'filtered ',
    'sending clicks',
    'chunk ',
    'fetching info for',
    '[PROGRESS]',
)


def _booster_task_log_line(task_id: str, line: str):
    """把一行 booster 日志写入该任务自己的缓冲区，并镜像到 schedule 日志。

    booster 内部已给普通日志加了 `[BV]` 前缀（同一任务内多稿并发时区分归属），
    镜像时若已带前缀就不再重复添加。
    """
    with booster_lock:
        if booster_tasks.get(task_id, {}).get("status") == "cancelled":
            return
        buf = log_buffers.get(task_id)
        if buf is not None:
            buf.append(f'[BOOSTER] {line}\n')
        # 镜像子任务日志到 schedule 日志，便于在定时任务页跟踪执行进度
        st = booster_tasks.get(task_id, {})
        log_target = st.get("log_target")
        bv = st.get("bv", "")
    # 锁外追加 mirror，避免在 booster_lock 内长时间阻塞其他 booster 任务
    if log_target and log_target != task_id:
        if any(k in line for k in _BOOSTER_MIRROR_SKIP):
            return
        mirror = line if (bv and line.startswith(f'[{bv}]')) else f'[{bv}] {line}'
        _append_log(log_target, f'  └ {mirror}\n')


def _booster_task_log_progress(task_id: str, line: str):
    """更新进度条：替换缓冲区里最后一条 `[BOOSTER] [PROGRESS]` 行，没有则追加。

    进度条只留在单任务日志里，不镜像到 schedule 日志（高频刷新且信息量低）。
    """
    with booster_lock:
        if booster_tasks.get(task_id, {}).get("status") == "cancelled":
            return
        buf = log_buffers.get(task_id)
        if buf is None:
            return
        lines = ''.join(buf).splitlines()
        if not lines:
            buf.append(f'[BOOSTER] {line}\n')
            return
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith('[BOOSTER] [PROGRESS]'):
                lines[i] = f'[BOOSTER] {line}'
                buf.clear()
                buf.append('\n'.join(lines) + '\n')
                return
        buf.append(f'[BOOSTER] {line}\n')


# 多个任务并发写控制台时，stdout 只有一份：加锁保证单次 write 不被其它线程截断。
# 进度条用 \r 原地刷新且不带换行，后一条整行日志前补一个换行，避免粘在进度条同一行上。
_console_lock = threading.Lock()
_console_line_open = False


def _make_booster_logger(task_id: str):
    """生成绑定到指定任务的 booster 日志函数（注入 booster.main 的 log_fn）。

    每个任务一个实例，日志只写进自己的缓冲区，因此多任务并发不会串台；
    同时原样转发到终端，保留控制台实时输出与进度条的行内覆盖。
    """
    def _log(msg):
        global _console_line_open
        text = str(msg)
        if not text:
            return
        try:
            with _console_lock:
                if not text.startswith('\r') and _console_line_open:
                    sys.stdout.write('\n')
                if text.startswith('\r') or text.endswith('\n'):
                    sys.stdout.write(text)
                else:
                    sys.stdout.write(text + '\n')
                sys.stdout.flush()
                _console_line_open = not text.endswith('\n')
        except Exception:
            pass
        if '\r' in text:
            # booster 用 \r 覆盖同一行更新进度
            _booster_task_log_progress(task_id, text.replace('\r', '').strip())
            return
        parts = text.split('\n')
        if parts and parts[-1] == '':
            parts.pop()
        for part in parts:
            if part:
                _booster_task_log_line(task_id, part)
            else:
                _append_log(task_id, '')  # 保留原有的空行分隔

    return _log


def _run_booster_task(task_id: str, bv_list: list[str], target: int, stop_event: threading.Event = None, max_rounds: int = 5, refetch_proxies: bool = True, proxies: list = None, probe_cache: dict = None, car_rounds: int = 0, cookie: str = ""):
    # 等待并发闸门：最多 max_concurrent_boost_tasks 个任务同时运行，其余排队
    _booster_gate.acquire()
    try:
        # 排队期间可能已被取消（stop 端点会设为 stopping），或所属任务组已被停止（event 已置位）
        with booster_lock:
            if booster_tasks[task_id]["status"] in ("cancelled", "stopping") or (stop_event is not None and stop_event.is_set()):
                booster_tasks[task_id]["status"] = "cancelled"
                booster_tasks[task_id]["end"] = time.time()
                return
            booster_tasks[task_id]["status"] = "running"

        # booster 日志通过 log_fn 直连本任务缓冲区：不再切换全局 sys.stdout。
        # 并发任务各自把 sys.stdout 换成自己的捕获对象时，后启动的会覆盖先启动的，
        # 导致所有任务的输出都流进同一个捕获对象（日志串台），这里改为按任务分发。
        logger = _make_booster_logger(task_id)
        try:
            bv_input = ",".join(bv_list)
            booster.main(bv_input, str(target), stop_event=stop_event, max_rounds=max_rounds, refetch_proxies=refetch_proxies, proxies=proxies, probe_cache=probe_cache, log_fn=logger, cookie=cookie or None)
            with booster_lock:
                if booster_tasks[task_id]["status"] != "cancelled":
                    booster_tasks[task_id]["status"] = "completed"
        except Exception as e:
            with booster_lock:
                log_buffers.get(task_id, []).append(f"\n[ERROR] {e}\n")
            with booster_lock:
                booster_tasks[task_id]["status"] = "error"
        finally:
            # 任务结束后向 schedule 日志输出总结
            with booster_lock:
                final_status = booster_tasks.get(task_id, {}).get("status", "")
                bv = booster_tasks.get(task_id, {}).get("bv", "")
                log_target = booster_tasks.get(task_id, {}).get("log_target")
            if log_target and log_target != task_id:
                if final_status == "completed" and car_rounds > 0:
                    _append_log(log_target, f"  └ [{bv}] ⚡ 代理池完成，转车机长线 {car_rounds} 轮\n")
                elif final_status == "completed":
                    _append_log(log_target, f"  └ [{bv}] ✅ 任务完成\n")
                elif final_status == "cancelled":
                    _append_log(log_target, f"  └ [{bv}] ⏹ 已停止\n")
                elif final_status == "error":
                    _append_log(log_target, f"  └ [{bv}] ❌ 任务出错\n")
    finally:
        with booster_lock:
            booster_tasks[task_id]["end"] = time.time()
        _booster_gate.release()

    # 代理池跑完后接力车机长线：放在闸门外，长线不会占住并发名额阻塞其他稿件
    with booster_lock:
        st = booster_tasks.get(task_id) or {}
        relay = car_rounds > 0 and st.get("status") == "completed" and st.get("bv")
        relay_bv, relay_log_target = st.get("bv", ""), st.get("log_target")
    if relay:
        _booster_car_relay(task_id, relay_bv, car_rounds, stop_event, relay_log_target)


def _booster_car_relay(task_id: str, bv: str, rounds: int, stop_event, log_target):
    """booster 结束后用车机匿名通道接力跑满 rounds 轮（或 stop_event 置位）。"""
    stop_event = stop_event or threading.Event()

    def _relay_log(line: str):
        _append_log(task_id, line)
        if log_target and log_target != task_id:
            _append_log(log_target, f"  └ [{bv}] {line}\n")

    with booster_lock:
        st = booster_tasks.get(task_id)
        if not st or st.get("status") == "cancelled":
            return
        st["status"] = "car"
    _relay_log(f"[CAR] 代理池已完成，进入车机长线（{rounds} 轮）")
    try:
        car_channel.run([bv], stop_event=stop_event, on_cycle=_car_on_cycle,
                        log=_relay_log, max_rounds=rounds)
    except Exception as e:
        _relay_log(f"[CAR] 车机长线出错：{type(e).__name__}: {e}")
        with booster_lock:
            if booster_tasks[task_id]["status"] not in ("cancelled", "stopping"):
                booster_tasks[task_id]["status"] = "error"
    finally:
        with booster_lock:
            st = booster_tasks[task_id]
            if st["status"] in ("cancelled", "stopping"):
                st["status"] = "cancelled"
            elif st["status"] != "error":
                st["status"] = "completed"
            st["end"] = time.time()
            final_status = st["status"]
        if log_target and log_target != task_id:
            _append_log(log_target, f"  └ [{bv}] " +
                        ("✅ 车机长线跑完\n" if final_status == "completed" else "⏹ 车机长线已停止\n"))


@app.route("/api/booster/run", methods=["POST"])
def booster_run():
    data = request.json
    bv_str = data.get("bv", "")
    target = data.get("target", 0)
    bv_list = [b.strip() for b in bv_str.split(",") if b.strip()]
    if not bv_list or not target:
        return jsonify({"error": "缺少 BV号 或 目标播放数"}), 400
    try:
        max_rounds = int(data.get("max_rounds", 5))
    except (TypeError, ValueError):
        return jsonify({"error": "最大轮数必须是整数"}), 400
    if max_rounds < 0:
        return jsonify({"error": "最大轮数不能为负数"}), 400
    _refetch = data.get("refetch_proxies")
    refetch_proxies = True if _refetch is None else bool(_refetch)

    tid = str(uuid.uuid4())[:8]
    stop_event = threading.Event()
    with booster_lock:
        booster_tasks[tid] = {
            "status": "queued",
            "start": time.time(),
            "end": None,
            "bv": bv_str,
            "target": target,
            "max_rounds": max_rounds,
            "refetch_proxies": refetch_proxies,
            "stop_event": stop_event,
        }
    with log_lock:
        log_buffers[tid] = []
    t = threading.Thread(target=_run_booster_task,
                         kwargs={"task_id": tid, "bv_list": bv_list, "target": int(target), "stop_event": stop_event,
                                 "max_rounds": max_rounds, "refetch_proxies": refetch_proxies,
                                 "cookie": _booster_view_cookie(_read_auto_cred())},
                         daemon=True)
    t.start()
    return jsonify({"task_id": tid})

@app.route("/api/booster/stop", methods=["POST"])
def booster_stop():
    data = request.json or {}
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"error": "缺少 task_id"}), 400
    with booster_lock:
        st = booster_tasks.get(task_id)
        if not st:
            return jsonify({"error": "任务不存在"}), 404
        se = st.get("stop_event")
        if se:
            se.set()
        st["status"] = "stopping"
    return jsonify({"success": True})


@app.route("/api/booster/stop-all", methods=["POST"])
def booster_stop_all():
    """停止全部 booster 任务：逐个置位独立 stop_event（手动/webhook 任务与定时子任务互不影响），并停止定时任务。"""
    stopped = 0
    with booster_lock:
        for st in booster_tasks.values():
            if st.get("status") in ("completed", "error", "cancelled"):
                continue
            se = st.get("stop_event")
            if se:
                se.set()
            st["status"] = "cancelled"
            stopped += 1
    if _booster_schedule["running"]:
        _booster_schedule["stop_event"].set()
    _car_stop()
    print(f"[BOOSTER] 已请求停止全部任务（{stopped} 个）")
    return jsonify({"success": True, "stopped": stopped})


@app.route("/api/booster/status/<task_id>")
def booster_status(task_id):
    st = booster_tasks.get(task_id)
    if not st:
        return jsonify({"error": "not found"}), 404
    safe = {k: v for k, v in st.items() if k != "stop_event"}
    return jsonify({**safe, "output": _get_log(task_id)})


# ── 车机匿名长线通道（car_channel） ──
# 实测：额度按 aid 立，窗口 65~85s，单稿件 42~43/h；长线吞吐 = 稿件数 × 42/h。
_car_spec = _ilu.spec_from_file_location("car_channel", str(ROOT / "bili-booster" / "car_channel.py"))
car_channel = _ilu.module_from_spec(_car_spec)
_car_spec.loader.exec_module(car_channel)

_car_lock = threading.RLock()
_car_task = {
    "status": "idle",        # idle / running / stopping / stopped / completed / error
    "start": None,
    "end": None,
    "bv": [],
    "play_s": car_channel.DEFAULT_PLAY_S,
    "interval": car_channel.DEFAULT_INTERVAL,
    "stop_event": None,
    "thread": None,
}
_car_stats = None            # 本次运行累计，仅存内存，任务启动时重置


def _car_fresh_stats() -> dict:
    return {"rounds": 0, "accepted": 0, "growth": 0, "per_bv": {}}


def _car_stats_current() -> dict:
    """取本次运行累计（仅内存）。调用方需持有 _car_lock 或接受快照语义。"""
    global _car_stats
    with _car_lock:
        if _car_stats is None:
            _car_stats = _car_fresh_stats()
        return _car_stats


def _car_log(line: str):
    _append_log("car", line if line.endswith("\n") else line + "\n")


def _car_on_cycle(ev: dict):
    """每完成一轮会话回调：累加本次统计；每 20 轮打一条累计日志。"""
    global _car_stats
    summary = None
    with _car_lock:
        stats = _car_stats_current()
        per = stats["per_bv"].setdefault(
            ev["bv"], {"views": 0, "growth": 0, "rounds": 0, "accepted": 0})
        stats["rounds"] += 1
        per["rounds"] += 1
        if ev.get("ok"):
            stats["accepted"] += 1
            per["accepted"] += 1
        if ev.get("views") is not None:
            per["views"] = ev["views"]
        if ev.get("delta") is not None:
            stats["growth"] += ev["delta"]
            per["growth"] += ev["delta"]
        if stats["rounds"] % 20 == 0:
            summary = (f'[CAR] 本次累计：{stats["rounds"]} 轮 / 有效 {stats["accepted"]} / '
                       f'播放增长 {stats["growth"]:+d}')
    if summary:
        _car_log(summary)


def _car_stop():
    """置位车机长线的停止事件（供 stop-all 复用）。"""
    with _car_lock:
        se = _car_task.get("stop_event")
        if se:
            se.set()
        if _car_task["status"] in ("running", "stopping"):
            _car_task["status"] = "stopping"


def _run_car_task(bv_list, play_s, interval, stop_event):
    try:
        car_channel.run(bv_list, play_s=play_s, interval=interval, stop_event=stop_event,
                        on_cycle=_car_on_cycle, log=_car_log)
        with _car_lock:
            _car_task["status"] = "stopped" if stop_event.is_set() else "completed"
    except Exception as e:
        _car_log(f'[CAR] 任务出错：{type(e).__name__}: {e}')
        with _car_lock:
            _car_task["status"] = "error"
    finally:
        with _car_lock:
            _car_task["end"] = time.time()


@app.route("/api/booster/car/start", methods=["POST"])
def booster_car_start():
    global _car_stats
    data = request.json or {}
    raw = str(data.get("bv", "")).replace("，", ",")
    bv_list = [b.strip() for b in raw.split(",") if b.strip()]
    if not bv_list:
        return jsonify({"error": "缺少 BV号"}), 400
    try:
        play_s = int(data.get("play_s") or car_channel.DEFAULT_PLAY_S)
        interval = int(data.get("interval") or car_channel.DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        return jsonify({"error": "播放秒数 / 节拍必须是整数"}), 400
    if not 1 <= play_s <= 300:
        return jsonify({"error": "单会话播放秒数需在 1~300 之间"}), 400
    interval = max(car_channel.MIN_INTERVAL, interval)

    with _car_lock:
        if _car_task["status"] in ("running", "stopping"):
            return jsonify({"error": "车机长线已在运行，请先停止"}), 400
        _car_stats = _car_fresh_stats()   # 本次运行重新累计，不读回上次结果
        stop_event = threading.Event()
        _car_task.update({
            "status": "running", "start": time.time(), "end": None,
            "bv": bv_list, "play_s": play_s, "interval": interval,
            "stop_event": stop_event,
        })
        with log_lock:
            log_buffers["car"] = []
        t = threading.Thread(target=_run_car_task,
                             args=(bv_list, play_s, interval, stop_event), daemon=True)
        _car_task["thread"] = t
        t.start()
    return jsonify({"success": True, "bv": bv_list, "play_s": play_s, "interval": interval})


@app.route("/api/booster/car/stop", methods=["POST"])
def booster_car_stop():
    _car_stop()
    return jsonify({"success": True})


@app.route("/api/booster/car/status")
def booster_car_status():
    with _car_lock:
        task = {
            "status": _car_task["status"],
            "start": _car_task["start"],
            "end": _car_task["end"],
            "bv": list(_car_task["bv"]),
            "play_s": _car_task["play_s"],
            "interval": _car_task["interval"],
        }
        stats = json.loads(json.dumps(_car_stats_current(), ensure_ascii=False))
    running = task["status"] in ("running", "stopping")
    elapsed = None
    if task["start"]:
        elapsed = int((task["end"] or time.time()) - task["start"])
    return jsonify({**task, "running": running, "elapsed": elapsed,
                    "stats": stats, "output": _get_log("car")})


@app.route("/api/booster/car/stats/remove", methods=["POST"])
def booster_car_stats_remove():
    """删除本次统计里的单个稿件行；all=True 时清空本次全部统计。"""
    data = request.json or {}
    bv = str(data.get("bv") or "").strip()
    all_flag = bool(data.get("all"))
    if not bv and not all_flag:
        return jsonify({"error": "缺少 bv"}), 400

    with _car_lock:
        stats = _car_stats_current()
        if all_flag:
            stats["rounds"] = 0
            stats["accepted"] = 0
            stats["growth"] = 0
            stats["per_bv"] = {}
        else:
            removed = stats["per_bv"].pop(bv, None)
            if removed is None:
                return jsonify({"error": "该稿件不在本次统计中"}), 404
            # 汇总数值同步扣掉被删稿件的部分，避免出现「没有明细但有总量」
            stats["rounds"] = max(0, stats["rounds"] - int(removed.get("rounds") or 0))
            stats["accepted"] = max(0, stats["accepted"] - int(removed.get("accepted") or 0))
            stats["growth"] -= int(removed.get("growth") or 0)
        snapshot = json.loads(json.dumps(stats, ensure_ascii=False))
    return jsonify({"success": True, "stats": snapshot})


@app.route("/api/booster/my-videos")
def booster_my_videos():
    """获取登录账号的投稿视频列表，支持 pn/ps 分页（选稿弹窗翻页用）"""
    cred = _read_auto_cred()
    sessdata = cred.get("sessdata", "")
    mid = cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or ""
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先在「自动互动」页面登录账号"})

    pn = max(1, request.args.get("pn", type=int) or 1)
    ps = max(1, min(request.args.get("ps", type=int) or FETCH_MEMBER_PAGE_SIZE, 100))

    try:
        result = _fetch_member_videos_page(cred, pn, ps)
        return jsonify({"success": True, "mid": mid, **result})
    except Exception as e:
        print(f"[WARN] 创作中心接口分页失败，回退空间接口（可能漏稿）: {e}")
        try:
            result = _fetch_space_videos_page(cred, mid, pn, min(ps, 50))
            return jsonify({"success": True, "mid": mid, **result})
        except Exception as e2:
            import traceback
            error_msg = f"{type(e).__name__}: {str(e2)}"
            print(f"[ERROR] booster_my_videos: {error_msg}")
            traceback.print_exc()
            return jsonify({"success": False, "message": error_msg})


FETCH_VIDEOS_TIMEOUT = 30          # 获取投稿列表的单次超时（秒）
BOOSTER_SCAN_LIMIT_DEFAULT = 30    # 定时扫描的默认深度（每个账号检查最近 N 个投稿）
BOOSTER_SCAN_LIMIT_WARN = 30       # 超过该深度需多次分页请求，提示风控风险
BOOSTER_MAX_SCAN_LIMIT = 3000      # 全量扫描的硬上限，避免无限翻页
BOOSTER_CAR_ROUNDS_DEFAULT = 20    # 定时任务跑完代理池后接力车机长线的默认轮数
BOOSTER_CAR_ROUNDS_MAX = 2000      # 车机长线轮数上限（约 2000 × 85s ≈ 47 小时）
FETCH_MEMBER_PAGE_SIZE = 50        # 创作中心接口单页条数
FETCH_PAGE_DELAY = (0.4, 0.9)      # 分页之间的随机间隔，降低触发风控的概率


def _fmt_duration(sec) -> str:
    """秒 → 'MM:SS'（超过 1 小时用 'H:MM:SS'）。"""
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _scan_limit_label(limit) -> str:
    """扫描深度的可读文案。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = BOOSTER_SCAN_LIMIT_DEFAULT
    return f"全量（最多 {BOOSTER_MAX_SCAN_LIMIT} 条）" if limit <= 0 else f"最近 {limit} 条"


def _fetch_member_videos(cred: dict, limit: int = BOOSTER_SCAN_LIMIT_DEFAULT) -> list:
    """通过创作中心「稿件管理」接口获取自己的投稿列表（最全）。

    空间接口 x/space/wbi/arc/search 会漏掉部分稿件（实测同一账号 1472 vs 1542），
    创作中心接口不会漏。limit<=0 表示全量（硬上限 BOOSTER_MAX_SCAN_LIMIT）。"""
    import random
    import httpx

    sessdata = cred.get("sessdata", "")
    uid = str(cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or "")
    if not sessdata or not uid:
        raise ValueError("缺少 SESSDATA 或 UID")

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
        "Referer": "https://member.bilibili.com/",
        "Origin": "https://member.bilibili.com",
    }
    cookies = {"SESSDATA": sessdata, "bili_jct": cred.get("bili_jct", ""), "DedeUserID": uid}

    limit = int(limit)
    cap = BOOSTER_MAX_SCAN_LIMIT if limit <= 0 else limit
    max_pages = (cap + FETCH_MEMBER_PAGE_SIZE - 1) // FETCH_MEMBER_PAGE_SIZE

    videos = []
    pn = 1
    with httpx.Client(timeout=FETCH_VIDEOS_TIMEOUT, headers=headers, cookies=cookies) as client:
        while len(videos) < cap and pn <= max_pages:
            if pn > 1:
                time.sleep(random.uniform(*FETCH_PAGE_DELAY))
            resp = client.get("https://member.bilibili.com/x/web/archives",
                              params={"status": "pubed", "pn": pn, "ps": FETCH_MEMBER_PAGE_SIZE, "coop": 1})
            data = resp.json()
            code = data.get("code")
            if code != 0:
                # -352/-412 等为风控/限流，直接抛错交由调用方回退空间接口
                raise RuntimeError(f"创作中心接口返回异常 code={code} msg={data.get('message') or ''}")
            items = (data.get("data") or {}).get("arc_audits") or []
            for item in items:
                arc = item.get("Archive") or item
                if int(arc.get("state", 0) or 0) != 0:  # 只保留已通过（公开）稿件
                    continue
                stat = item.get("stat") or {}
                videos.append({
                    "bvid": arc.get("bvid", ""),
                    "title": arc.get("title", ""),
                    "pic": arc.get("cover", ""),
                    "play": int(stat.get("view", 0) or 0),
                    "created": int(arc.get("ptime", 0) or 0),
                    "length": _fmt_duration(arc.get("duration")),
                })
                if len(videos) >= cap:
                    break
            if len(items) < FETCH_MEMBER_PAGE_SIZE:  # 已到最后一页
                break
            pn += 1
    return videos


def _fetch_member_videos_page(cred: dict, pn: int = 1, ps: int = FETCH_MEMBER_PAGE_SIZE) -> dict:
    """创作中心「稿件管理」接口的指定页，供选稿弹窗翻页使用。

    返回 {videos, total, has_more, page, page_size}；total 取不到时为 None。"""
    import httpx

    sessdata = cred.get("sessdata", "")
    uid = str(cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or "")
    if not sessdata or not uid:
        raise ValueError("缺少 SESSDATA 或 UID")

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
        "Referer": "https://member.bilibili.com/",
        "Origin": "https://member.bilibili.com",
    }
    cookies = {"SESSDATA": sessdata, "bili_jct": cred.get("bili_jct", ""), "DedeUserID": uid}

    pn = max(1, int(pn))
    ps = max(1, min(int(ps), 100))
    with httpx.Client(timeout=FETCH_VIDEOS_TIMEOUT, headers=headers, cookies=cookies) as client:
        resp = client.get("https://member.bilibili.com/x/web/archives",
                          params={"status": "pubed", "pn": pn, "ps": ps, "coop": 1})
        data = resp.json()
    code = data.get("code")
    if code != 0:
        raise RuntimeError(f"创作中心接口返回异常 code={code} msg={data.get('message') or ''}")

    payload = data.get("data") or {}
    items = payload.get("arc_audits") or []
    page = payload.get("page") or {}
    total = None
    for key in ("count", "total", "total_count"):
        try:
            if page.get(key) is not None:
                total = int(page.get(key))
                break
        except (TypeError, ValueError):
            continue

    videos = []
    for item in items:
        arc = item.get("Archive") or item
        if int(arc.get("state", 0) or 0) != 0:  # 只保留已通过（公开）稿件
            continue
        stat = item.get("stat") or {}
        videos.append({
            "bvid": arc.get("bvid", ""),
            "title": arc.get("title", ""),
            "pic": arc.get("cover", ""),
            "play": int(stat.get("view", 0) or 0),
            "created": int(arc.get("ptime", 0) or 0),
            "length": _fmt_duration(arc.get("duration")),
        })

    has_more = (pn * ps < total) if total is not None else (len(items) >= ps)
    return {"videos": videos, "total": total, "has_more": has_more, "page": pn, "page_size": ps}


def _fetch_space_videos_page(cred: dict, mid, pn: int = 1, ps: int = 50) -> dict:
    """空间接口分页（创作中心接口失败时的兜底，单页上限 50，可能漏合集内稿件）。"""
    from bilibili_api import Credential
    from bilibili_api.user import User, VideoOrder

    own_mid = str(cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or "")
    target_mid = str(mid or own_mid).strip()
    if not target_mid:
        raise ValueError("缺少 UID")

    credential = Credential(
        sessdata=cred.get("sessdata", ""),
        bili_jct=cred.get("bili_jct", ""),
        dedeuserid=own_mid,
        ac_time_value=cred.get("ac_time_value", ""),
    )
    u = User(int(target_mid), credential=credential)
    pn = max(1, int(pn))
    ps = max(1, min(int(ps), 50))

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(asyncio.wait_for(
            u.get_videos(pn=pn, ps=ps, order=VideoOrder.PUBDATE), timeout=FETCH_VIDEOS_TIMEOUT))
    finally:
        loop.close()

    vlist = (result or {}).get("list", {}).get("vlist", [])
    videos = [{
        "bvid": v.get("bvid", ""),
        "title": v.get("title", ""),
        "pic": v.get("pic", ""),
        "play": v.get("play", 0),
        "created": v.get("created", 0),
        "length": v.get("length", ""),
    } for v in vlist]

    page = (result or {}).get("page") or {}
    total = None
    try:
        if page.get("count") is not None:
            total = int(page.get("count"))
    except (TypeError, ValueError):
        total = None
    has_more = (pn * ps < total) if total is not None else (len(vlist) >= ps)
    return {"videos": videos, "total": total, "has_more": has_more, "page": pn, "page_size": ps}


def _fetch_season_videos(cred: dict, mid, limit=None, public=False) -> list:
    """获取指定账号所有合集内的稿件，用于补齐空间防刷屏导致的漏稿。

    空间防刷屏开启后，合集内除最早发布的稿件外，其余稿件会从空间接口
    （x/space/wbi/arc/search）隐藏；合集详情接口 seasons_archives_list 不受该设置影响。
    public=True 时用公开接口拿合集列表（可查他人 UID）；否则用创作中心接口（自己账号，更全）。
    单个合集失败只跳过，不影响其他合集。limit 与扫描深度一致（0=全量，带上限）。"""
    import random
    import httpx

    sessdata = cred.get("sessdata", "")
    uid = str(mid or cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or "")
    if not uid or (not public and not sessdata):
        return []

    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    limit = int(limit or 0)
    cap = BOOSTER_MAX_SCAN_LIMIT if limit <= 0 else max(limit, FETCH_MEMBER_PAGE_SIZE * 2)

    # 1) 拿合集 ID 列表
    season_ids = []
    if public:
        # 公开接口：任意账号的合集列表（page_size 上限 20，超过会返回 -400）
        headers = {"User-Agent": ua,
                   "Referer": f"https://space.bilibili.com/{uid}/channel/collectiondetail"}
        with httpx.Client(timeout=FETCH_VIDEOS_TIMEOUT, headers=headers) as client:
            pn = 1
            while pn <= 20:
                if pn > 1:
                    time.sleep(random.uniform(*FETCH_PAGE_DELAY))
                resp = client.get("https://api.bilibili.com/x/polymer/web-space/seasons_series_list",
                                  params={"mid": uid, "page_num": pn, "page_size": 20})
                data = resp.json()
                code = data.get("code")
                if code != 0:
                    raise RuntimeError(f"公开合集列表接口返回异常 code={code} msg={data.get('message') or ''}")
                items = (data.get("data") or {}).get("items_lists") or {}
                seasons = items.get("seasons_list") or []
                for s in seasons:
                    sid = (s.get("meta") or {}).get("season_id")
                    if sid:
                        try:
                            season_ids.append(int(sid))
                        except (TypeError, ValueError):
                            pass
                page = items.get("page") or {}
                total = int(page.get("total") or 0)
                size = int(page.get("page_size") or 20)
                if not seasons or len(seasons) < size or (total and pn * size >= total):
                    break
                pn += 1
    else:
        # 创作中心：自己名下全部合集（含未公开状态）
        headers = {"User-Agent": ua, "Referer": "https://member.bilibili.com/",
                   "Origin": "https://member.bilibili.com"}
        cookies = {"SESSDATA": sessdata, "bili_jct": cred.get("bili_jct", ""), "DedeUserID": uid}
        with httpx.Client(timeout=FETCH_VIDEOS_TIMEOUT, headers=headers, cookies=cookies) as client:
            pn = 1
            while pn <= 20:
                if pn > 1:
                    time.sleep(random.uniform(*FETCH_PAGE_DELAY))
                resp = client.get("https://member.bilibili.com/x2/creative/web/seasons",
                                  params={"pn": pn, "ps": FETCH_MEMBER_PAGE_SIZE})
                data = resp.json()
                code = data.get("code")
                if code != 0:
                    raise RuntimeError(f"合集列表接口返回异常 code={code} msg={data.get('message') or ''}")
                d = data.get("data") or {}
                items = d.get("seasons") or []
                for s in items:
                    sobj = s.get("season") if isinstance(s.get("season"), dict) else s
                    sid = sobj.get("id") or s.get("id")
                    if sid:
                        try:
                            season_ids.append(int(sid))
                        except (TypeError, ValueError):
                            pass
                total = int(d.get("total") or 0)
                if len(items) < FETCH_MEMBER_PAGE_SIZE or (total and pn * FETCH_MEMBER_PAGE_SIZE >= total):
                    break
                pn += 1

    if not season_ids:
        return []

    # 2) 合集详情（公开接口）：逐合集翻页取全部稿件
    videos = []
    space_headers = {"User-Agent": ua,
                     "Referer": f"https://space.bilibili.com/{uid}/channel/collectiondetail"}
    with httpx.Client(timeout=FETCH_VIDEOS_TIMEOUT, headers=space_headers) as client:
        for sid in season_ids:
            if len(videos) >= cap:
                break
            try:
                page_num = 1
                while page_num <= 100:
                    if page_num > 1:
                        time.sleep(random.uniform(*FETCH_PAGE_DELAY))
                    resp = client.get(
                        "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
                        params={"mid": uid, "season_id": sid, "page_num": page_num,
                                "page_size": FETCH_MEMBER_PAGE_SIZE, "sort_reverse": "false"})
                    data = resp.json()
                    code = data.get("code")
                    if code != 0:
                        raise RuntimeError(f"code={code} msg={data.get('message') or ''}")
                    d = data.get("data") or {}
                    archives = d.get("archives") or []
                    for a in archives:
                        stat = a.get("stat") or {}
                        videos.append({
                            "aid": int(a.get("aid") or 0),
                            "bvid": a.get("bvid", ""),
                            "title": a.get("title", ""),
                            "pic": a.get("pic", ""),
                            "play": int(stat.get("view", 0) or 0),
                            "created": int(a.get("pubdate") or a.get("ctime") or 0),
                            "length": _fmt_duration(a.get("duration")),
                        })
                        if len(videos) >= cap:
                            break
                    page_info = d.get("page") or {}
                    total = int(page_info.get("total") or 0)
                    size = int(page_info.get("page_size") or FETCH_MEMBER_PAGE_SIZE)
                    if len(videos) >= cap or not archives or len(archives) < size \
                            or (total and page_num * size >= total):
                        break
                    page_num += 1
            except Exception as e:
                print(f"[WARN] 合集 {sid} 稿件获取失败，已跳过: {e}")
    return videos


def _merge_videos(*lists) -> list:
    """按 bvid 去重合并多个投稿列表（保留先出现的记录，即主列表优先）。"""
    merged = []
    seen = set()
    for lst in lists:
        for v in lst or []:
            bvid = str(v.get("bvid") or "").strip()
            if bvid:
                if bvid in seen:
                    continue
                seen.add(bvid)
            merged.append(v)
    return merged


def _fetch_my_videos(cred: dict, mid=None, limit=None) -> list:
    """获取投稿列表，供 my-videos 接口和定时任务共用。

    - 自己（mid 为空或等于凭证本人）：创作中心接口为主（失败回退空间接口），再用合集接口补齐合集稿件。
    - 他人 UID：空间接口（单页 50 条）+ 公开合集接口补齐合集稿件；合集外的更早稿件仍可能漏。
    limit 为扫描深度（None 时读配置，0=全量）。"""
    from bilibili_api import Credential
    from bilibili_api.user import User, VideoOrder

    own_mid = str(cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or "")
    target_mid = str(mid or "").strip() or own_mid

    if limit is None:
        try:
            limit = int(_load_booster_config().get("schedule", {}).get("scan_limit", BOOSTER_SCAN_LIMIT_DEFAULT))
        except (TypeError, ValueError):
            limit = BOOSTER_SCAN_LIMIT_DEFAULT
    limit = int(limit)

    if own_mid and target_mid == own_mid:
        try:
            videos = _fetch_member_videos(cred, limit)
        except Exception as e:
            print(f"[WARN] 创作中心接口获取投稿失败，回退空间接口（可能漏稿）: {e}")
        else:
            # 空间防刷屏会把合集内非首个稿件从空间接口隐藏，创作中心接口同样可能只返回首个；
            # 合集详情接口不受该设置影响，这里补齐并统一按发布时间倒序。
            try:
                videos = _merge_videos(videos, _fetch_season_videos(cred, own_mid, limit))
            except Exception as e:
                print(f"[WARN] 合集稿件补充失败（不影响主列表）: {e}")
            videos.sort(key=lambda v: int(v.get("created") or 0), reverse=True)
            if limit > 0:
                videos = videos[:limit]
            return videos

    # Credential 会自动处理 SESSDATA 编码和 buvid 获取
    credential = Credential(
        sessdata=cred.get("sessdata", ""),
        bili_jct=cred.get("bili_jct", ""),
        dedeuserid=own_mid,
        ac_time_value=cred.get("ac_time_value", ""),
    )
    u = User(int(target_mid), credential=credential)
    # 空间接口单页上限 50，且不支持翻页（会漏稿，仅用于他人 UID 或创作中心接口失败时兜底）
    ps = 50 if limit <= 0 else max(1, min(limit, 50))

    loop = asyncio.new_event_loop()
    try:
        result = None
        for _attempt in range(3):
            try:
                # 加超时：网络卡住时不再无限等待，让「停止」能及时收敛
                result = loop.run_until_complete(
                    asyncio.wait_for(u.get_videos(ps=ps, order=VideoOrder.PUBDATE), timeout=FETCH_VIDEOS_TIMEOUT))
                break
            except asyncio.TimeoutError:
                raise TimeoutError(f"获取投稿列表超时（>{FETCH_VIDEOS_TIMEOUT}s）")
            except Exception as retry_err:
                if _attempt < 2 and "第三方请求库" in str(retry_err):
                    time.sleep(1)
                    continue
                raise
    finally:
        loop.close()

    vlist = (result or {}).get("list", {}).get("vlist", [])
    videos = []
    for v in vlist:
        videos.append({
            "bvid": v.get("bvid", ""),
            "title": v.get("title", ""),
            "pic": v.get("pic", ""),
            "play": v.get("play", 0),
            "created": v.get("created", 0),
            "length": v.get("length", ""),
        })

    # 空间防刷屏会把合集内非首个稿件从空间接口隐藏，用公开合集接口补齐（他人 UID 同样可查）
    try:
        videos = _merge_videos(videos, _fetch_season_videos(cred, target_mid, limit, public=True))
    except Exception as e:
        print(f"[WARN] UID {target_mid} 合集稿件补充失败（不影响主列表）: {e}")
    videos.sort(key=lambda v: int(v.get("created") or 0), reverse=True)
    if limit > 0:
        videos = videos[:limit]
    return videos


@app.route("/api/booster/tasks")
def booster_all():
    with booster_lock:
        # 过滤 stop_event（threading.Event 无法被 jsonify 序列化，否则 500 导致总览状态取不到）
        safe = {}
        for k, v in booster_tasks.items():
            safe[k] = {kk: vv for kk, vv in v.items() if kk != "stop_event"}
        return jsonify(safe)


# ---- Booster 定时任务：自动为低播放量投稿跑一次刷量 ----
BOOSTER_CONFIG_FILE = CONFIG_DIR / "booster_config.yaml"

_booster_schedule = {
    "running": False,
    "thread": None,
    "stop_event": threading.Event(),
    "last_run": None,
    "next_run": None,
    "run_count": 0,
}

# 定时任务链路共享的代理探测缓存：跨稿件、跨轮次复用探测结果（TTL/上限由 booster 模块控制）。
# 只存内存，重启即清空。
_booster_probe_cache = {}


def _load_booster_config():
    import yaml
    if BOOSTER_CONFIG_FILE.exists():
        try:
            with open(BOOSTER_CONFIG_FILE, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def _save_booster_config(cfg):
    import yaml
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(BOOSTER_CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


def _booster_end_once(log_target: str):
    """「once」模式下标记结束并写日志。"""
    if _booster_schedule["running"] == "once":
        _booster_schedule["running"] = False
        _append_log(log_target, "=== 手动执行已结束 ===")


def _booster_active_bvids() -> set:
    """当前仍在途（排队/运行/停止中）的 bvid 集合，用于避免重复派发。"""
    active = set()
    with booster_lock:
        for t in booster_tasks.values():
            if t.get("status") in ("queued", "running", "stopping", "car") and t.get("bv"):
                active.add(t["bv"])
    return active


def _booster_spawn_tasks(low_videos: list, target: int, stop_event: threading.Event, log_target: str,
                         proxies: list = None, probe_cache: dict = None, car_rounds: int = 0, cookie: str = "") -> int:
    """为给定稿件列表各创建一个 booster 子任务，返回创建数量。

    已在队列/运行中的 bvid，以及当前播放量已达目标的稿件直接跳过，避免每轮重复派发造成堆积。
    proxies 为本轮共享的代理快照：同一批子任务共用一份，避免各自重复拉取代理池。
    probe_cache 为共享的探测结果缓存：同一批子任务对同一批代理只探测一次。
    cookie 为账号 Cookie：view 接口带登录态，降低服务器机房 IP 被判 412 的概率。
    """
    created = 0
    active_bvids = _booster_active_bvids()
    for v in low_videos:
        if stop_event.is_set():
            _append_log(log_target, "[SYSTEM] 正在停止...")
            break
        bvid = v["bvid"]
        play = int(v.get("play", 0) or 0)
        if play >= target:
            _append_log(log_target, f"跳过 {bvid}：当前播放 {play} 已达目标 {target}")
            continue
        if bvid in active_bvids:
            _append_log(log_target, f"跳过 {bvid}：已有任务在排队/运行中")
            continue
        tid = str(uuid.uuid4())[:8]
        with booster_lock:
            booster_tasks[tid] = {
                "status": "queued",
                "start": time.time(),
                "end": None,
                "bv": bvid,
                "title": v.get("title", ""),
                "target": target,
                "max_rounds": 5,
                "car_rounds": car_rounds,
                "refetch_proxies": proxies is None,
                "stop_event": stop_event,
                "log_target": log_target,  # 便于子任务日志回流
            }
        active_bvids.add(bvid)
        with log_lock:
            log_buffers[tid] = []
        _append_log(log_target, f"已创建任务 {tid} → {bvid}《{v.get('title','')}》（当前播放 {play}）")
        t = threading.Thread(target=_run_booster_task,
                             kwargs={"task_id": tid, "bv_list": [bvid], "target": target, "stop_event": stop_event,
                                     "max_rounds": 5, "refetch_proxies": proxies is None, "proxies": proxies,
                                     "probe_cache": probe_cache, "car_rounds": car_rounds, "cookie": cookie},
                             daemon=True)
        t.start()
        created += 1
    return created


def _booster_schedule_once(stop_event: threading.Event, log_target: str = "booster-schedule"):
    """执行一次：先为「刷量账号」抓取各自投稿，再为「监控 UID」抓取公开投稿，
    为播放量低于阈值的稿件各创建一个 booster 任务。两类目标共用阈值与目标播放数。"""
    cfg = _load_booster_config().get("schedule", {})
    try:
        threshold = int(cfg.get("play_threshold", 200))
        target = int(cfg.get("target_play", 200))
        scan_limit = int(cfg.get("scan_limit", BOOSTER_SCAN_LIMIT_DEFAULT))
    except (TypeError, ValueError):
        threshold, target, scan_limit = 200, 200, BOOSTER_SCAN_LIMIT_DEFAULT
    if cfg.get("car_enabled", True):
        try:
            car_rounds = max(1, int(cfg.get("car_rounds", BOOSTER_CAR_ROUNDS_DEFAULT)))
        except (TypeError, ValueError):
            car_rounds = BOOSTER_CAR_ROUNDS_DEFAULT
        car_rounds = min(car_rounds, BOOSTER_CAR_ROUNDS_MAX)
    else:
        car_rounds = 0
    scan_desc = _scan_limit_label(scan_limit)
    if car_rounds > 0:
        _append_log(log_target, f"车机长线已开启：符合条件稿件跑完代理池后接力 {car_rounds} 轮"
                                f"（约 {car_rounds * car_channel.DEFAULT_INTERVAL // 60} 分钟/稿件）")
    if scan_limit <= 0 or scan_limit > BOOSTER_SCAN_LIMIT_WARN:
        _append_log(log_target, f"[WARN] 扫描深度为{scan_desc}，超过 {BOOSTER_SCAN_LIMIT_WARN} 条会多次分页请求，"
                                "存在触发 B 站风控的风险，建议按需调低")

    account_uids = [str(u).strip() for u in (cfg.get("account_uids") or []) if str(u or "").strip()]
    watch_uids = [str(u).strip() for u in (cfg.get("uids") or []) if str(u or "").strip()]

    main_cred = _read_auto_cred()
    main_mid = main_cred.get("dedeuserid") or main_cred.get("mid") or main_cred.get("login_uid") or ""

    accounts = []
    if account_uids:
        try:
            from core import load_account_dicts_by_uids
            accounts = load_account_dicts_by_uids(account_uids)
        except Exception as e:
            _append_log(log_target, f"[ERROR] 加载账号凭证失败: {e}")
        if not accounts and main_cred.get("sessdata") and main_mid:
            # 回退：多账号存储为空时直接用主账号凭证文件
            accounts = [("", main_cred, {"name": ""})]

    if not accounts and not watch_uids:
        _append_log(log_target, "[ERROR] 未配置刷量账号或监控 UID，请先在页面上配置")
        _booster_end_once(log_target)
        return

    total_created = 0

    # 本轮共享代理快照：首次真正需要派发时才拉取一次，本轮所有子任务复用，避免各自重复拉取 6 源代理池
    _shared_proxies = None
    _proxies_fetched = False

    def _get_shared_proxies():
        nonlocal _shared_proxies, _proxies_fetched
        if _proxies_fetched:
            return _shared_proxies
        _proxies_fetched = True
        try:
            _t0 = time.time()
            _proxies = booster.get_total_proxies(lambda m: _append_log(log_target, f"[PROXY] {m}"), stop_event=stop_event)
            if _proxies:
                _shared_proxies = _proxies
                _append_log(log_target, f"[PROXY] 本轮共享代理池已就绪：{len(_proxies)} 个（耗时 {time.time()-_t0:.1f}s），本轮所有子任务复用")
            else:
                _append_log(log_target, "[PROXY] 本轮未获取到代理，回退为各子任务自行拉取")
        except Exception as e:
            _append_log(log_target, f"[WARN] 获取共享代理池失败，回退为各子任务自行拉取: {e}")
        return _shared_proxies

    for uid, acc_cred, meta in accounts:
        if stop_event.is_set():
            _append_log(log_target, "[SYSTEM] 正在停止...")
            break
        name = meta.get("name") or (f"UID {uid}" if uid else "主账号")
        _append_log(log_target, f"获取账号【{name}】的投稿列表...（扫描{scan_desc}，筛选条件：播放量 < {threshold}，目标播放数：{target}）")
        try:
            videos = _fetch_my_videos(acc_cred, limit=scan_limit)
        except Exception as e:
            _append_log(log_target, f"[ERROR] 账号【{name}】获取投稿列表失败: {e}")
            continue

        if stop_event.is_set():
            _append_log(log_target, "[SYSTEM] 正在停止...")
            break

        low = [v for v in videos if int(v.get("play", 0) or 0) < threshold]
        if not low:
            _append_log(log_target, f"账号【{name}】共 {len(videos)} 个投稿，无播放量低于 {threshold} 的稿件，本轮跳过")
            continue

        _append_log(log_target, f"账号【{name}】共 {len(videos)} 个投稿，其中 {len(low)} 个播放量低于 {threshold}：" +
                    ", ".join(v["bvid"] for v in low))
        total_created += _booster_spawn_tasks(low, target, stop_event, log_target,
                                              proxies=_get_shared_proxies(), probe_cache=_booster_probe_cache,
                                              car_rounds=car_rounds, cookie=_booster_view_cookie(acc_cred))

    # 监控 UID：统一用主账号凭证抓取其公开投稿
    if watch_uids and not stop_event.is_set():
        if not (main_cred.get("sessdata") and main_mid):
            _append_log(log_target, "[ERROR] 未登录，请先在「自动互动」页面登录主账号")
        else:
            for uid in watch_uids:
                if stop_event.is_set():
                    _append_log(log_target, "[SYSTEM] 正在停止...")
                    break
                _append_log(log_target, f"获取监控 UID {uid} 的投稿列表...（扫描{scan_desc}，空间接口最多 50 条 + 合集补齐，"
                                        f"筛选条件：播放量 < {threshold}，目标播放数：{target}）")
                try:
                    videos = _fetch_my_videos(main_cred, mid=uid, limit=scan_limit)
                except Exception as e:
                    _append_log(log_target, f"[ERROR] UID {uid} 获取投稿列表失败: {e}")
                    continue

                if stop_event.is_set():
                    _append_log(log_target, "[SYSTEM] 正在停止...")
                    break

                low = [v for v in videos if int(v.get("play", 0) or 0) < threshold]
                if not low:
                    _append_log(log_target, f"UID {uid} 共 {len(videos)} 个投稿，无播放量低于 {threshold} 的稿件，本轮跳过")
                    continue

                _append_log(log_target, f"UID {uid} 共 {len(videos)} 个投稿，其中 {len(low)} 个播放量低于 {threshold}：" +
                            ", ".join(v["bvid"] for v in low))
                total_created += _booster_spawn_tasks(low, target, stop_event, log_target,
                                                      proxies=_get_shared_proxies(), probe_cache=_booster_probe_cache,
                                                      car_rounds=car_rounds, cookie=_booster_view_cookie(main_cred))

    # 「once」模式下不进入循环：等所有派生的 booster 子任务都结束后才标记结束
    if _booster_schedule["running"] == "once":
        if total_created:
            threading.Thread(target=_wait_once_done, args=(log_target,), daemon=True).start()
        else:
            _booster_end_once(log_target)


def _booster_round_pending(stop_event: threading.Event, log_target: str) -> int:
    """统计某一轮（同一 stop_event + log_target）仍在排队/运行中的 booster 子任务数。"""
    with booster_lock:
        n = 0
        for t in booster_tasks.values():
            if t.get("log_target") != log_target:
                continue
            if t.get("stop_event") is not stop_event:
                continue
            if t.get("status") not in ("completed", "error", "cancelled", "stopping"):
                n += 1
        return n


def _booster_schedule_loop():
    """定时任务主循环：不重叠 + 固定节奏。

    下一轮必须满足两个条件才启动：
    1) 本轮派生的 booster 子任务已全部结束（避免上一轮还没跑完就叠下一轮、把队列越堆越深）；
    2) 已到「本轮开始时刻 + interval」（执行耗时计入周期，实际间隔与配置一致）。
    即下一轮启动时刻 = max(本轮子任务跑完时刻, 本轮开始时刻 + interval)。
    """
    log_target = "booster-schedule"
    while not _booster_schedule["stop_event"].is_set():
        stop_event = _booster_schedule["stop_event"]
        cfg = _load_booster_config().get("schedule", {})
        try:
            interval = int(cfg.get("interval_minutes", 60)) * 60
        except (TypeError, ValueError):
            interval = 60 * 60

        round_start = time.time()
        _append_log(log_target, f"=== 定时刷量执行 (第{_booster_schedule['run_count']+1}次) ===")
        _booster_schedule_once(stop_event)
        _booster_schedule["run_count"] += 1
        _booster_schedule["last_run"] = time.time()
        _booster_schedule["next_run"] = round_start + interval

        if stop_event.is_set():
            break

        # 1) 等本轮子任务全部结束（含排队中的），不重叠
        last_pending = -1
        announced = False
        while True:
            pending = _booster_round_pending(stop_event, log_target)
            if pending == 0:
                break
            if pending != last_pending:
                if not announced:
                    _append_log(log_target, f"=== 本轮还有 {pending} 个子任务在执行，等其全部结束后再进入下一轮 ===")
                    announced = True
                else:
                    _append_log(log_target, f"=== 本轮剩余 {pending} 个子任务执行中... ===")
                last_pending = pending
            if stop_event.is_set():
                break
            time.sleep(5)

        if stop_event.is_set():
            break

        # 2) 再按固定节奏等到「本轮开始时刻 + interval」
        cost = time.time() - round_start
        wait = max(0, int(round(round_start + interval - time.time())))
        if wait > 0:
            _append_log(log_target, f"=== 本轮耗时 {cost:.0f}s（含子任务执行），等待 {wait} 秒后执行下一次 ===")
        else:
            _append_log(log_target, f"=== 本轮耗时 {cost:.0f}s（含子任务执行），已达/超过间隔 {interval} 秒，立即执行下一次 ===")
        _booster_schedule["next_run"] = time.time() + wait

        # 分段等待，以便能快速响应停止
        for _ in range(wait):
            if stop_event.is_set():
                break
            time.sleep(1)

    _booster_schedule["running"] = False
    _append_log(log_target, "定时刷量任务已停止")


def _wait_once_done(log_target: str):
    """「once」模式下等待所有派生的 booster 子任务结束，再标记 running=False。
    进度通过子任务日志镜像实时显示在 schedule 日志上，这里不再额外输出。"""
    last_pending = -1
    while True:
        with booster_lock:
            stop_event = _booster_schedule["stop_event"]
            still_running = []
            for t in booster_tasks.values():
                if t.get("log_target") != log_target:
                    continue
                if t.get("stop_event") is not stop_event:
                    continue
                if t.get("status") not in ("completed", "error", "cancelled", "stopping"):
                    still_running.append(t)
        if not still_running:
            break
        # 仅在计数变化时静默记录（不输出到日志，避免淹没真实进度）
        last_pending = len(still_running)
        if stop_event.is_set() and not still_running:
            break
        time.sleep(1)
    if _booster_schedule["running"] == "once":
        _booster_schedule["running"] = False
        _append_log(log_target, "=== 手动执行已结束 ===")


@app.route("/api/booster/schedule/config", methods=["GET"])
def booster_schedule_config_get():
    cfg = _load_booster_config().get("schedule", {})
    return jsonify({
        "interval_minutes": cfg.get("interval_minutes", 60),
        "play_threshold": cfg.get("play_threshold", 200),
        "target_play": cfg.get("target_play", 200),
        "max_concurrent_tasks": cfg.get("max_concurrent_tasks", DEFAULT_MAX_CONCURRENT_BOOST_TASKS),
        "scan_limit": cfg.get("scan_limit", BOOSTER_SCAN_LIMIT_DEFAULT),
        "car_enabled": bool(cfg.get("car_enabled", True)),
        "car_rounds": cfg.get("car_rounds", BOOSTER_CAR_ROUNDS_DEFAULT),
        "account_uids": cfg.get("account_uids") or [],
        "uids": cfg.get("uids") or [],
    })


@app.route("/api/booster/user-info")
def booster_user_info():
    """批量查询 UID 的昵称与头像，用于定时任务页展示监控对象。
    参数 uids=逗号分隔（最多 100 个）；返回 {success, users: {uid: {name, face}}}。"""
    raw = (request.args.get("uids") or "").strip()
    uids = [x.strip() for x in raw.split(",") if x.strip().isdigit()][:100]
    if not uids:
        return jsonify({"success": False, "message": "缺少有效 UID"}), 400

    now = time.time()
    users = {}
    missing = []
    for uid in uids:
        hit = _WATCH_USER_CACHE.get(uid)
        if hit and now - hit[2] < 6 * 3600:
            users[uid] = {"name": hit[0], "face": hit[1]}
        else:
            missing.append(uid)

    if missing:
        # web-interface/card 未签名易被风控（-352），改用 bilibili_api 的 WBI 签名接口
        from bilibili_api.user import User as BiliUser
        loop = asyncio.new_event_loop()
        try:
            for uid in missing:
                try:
                    info = loop.run_until_complete(BiliUser(int(uid)).get_user_info())
                    name = str(info.get("name") or "")
                    face = str(info.get("face") or "")
                    if face.startswith("http://"):
                        face = "https://" + face[len("http://"):]
                    if not name:
                        continue
                    users[uid] = {"name": name, "face": face}
                    _WATCH_USER_CACHE[uid] = (name, face, now)
                except Exception:
                    continue
                time.sleep(0.2)  # 轻微限速，避免短时间批量查询触发风控
        finally:
            loop.close()
    return jsonify({"success": True, "users": users})


@app.route("/api/booster/schedule/config", methods=["POST"])
def booster_schedule_config_save():
    data = request.json or {}
    try:
        interval = int(data.get("interval_minutes", 60))
        threshold = int(data.get("play_threshold", 200))
        target = int(data.get("target_play", 200))
        concurrent = int(data.get("max_concurrent_tasks", DEFAULT_MAX_CONCURRENT_BOOST_TASKS))
        scan_limit = int(data.get("scan_limit", BOOSTER_SCAN_LIMIT_DEFAULT))
        car_rounds = int(data.get("car_rounds", BOOSTER_CAR_ROUNDS_DEFAULT))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "配置项必须为整数"}), 400
    car_enabled = bool(data.get("car_enabled", True))
    account_uids = [str(u).strip() for u in (data.get("account_uids") or []) if str(u or "").strip()]
    uids = [str(u).strip() for u in (data.get("uids") or []) if str(u or "").strip()]
    if interval < 1 or threshold < 1 or target < 1:
        return jsonify({"success": False, "message": "配置项必须为正整数"}), 400
    if car_rounds < 1:
        return jsonify({"success": False, "message": "车机长线轮数必须为正整数"}), 400
    if car_rounds > BOOSTER_CAR_ROUNDS_MAX:
        return jsonify({"success": False, "message": f"车机长线轮数上限为 {BOOSTER_CAR_ROUNDS_MAX}"}), 400
    if concurrent < 1:
        return jsonify({"success": False, "message": "同时运行稿件数必须为正整数"}), 400
    if concurrent > MAX_CONCURRENT_BOOST_TASKS_CAP:
        return jsonify({"success": False, "message": f"同时运行稿件数上限为 {MAX_CONCURRENT_BOOST_TASKS_CAP}"}), 400
    if scan_limit < 0:
        return jsonify({"success": False, "message": "扫描深度必须为 0（全量）或正整数"}), 400
    if scan_limit > BOOSTER_MAX_SCAN_LIMIT:
        return jsonify({"success": False, "message": f"扫描深度上限为 {BOOSTER_MAX_SCAN_LIMIT}，如需全量请填 0"}), 400
    if not account_uids and not uids:
        return jsonify({"success": False, "message": "请至少配置一个刷量账号或监控 UID"}), 400
    cfg = _load_booster_config()
    cfg["schedule"] = {
        "interval_minutes": interval,
        "play_threshold": threshold,
        "target_play": target,
        "max_concurrent_tasks": concurrent,
        "scan_limit": scan_limit,
        "car_enabled": car_enabled,
        "car_rounds": car_rounds,
        "account_uids": account_uids,
        "uids": uids,
    }
    _save_booster_config(cfg)
    _booster_gate.set_limit(concurrent)  # 立即生效，无需重启；正在运行的任务不受影响，仅影响后续调度
    resp = {"success": True}
    if scan_limit <= 0 or scan_limit > BOOSTER_SCAN_LIMIT_WARN:
        resp["warning"] = (f"扫描深度为{_scan_limit_label(scan_limit)}，超过 {BOOSTER_SCAN_LIMIT_WARN} 条会多次分页请求，"
                           "存在触发 B 站风控的风险")
    return jsonify(resp)


@app.route("/api/booster/schedule/start", methods=["POST"])
def booster_schedule_start():
    if _booster_schedule["running"]:
        return jsonify({"success": False, "message": "定时刷量任务已在运行"})

    _booster_schedule["stop_event"] = threading.Event()
    _booster_schedule["running"] = True
    _booster_schedule["run_count"] = 0
    _booster_schedule["last_run"] = None
    with log_lock:
        log_buffers["booster-schedule"] = []
    _append_log("booster-schedule", "定时刷量任务已启动")

    t = threading.Thread(target=_booster_schedule_loop, daemon=True)
    _booster_schedule["thread"] = t
    t.start()
    return jsonify({"success": True})


@app.route("/api/booster/schedule/stop", methods=["POST"])
def booster_schedule_stop():
    if not _booster_schedule["running"]:
        return jsonify({"success": True, "message": "未在运行"})
    _booster_schedule["stop_event"].set()
    # 同步取消持有该 event 的所有未结束子任务（含排队中的），排队任务将直接跳过不再执行
    with booster_lock:
        ev = _booster_schedule["stop_event"]
        for st in booster_tasks.values():
            if st.get("stop_event") is not ev:
                continue
            if st.get("status") in ("completed", "error", "cancelled"):
                continue
            st["status"] = "cancelled"
    return jsonify({"success": True})


@app.route("/api/booster/schedule/run-once", methods=["POST"])
def booster_schedule_run_once():
    """手动触发一次低播放量扫描（不影响定时循环）。复用 schedule 的 stop_event，以便「停止」能一并中止。"""
    if _booster_schedule["running"]:
        # 定时在跑：复用定时 stop_event，使「停止」按钮能一并中断本轮执行
        stop_event = _booster_schedule["stop_event"]
    else:
        # 定时未在跑：建立一次性的"运行中"状态，对应「停止」按钮
        _booster_schedule["stop_event"] = threading.Event()
        _booster_schedule["running"] = "once"
        _booster_schedule["last_run"] = None
        with log_lock:
            log_buffers["booster-schedule"] = []
        _append_log("booster-schedule", "=== 手动执行低播放量扫描 ===")
        stop_event = _booster_schedule["stop_event"]

    t = threading.Thread(target=_booster_schedule_once, args=(stop_event,), daemon=True)
    t.start()
    return jsonify({"success": True})


@app.route("/api/booster/schedule/status")
def booster_schedule_status():
    cfg = _load_booster_config().get("schedule", {})
    # 统计当前由本 schedule 派生的活跃子任务
    stop_event = _booster_schedule.get("stop_event")
    active_bvids = []
    with booster_lock:
        for t in booster_tasks.values():
            if t.get("stop_event") is not stop_event:
                continue
            if t.get("status") in ("completed", "error", "cancelled"):
                continue
            active_bvids.append({
                "bv": t.get("bv", ""),
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "target": t.get("target", 0),
            })
    return jsonify({
        "running": _booster_schedule["running"],
        "interval_minutes": cfg.get("interval_minutes", 60),
        "play_threshold": cfg.get("play_threshold", 200),
        "target_play": cfg.get("target_play", 200),
        "max_concurrent_tasks": _booster_gate.get_limit(),
        "scan_limit": cfg.get("scan_limit", BOOSTER_SCAN_LIMIT_DEFAULT),
        "account_uids": cfg.get("account_uids") or [],
        "uids": cfg.get("uids") or [],
        "last_run": _booster_schedule["last_run"],
        "run_count": _booster_schedule["run_count"],
        "active_tasks": active_bvids,
        "log": _get_log("booster-schedule"),
    })


# =========================================================================
#  四、B站直播间红包助手 (bili-redpocket)
# =========================================================================

REDPOCKET_DIR = str(ROOT / "bili-redpocket")
REDPOCKET_SCRIPT = os.path.join(REDPOCKET_DIR, "auto_send_red_pocket.py")
REDPOCKET_ROOMS_CONFIG = CONFIG_DIR / "redpocket_rooms.yaml"
REDPOCKET_WEBUI = os.path.join(REDPOCKET_DIR, "web_ui.py")

redpocket_process = None
redpocket_lock = threading.Lock()


def _redpocket_running():
    global redpocket_process
    if redpocket_process and redpocket_process.poll() is None:
        return True, redpocket_process.pid
    # 检查是否有残留进程
    for proc in __import__("psutil").process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.cmdline()
            if cmdline and any("auto_send_red_pocket" in c for c in cmdline):
                return True, proc.pid
        except Exception:
            continue
    return False, None


@app.route("/api/redpocket/status")
def redpocket_status():
    running, pid = _redpocket_running()
    # 获取最新日志
    log_dir = os.path.join(REDPOCKET_DIR, "logs")
    logs = ""
    if os.path.exists(log_dir):
        log_files = sorted(
            [f for f in os.listdir(log_dir) if f.endswith(".log")],
            reverse=True,
        )
        if log_files:
            try:
                with open(os.path.join(log_dir, log_files[0]), "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    logs = "".join(lines[-100:])
            except Exception:
                pass
    return jsonify({"running": running, "pid": pid, "logs": logs})


@app.route("/api/redpocket/start", methods=["POST"])
def redpocket_start():
    running, _ = _redpocket_running()
    if running:
        return jsonify({"success": False, "message": "已在运行中"})

    python_candidates = [
        sys.executable,
    ]
    python_exe = None
    for c in python_candidates:
        if os.path.exists(c):
            python_exe = c
            break
    if not python_exe:
        return jsonify({"success": False, "message": "找不到 Python 解释器"})

    global redpocket_process
    try:
        redpocket_process = subprocess.Popen(
            [python_exe, REDPOCKET_SCRIPT],
            cwd=REDPOCKET_DIR,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        return jsonify({"success": True, "message": f"已启动 PID: {redpocket_process.pid}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/redpocket/stop", methods=["POST"])
def redpocket_stop():
    running, pid = _redpocket_running()
    if not running:
        return jsonify({"success": True, "message": "未在运行"})

    try:
        # 写入停止信号文件，让脚本优雅断开连接后自行退出
        stop_file = os.path.join(REDPOCKET_DIR, ".stop_signal")
        with open(stop_file, "w") as f:
            f.write(str(pid))

        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            try:
                proc.wait(timeout=10)
            except psutil.TimeoutExpired:
                # 10秒内未自行退出，强制终止
                proc.kill()
                proc.wait(timeout=3)

        # 清理停止信号文件
        if os.path.exists(stop_file):
            os.remove(stop_file)

        return jsonify({"success": True, "message": "已停止"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ---- 红包房间管理（读写 data/redpocket_rooms.yaml） ----

def _read_watch_rooms():
    """从 redpocket_rooms.yaml 中读取监听房间列表"""
    import yaml
    if not REDPOCKET_ROOMS_CONFIG.exists():
        return []
    try:
        with open(REDPOCKET_ROOMS_CONFIG, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("rooms", [])
    except Exception as e:
        print(f"[REDPOCKET] 读取房间配置失败: {e}")
        return []


def _write_watch_rooms(rooms):
    """将监听房间列表写入 redpocket_rooms.yaml"""
    import yaml
    REDPOCKET_ROOMS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(REDPOCKET_ROOMS_CONFIG, "w", encoding="utf-8") as f:
        yaml.dump({"rooms": rooms}, f, allow_unicode=True, default_flow_style=False)


@app.route("/api/redpocket/rooms")
def redpocket_rooms():
    return jsonify({"rooms": _read_watch_rooms()})


@app.route("/api/redpocket/room", methods=["POST"])
def redpocket_add_room():
    data = request.json
    room = {
        "room_id": int(data["room_id"]),
        "red_pocket_id": int(data.get("red_pocket_id", 189)),
        "duration": int(data.get("duration", 600)),
        "count": int(data.get("count", 1)),
        "danmu_msg": data.get("danmu_msg", "老板大气！点点红包抽礼物"),
        "uname": data.get("uname", ""),
        "title": data.get("title", ""),
        "face": data.get("face", ""),
        "uid": int(data.get("uid", 0)),
        "cover_from_user": data.get("cover_from_user", ""),
    }
    # 电池红包
    if int(data.get("red_pocket_id", 189)) == 0:
        room["danmu_msg"] = ""
        room["total_battery"] = int(data.get("total_battery", 20))
        room["award_num"] = int(data.get("award_num", 10))
        room["join_requirement"] = int(data.get("join_requirement", 0))
    rooms = _read_watch_rooms()
    rooms.append(room)
    _write_watch_rooms(rooms)
    return jsonify({"success": True, "rooms": rooms})


@app.route("/api/redpocket/room/<int:index>", methods=["DELETE"])
def redpocket_del_room(index):
    rooms = _read_watch_rooms()
    if 0 <= index < len(rooms):
        rooms.pop(index)
        _write_watch_rooms(rooms)
        return jsonify({"success": True, "rooms": rooms})
    return jsonify({"success": False, "message": "索引无效"}), 400


@app.route("/api/redpocket/room/<int:index>", methods=["PUT"])
def redpocket_update_room(index):
    rooms = _read_watch_rooms()
    if not (0 <= index < len(rooms)):
        return jsonify({"success": False, "message": "索引无效"}), 400
    data = request.json
    red_pocket_id = int(data.get("red_pocket_id", rooms[index]["red_pocket_id"]))
    rooms[index].update({
        "room_id": int(data.get("room_id", rooms[index]["room_id"])),
        "red_pocket_id": red_pocket_id,
        "duration": int(data.get("duration", rooms[index].get("duration", 600))),
        "count": int(data.get("count", rooms[index].get("count", 1))),
        "danmu_msg": data.get("danmu_msg", rooms[index].get("danmu_msg", "")),
    })
    # 电池红包字段
    if red_pocket_id == 0:
        rooms[index]["danmu_msg"] = ""
        rooms[index]["total_battery"] = int(data.get("total_battery", rooms[index].get("total_battery", 20)))
        rooms[index]["award_num"] = int(data.get("award_num", rooms[index].get("award_num", 10)))
        rooms[index]["join_requirement"] = int(data.get("join_requirement", rooms[index].get("join_requirement", 0)))
    else:
        # 非电池红包，移除电池字段
        rooms[index].pop("total_battery", None)
        rooms[index].pop("award_num", None)
        rooms[index].pop("join_requirement", None)
    _write_watch_rooms(rooms)
    return jsonify({"success": True, "rooms": rooms})


# ---- 红包模块登录 (复用 web_ui.py 的扫码逻辑) ----

@app.route("/api/redpocket/login/qrcode")
def redpocket_qr():
    """生成 B站扫码登录二维码"""
    import base64
    import httpx
    import qrcode

    try:
        resp = httpx.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        qr_data = resp.json()["data"]

        img = qrcode.make(qr_data["url"])
        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()

        return jsonify({
            "success": True,
            "qrcode_key": qr_data["qrcode_key"],
            "qr_image": f"data:image/png;base64,{b64}",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/redpocket/login/poll/<qrcode_key>")
def redpocket_poll(qrcode_key):
    import httpx

    try:
        resp = httpx.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        code = data.get("data", {}).get("code")

        if code == 0:
            # 登录成功，提取 cookie
            cookies = dict(resp.cookies.items())
            sessdata = cookies.get("SESSDATA", "")
            bili_jct = cookies.get("bili_jct", "")
            buvid3 = cookies.get("buvid3", "")
            login_uid = cookies.get("DedeUserID", "")

            # 更新 config.yaml
            _update_redpocket_config(sessdata, bili_jct, buvid3, login_uid)
            return jsonify({"success": True, "message": "登录成功", "login_uid": login_uid})
        elif code == 86038:
            return jsonify({"success": False, "message": "二维码已过期"})
        elif code == 86039:
            return jsonify({"success": False, "status": "waiting", "message": "等待扫码"})
        elif code == 86040:
            return jsonify({"success": False, "status": "confirming", "message": "等待确认"})
        else:
            return jsonify({"success": False, "message": f"错误码: {code}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


def _update_redpocket_config(sessdata, bili_jct, buvid3, login_uid):
    _ensure_redpocket_config()
    import yaml

    config_path = REDPOCKET_CONFIG
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}

    cfg.setdefault("bilibili", {}).update({
        "sessdata": sessdata,
        "bili_jct": bili_jct,
        "login_uid": int(login_uid) if login_uid else 0,
    })
    if buvid3:
        cfg["bilibili"]["buvid3"] = buvid3
    cfg.setdefault("network", {"browser_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }})
    cfg.setdefault("logging", {"level": "INFO"})

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


@app.route("/api/redpocket/config")
def redpocket_config():
    _ensure_redpocket_config()
    import yaml

    config_path = REDPOCKET_CONFIG
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return jsonify({
            "sessdata": cfg.get("bilibili", {}).get("sessdata", ""),
            "login_uid": cfg.get("bilibili", {}).get("login_uid", ""),
        })
    return jsonify({"sessdata": "", "login_uid": ""})


@app.route("/api/redpocket/room-info/<room_id>")
def redpocket_room_info(room_id):
    import httpx

    try:
        resp = httpx.get(
            "https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room",
            params={"roomid": room_id},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("info"):
            return jsonify({"success": True, "uid": data["data"]["info"]["uid"]})
        return jsonify({"success": False, "message": "未找到该房间"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/redpocket/live-info/<uid>")
def redpocket_live_info(uid):
    import httpx

    try:
        resp = httpx.get(
            "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
            params={"uids[]": uid},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 and uid in data.get("data", {}):
            info = data["data"][uid]
            return jsonify({"success": True, "data": {
                "uid": uid,
                "uname": info.get("uname", ""),
                "title": info.get("title", ""),
                "room_id": info.get("room_id", 0),
                "live_status": info.get("live_status", 0),
                "face": info.get("face", ""),
                "cover_from_user": info.get("cover_from_user", ""),
                "area_v2_parent_name": info.get("area_v2_parent_name", ""),
                "area_v2_name": info.get("area_v2_name", ""),
            }})
        return jsonify({"success": False, "message": "未找到"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# =========================================================================
#  四-B、B站直播间 LiveHelper (bili-redpocket/livehelper.py)
# =========================================================================

LIVEHELPER_SCRIPT = os.path.join(REDPOCKET_DIR, "livehelper.py")
livehelper_process = None
livehelper_lock = threading.Lock()


def _livehelper_running():
    global livehelper_process
    if livehelper_process and livehelper_process.poll() is None:
        return True, livehelper_process.pid
    import psutil
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.cmdline()
            if cmdline and any("livehelper" in c for c in cmdline):
                return True, proc.pid
        except Exception:
            continue
    return False, None


def _read_livehelper_config():
    """读取 redpocket 配置中的 livehelper 配置"""
    _ensure_redpocket_config()
    import yaml
    config_path = REDPOCKET_CONFIG
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("livehelper", {})
    return {}


def _write_livehelper_config(lh_cfg):
    """更新 redpocket 配置中的 livehelper 配置"""
    _ensure_redpocket_config()
    import yaml
    config_path = REDPOCKET_CONFIG
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {"bilibili": {}, "network": {"browser_headers": {}}, "logging": {"level": "INFO"}}
    cfg["livehelper"] = lh_cfg
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


@app.route("/api/livehelper/status")
def livehelper_status():
    running, pid = _livehelper_running()
    log_dir = os.path.join(REDPOCKET_DIR, "logs")
    logs = ""
    if os.path.exists(log_dir):
        log_files = sorted(
            [f for f in os.listdir(log_dir) if f.endswith(".log")],
            reverse=True,
        )
        if log_files:
            try:
                with open(os.path.join(log_dir, log_files[0]), "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    logs = "".join(lines[-100:])
            except Exception:
                pass
    return jsonify({"running": running, "pid": pid, "logs": logs})


@app.route("/api/livehelper/start", methods=["POST"])
def livehelper_start():
    running, _ = _livehelper_running()
    if running:
        return jsonify({"success": False, "message": "已在运行中"})

    python_exe = sys.executable
    if not python_exe or not os.path.exists(python_exe):
        return jsonify({"success": False, "message": "找不到 Python 解释器"})

    global livehelper_process
    try:
        livehelper_process = subprocess.Popen(
            [python_exe, LIVEHELPER_SCRIPT],
            cwd=REDPOCKET_DIR,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        return jsonify({"success": True, "message": f"已启动 PID: {livehelper_process.pid}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/livehelper/stop", methods=["POST"])
def livehelper_stop():
    running, pid = _livehelper_running()
    if not running:
        return jsonify({"success": True, "message": "未在运行"})

    try:
        stop_file = os.path.join(REDPOCKET_DIR, ".stop_signal")
        with open(stop_file, "w") as f:
            f.write(str(pid))

        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            try:
                proc.wait(timeout=10)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

        if os.path.exists(stop_file):
            os.remove(stop_file)

        return jsonify({"success": True, "message": "已停止"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/livehelper/config", methods=["GET"])
def livehelper_config_get():
    _ensure_redpocket_config()
    import yaml
    config_path = REDPOCKET_CONFIG
    cfg = {"enabled": True, "room_id": "", "interval_seconds": 60,
           "interval_jitter_seconds": 10, "skip_duplicate": True,
           "force_qr_login": False, "credential_file": "bilibili.json", "quotes": []}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            yml = yaml.safe_load(f) or {}
            lh = yml.get("livehelper", {})
            cfg.update(lh)
            # 登录状态也返回
            bili = yml.get("bilibili", {})
            cfg["_login_uid"] = bili.get("login_uid", "")
            cfg["_has_login"] = bool(bili.get("sessdata") and bili.get("bili_jct"))
    return jsonify(cfg)


@app.route("/api/livehelper/config", methods=["POST"])
def livehelper_config_save():
    data = request.json or {}
    # 只保存 livehelper 相关字段
    lh_cfg = {
        "enabled": data.get("enabled", True),
        "room_id": data.get("room_id", ""),
        "interval_seconds": int(data.get("interval_seconds", 60)),
        "interval_jitter_seconds": int(data.get("interval_jitter_seconds", 10)),
        "skip_duplicate": data.get("skip_duplicate", True),
        "force_qr_login": data.get("force_qr_login", False),
        "credential_file": data.get("credential_file", "bilibili.json"),
        "quotes": data.get("quotes", []),
    }
    _write_livehelper_config(lh_cfg)
    return jsonify({"success": True})


# =========================================================================
#  四-C、B站直播间抢电池红包 (bili-battery-redpocket)
# =========================================================================

BATTERY_DIR = str(ROOT / "bili-battery-redpocket")
BATTERY_SCRIPT = os.path.join(BATTERY_DIR, "auto_grab_battery_red_pocket.py")

battery_process = None
battery_lock = threading.Lock()


def _battery_running():
    global battery_process
    if battery_process and battery_process.poll() is None:
        return True, battery_process.pid
    # 检查是否有残留进程
    for proc in __import__("psutil").process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.cmdline()
            if cmdline and any("auto_grab_battery_red_pocket" in c for c in cmdline):
                return True, proc.pid
        except Exception:
            continue
    return False, None


def _read_battery_config():
    """读取 redpocket 配置中的 battery_redpocket 配置"""
    _ensure_redpocket_config()
    import yaml
    config_path = REDPOCKET_CONFIG
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("battery_redpocket", {})
    return {}


def _write_battery_config(bat_cfg):
    """更新 redpocket 配置中的 battery_redpocket 配置"""
    _ensure_redpocket_config()
    import yaml
    config_path = REDPOCKET_CONFIG
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {"bilibili": {}, "network": {"browser_headers": {}}, "logging": {"level": "INFO"}}
    cfg["battery_redpocket"] = bat_cfg
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


@app.route("/api/battery/status")
def battery_status():
    running, pid = _battery_running()
    log_dir = os.path.join(BATTERY_DIR, "logs")
    logs = ""
    if os.path.exists(log_dir):
        log_files = sorted(
            [f for f in os.listdir(log_dir) if f.endswith(".log")],
            reverse=True,
        )
        if log_files:
            try:
                with open(os.path.join(log_dir, log_files[0]), "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    logs = "".join(lines[-100:])
            except Exception:
                pass
    return jsonify({"running": running, "pid": pid, "logs": logs})


@app.route("/api/battery/start", methods=["POST"])
def battery_start():
    running, _ = _battery_running()
    if running:
        return jsonify({"success": False, "message": "已在运行中"})

    python_exe = sys.executable
    if not python_exe or not os.path.exists(python_exe):
        return jsonify({"success": False, "message": "找不到 Python 解释器"})

    global battery_process
    try:
        battery_process = subprocess.Popen(
            [python_exe, BATTERY_SCRIPT],
            cwd=BATTERY_DIR,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        return jsonify({"success": True, "message": f"已启动 PID: {battery_process.pid}"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/battery/stop", methods=["POST"])
def battery_stop():
    running, pid = _battery_running()
    if not running:
        return jsonify({"success": True, "message": "未在运行"})

    try:
        stop_file = os.path.join(BATTERY_DIR, ".stop_signal")
        with open(stop_file, "w") as f:
            f.write(str(pid))

        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            try:
                proc.wait(timeout=10)
            except psutil.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)

        if os.path.exists(stop_file):
            os.remove(stop_file)

        return jsonify({"success": True, "message": "已停止"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/battery/config", methods=["GET"])
def battery_config_get():
    _ensure_redpocket_config()
    import yaml
    config_path = REDPOCKET_CONFIG
    cfg = {"enabled": True, "poll_interval": 1.0, "only_battery": True, "rooms": []}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            yml = yaml.safe_load(f) or {}
            bat = yml.get("battery_redpocket", {})
            if bat:
                cfg.update(bat)
            # 登录状态也返回
            bili = yml.get("bilibili", {})
            cfg["_login_uid"] = bili.get("login_uid", "")
            cfg["_has_login"] = bool(bili.get("sessdata") and bili.get("bili_jct"))
    return jsonify(cfg)


@app.route("/api/battery/config", methods=["POST"])
def battery_config_save():
    data = request.json or {}
    # 只保存 battery_redpocket 相关字段
    rooms = data.get("rooms", [])
    if isinstance(rooms, str):
        rooms = [r.strip() for r in rooms.replace("，", ",").split(",") if r.strip()]
    elif isinstance(rooms, list):
        rooms = [str(r).strip() for r in rooms if str(r).strip()]
    bat_cfg = {
        "enabled": data.get("enabled", True),
        "poll_interval": float(data.get("poll_interval", 1.0)),
        "only_battery": data.get("only_battery", True),
        "rooms": rooms,
    }
    _write_battery_config(bat_cfg)
    return jsonify({"success": True})


# =========================================================================
#  五、合集助手 (Collection Assistant)
# =========================================================================

COLL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://member.bilibili.com/",
    "Origin": "https://member.bilibili.com",
}


def _coll_cred():
    """读取合集助手凭证（复用自动互动的登录信息）。"""
    cred = _read_auto_cred()
    sessdata = cred.get("sessdata", "")
    bili_jct = cred.get("bili_jct", "")
    mid = cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or ""
    return sessdata, bili_jct, str(mid)


def _coll_cookies(sessdata: str, bili_jct: str, mid: str = "") -> dict:
    c = {"SESSDATA": sessdata, "bili_jct": bili_jct}
    if mid:
        c["DedeUserID"] = str(mid)
    return c


@app.route("/api/collection/status")
def collection_status():
    """检查合集助手登录状态。"""
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": True, "logged_in": False})
    return jsonify({"success": True, "logged_in": True, "mid": mid})


@app.route("/api/collection/seasons")
def collection_seasons():
    """获取用户所有新版合集（SEASON），通过创作中心 API。"""
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先在「自动互动」页面登录账号"})
    try:
        import httpx
        cookies = _coll_cookies(sessdata, bili_jct, mid)
        seasons = []
        pn = 1
        while True:
            resp = httpx.get(
                "https://member.bilibili.com/x2/creative/web/seasons",
                params={"pn": pn, "ps": 30},
                headers=COLL_HEADERS, cookies=cookies, timeout=15,
            )
            data = resp.json()
            if data.get("code") != 0:
                return jsonify({"success": False, "message": data.get("message", "获取合集列表失败")})
            items = data.get("data", {}).get("seasons", [])
            if not items:
                break
            for s in items:
                # 兼容两种结构：flat 或 nested under "season"
                _sobj = s.get("season") if isinstance(s.get("season"), dict) else s
                # 提取 section_id（合集列表 API 返回中已包含）
                _secs = s.get("sections") or _sobj.get("sections") or {}
                if isinstance(_secs, dict):
                    _sec_list = _secs.get("sections", [])
                elif isinstance(_secs, list):
                    _sec_list = _secs
                else:
                    _sec_list = []
                _section_id = _sec_list[0]["id"] if _sec_list else None
                # fallback: 从 part_episodes 中取 sectionId
                if not _section_id:
                    _peps = s.get("part_episodes", [])
                    if _peps and isinstance(_peps, list):
                        _section_id = _peps[0].get("sectionId") or _peps[0].get("section_id")
                if pn == 1 and len(seasons) == 0:
                    print(f"[COLLECTION-SEASONS] first item: season_id={_sobj.get('id')}, sections_raw_type={type(s.get('sections')).__name__}, _sec_list_len={len(_sec_list)}, section_id={_section_id}, part_episodes_len={len(s.get('part_episodes', []))}")
                seasons.append({
                    "season_id": _sobj.get("id"),
                    "title": _sobj.get("title", ""),
                    "desc": _sobj.get("desc", ""),
                    "cover": _sobj.get("cover", ""),
                    "video_count": _sobj.get("video_count", 0) or _sobj.get("ep_num", 0),
                    "state": _sobj.get("state", 0),
                    "ctime": _sobj.get("ctime", 0),
                    "section_id": _section_id,
                })
            total = data.get("data", {}).get("total", 0)
            if pn * 30 >= total:
                break
            pn += 1
        return jsonify({"success": True, "seasons": seasons, "total": len(seasons)})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/collection/season-sections/<int:season_id>")
def collection_season_sections(season_id):
    """获取合集的小节列表（含 section_id）。"""
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先登录"})
    try:
        import httpx
        cookies = _coll_cookies(sessdata, bili_jct, mid)
        resp = httpx.get(
            "https://member.bilibili.com/x2/creative/web/season/section",
            params={"id": season_id},
            headers=COLL_HEADERS, cookies=cookies, timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            return jsonify({"success": False, "message": data.get("message", "获取小节失败")})
        sections = data.get("data", {}).get("sections", [])
        result = []
        for sec in sections:
            eps = sec.get("episodes", [])
            result.append({
                "section_id": sec.get("id"),
                "title": sec.get("title", ""),
                "episode_count": len(eps),
                "episodes": [{"aid": ep.get("aid"), "cid": ep.get("cid"),
                              "title": ep.get("title", ""), "bvid": ep.get("bvid", "")}
                             for ep in eps],
            })
        return jsonify({"success": True, "sections": result})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/collection/orphan-videos", methods=["GET", "POST"])
def collection_orphan_videos():
    """检测散落稿件：获取全部投稿，对比所有合集内稿件，找出不在任何合集中的视频，并智能匹配。
    使用 bilibili_api 公开 API (api.bilibili.com) 获取合集内视频，避免 member.bilibili.com 认证问题。
    """
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先在「自动互动」页面登录账号"})

    # 获取用户自定义分组关键词
    user_keywords = []
    if request.method == "POST":
        body = request.json or {}
        user_keywords = [kw for kw in (body.get("user_keywords") or []) if isinstance(kw, str) and len(kw) >= 2]
    if user_keywords:
        print(f"[COLLECTION] 用户自定义关键词: {user_keywords}")

    try:
        from bilibili_api import Credential
        from bilibili_api.user import User, VideoOrder
        from bilibili_api.channel_series import ChannelSeriesType

        credential = Credential(
            sessdata=sessdata, bili_jct=bili_jct,
            dedeuserid=str(mid), ac_time_value="",
        )
        u = User(int(mid), credential=credential)

        loop = asyncio.new_event_loop()
        try:
            # 1) 通过 bilibili_api 获取所有合集（公开 API，无需创作中心认证）
            channels = loop.run_until_complete(u.get_channels())
            # 过滤出 SEASON 类型（新版合集）
            season_channels = [ch for ch in channels
                               if ch.get_type() == ChannelSeriesType.SEASON]

            seasons = []
            season_aids = set()

            for ch in season_channels:
                sid = ch.get_id()
                # 从缓存的 meta 中获取合集信息
                meta = ch.meta or {}
                title = meta.get("name", "") or meta.get("title", "")
                total_in_season = meta.get("total", 0)

                seasons.append({
                    "season_id": sid,
                    "title": title,
                    "video_count": total_in_season,
                    "state": 0,
                    "section_id": None,
                })

                # 2) 获取该合集内所有视频的 aid（公开 API）
                try:
                    pn2 = 1
                    while True:
                        ch_vids = loop.run_until_complete(
                            u.get_channel_videos_season(sid, pn=pn2, ps=100)
                        )
                        archives = ch_vids.get("archives", [])
                        if not archives:
                            break
                        for arch in archives:
                            aid = arch.get("aid")
                            if aid:
                                season_aids.add(aid)
                        page_info = ch_vids.get("page", {})
                        total_arch = page_info.get("total", 0)
                        if pn2 * 100 >= total_arch:
                            break
                        pn2 += 1
                except Exception as e:
                    print(f"[COLLECTION] 获取合集 {sid}({title}) 视频失败: {e}")
        finally:
            loop.close()

        # 2.5) 从创作中心 API 补充 section_id
        try:
            import httpx as _httpx
            _cookies = _coll_cookies(sessdata, bili_jct, mid)
            _pn = 1
            while True:
                _resp = _httpx.get(
                    "https://member.bilibili.com/x2/creative/web/seasons",
                    params={"pn": _pn, "ps": 30},
                    headers=COLL_HEADERS, cookies=_cookies, timeout=15,
                )
                _data = _resp.json()
                if _data.get("code") != 0:
                    break
                _items = _data.get("data", {}).get("seasons", [])
                if not _items:
                    break
                for _s in _items:
                    _sobj = _s.get("season") if isinstance(_s.get("season"), dict) else _s
                    _sid = _sobj.get("id")
                    _secs = _s.get("sections") or _sobj.get("sections") or {}
                    if isinstance(_secs, dict):
                        _sec_list = _secs.get("sections", [])
                    elif isinstance(_secs, list):
                        _sec_list = _secs
                    else:
                        _sec_list = []
                    _sec_id = _sec_list[0]["id"] if _sec_list else None
                    # fallback: 从 part_episodes 取 sectionId
                    if not _sec_id:
                        _peps = _s.get("part_episodes", [])
                        if _peps and isinstance(_peps, list):
                            _sec_id = _peps[0].get("sectionId") or _peps[0].get("section_id")
                    # 合并到 seasons 列表（补充 section_id + 添加空合集）
                    _found = False
                    for _s2 in seasons:
                        if _s2["season_id"] == _sid:
                            _s2["section_id"] = _sec_id
                            _found = True
                            break
                    if not _found and _sid:
                        # bilibili_api 没返回的合集（通常是空合集），补充进来
                        seasons.append({
                            "season_id": _sid,
                            "title": _sobj.get("title", "") or _sobj.get("name", ""),
                            "video_count": _sobj.get("ep_num", 0) or 0,
                            "state": _sobj.get("state", 0),
                            "section_id": _sec_id,
                        })
                        print(f"[COLLECTION] 补充空合集: id={_sid}, title={_sobj.get('title','')}")
                _total = _data.get("data", {}).get("total", 0)
                if _pn * 30 >= _total:
                    break
                _pn += 1
            print(f"[COLLECTION] section_id 补充完成: {sum(1 for s in seasons if s.get('section_id'))}/{len(seasons)}")
        except Exception as e:
            print(f"[COLLECTION] 补充 section_id 失败: {e}")

        # 3) 获取用户全部投稿（空间 API；防刷屏会隐藏合集内稿件，见 3a 补齐）
        all_videos_raw = []
        pn = 1
        loop2 = asyncio.new_event_loop()
        try:
            while True:
                result = loop2.run_until_complete(
                    u.get_videos(pn=pn, ps=50, order=VideoOrder.PUBDATE)
                )
                vlist = result.get("list", {}).get("vlist", [])
                if not vlist:
                    break
                for v in vlist:
                    all_videos_raw.append({
                        "aid": v.get("aid"),
                        "bvid": v.get("bvid", ""),
                        "title": v.get("title", ""),
                        "pic": v.get("pic", ""),
                        "created": v.get("created", 0),
                        "length": v.get("length", ""),
                        "play": v.get("play", 0),
                        "mid": v.get("mid"),
                    })
                total_count = result.get("page", {}).get("count", 0)
                if pn * 50 >= total_count:
                    break
                pn += 1
        finally:
            loop2.close()

        # 3a) 合集补齐：空间接口受「防刷屏」影响会隐藏合集内除首个外的稿件，
        #     用合集详情接口（不受影响）补回，并同步并入 season_aids 兜底。
        try:
            _season_cred = {"sessdata": sessdata, "bili_jct": bili_jct, "dedeuserid": str(mid)}
            _season_vids = _fetch_season_videos(_season_cred, mid, limit=0)
            if _season_vids:
                for _v in _season_vids:
                    if _v.get("aid"):
                        season_aids.add(_v["aid"])
                    _v["_from_season"] = True
                _before = len(all_videos_raw)
                all_videos_raw = _merge_videos(all_videos_raw, _season_vids)
                print(f"[COLLECTION] 合集补齐稿件: +{len(all_videos_raw) - _before}（合计 {len(all_videos_raw)}）")
        except Exception as e:
            print(f"[COLLECTION] 合集补齐失败（不影响散落扫描）: {e}")

        # 3b) 初步过滤：mid 不匹配的为联投
        #     合集接口返回不含 mid，用 _from_season 标记跳过该判定
        coop_count = 0
        normal_videos = []
        for v in all_videos_raw:
            if not v.get("_from_season") and str(v.get("mid", "")) != str(mid):
                coop_count += 1
            else:
                normal_videos.append(v)

        # 4) 筛选初步散落稿件（不在任何合集中的）
        prelim_orphans = [v for v in normal_videos if v["aid"] not in season_aids]

        # 4b) 对散落稿件并发调用视频详情 API，检查 state（仅自己可见等）
        #     联投过滤已在 3b 通过 mid 判断：mid==自己 的是发起人，保留；mid!=自己 的是参与者，已过滤
        from bilibili_api.video import Video as BiliVideo
        hidden_count = 0

        async def _check_video_state(v):
            """返回 (aid, state)"""
            try:
                vid = BiliVideo(aid=v["aid"], credential=credential)
                info = await vid.get_info()
                _state = info.get("state", 0)
                return (v["aid"], _state)
            except Exception:
                return (v["aid"], 0)

        loop3 = asyncio.new_event_loop()
        try:
            async def _run_checks():
                sem = asyncio.Semaphore(10)
                async def _limited_check(v):
                    async with sem:
                        return await _check_video_state(v)
                return await asyncio.gather(*[_limited_check(v) for v in prelim_orphans])

            results = loop3.run_until_complete(_run_checks())
        finally:
            loop3.close()

        # 根据检查结果过滤（仅过滤不可见稿件）
        exclude_aids = set()
        for aid, state in results:
            if state != 0:
                exclude_aids.add(aid)
                hidden_count += 1

        orphans = [v for v in prelim_orphans if v["aid"] not in exclude_aids]

        # debug
        debug_orphan_samples = []
        for o in orphans[:5]:
            debug_orphan_samples.append({
                "aid": o["aid"],
                "title": o["title"],
            })

        # 5) 智能匹配
        matches = _smart_match(orphans, seasons, user_keywords)

        return jsonify({
            "success": True,
            "total_videos": len(all_videos_raw),
            "coop_count": coop_count,
            "hidden_count": hidden_count,
            "seasons": seasons,
            "orphan_count": len(orphans),
            "orphans": orphans,
            "matches": matches,
            "debug_season_aids_count": len(season_aids),
            "debug_orphan_samples": debug_orphan_samples,
        })
    except Exception as e:
        import traceback
        return jsonify({"success": False, "message": f"{e}\n{traceback.format_exc()}"})


def _smart_match(orphans: list[dict], seasons: list[dict], user_keywords: list[str] = None) -> list[dict]:
    """智能匹配散落稿件到合集。

    匹配策略：
    1. 视频标题包含合集名称关键词 → 高置信度匹配
    2. 合集名称包含在视频标题中 → 高置信度
    3. 模糊相似度 > 0.5 → 中置信度
    4. 无法匹配的稿件按标题前缀分组，建议新建合集
    """
    import difflib

    # 预处理合集名称：提取关键词
    season_keywords = {}
    for s in seasons:
        title = s["title"].strip()
        # 移除常见后缀
        clean = re.sub(r'(合集|系列|全集|完整版|合辑)$', '', title).strip()
        if not clean:
            clean = title
        season_keywords[s["season_id"]] = {
            "title": title,
            "clean": clean,
            "keywords": [w for w in re.split(r'[\s/·\-_|【】\[\]()（）]+', clean) if len(w) >= 2],
        }

    matches = []
    unmatched = []

    for v in orphans:
        vtitle = v["title"].strip()
        best_match = None
        best_score = 0.0

        for s in seasons:
            sid = s["season_id"]
            info = season_keywords[sid]
            score = 0.0

            # 策略1: 合集名（清洗后）完整出现在视频标题中
            if info["clean"] and info["clean"] in vtitle:
                score = 0.95
            # 策略2: 视频标题包含合集的所有关键词
            elif info["keywords"] and all(kw in vtitle for kw in info["keywords"]):
                score = 0.85
            # 策略3: 模糊匹配
            else:
                ratio = difflib.SequenceMatcher(None, vtitle, info["clean"]).ratio()
                # 也尝试与原标题比较
                ratio2 = difflib.SequenceMatcher(None, vtitle, info["title"]).ratio()
                score = max(ratio, ratio2)
                # 部分关键词匹配加分
                if info["keywords"]:
                    hit = sum(1 for kw in info["keywords"] if kw in vtitle)
                    kw_ratio = hit / len(info["keywords"])
                    score = max(score, kw_ratio * 0.7)

            if score > best_score:
                best_score = score
                best_match = s

        if best_match and best_score >= 0.45:
            confidence = "high" if best_score >= 0.8 else "medium" if best_score >= 0.6 else "low"
            matches.append({
                "video": v,
                "season": best_match,
                "score": round(best_score, 3),
                "confidence": confidence,
            })
        else:
            unmatched.append(v)

    # 对未匹配稿件按标题前缀分组，建议新建合集
    groups = _group_by_prefix(unmatched, user_keywords)
    for g in groups:
        matches.append({
            "video": None,
            "videos": g["videos"],
            "suggested_name": g["name"],
            "is_new_group": True,
            "confidence": "suggest",
        })

    return matches


# 通用括号标记，不作为分组依据
_GENERIC_TAGS = {
    '高清', '完整版', '修复', '翻唱', 'cover', 'COVER', 'Cover',
    'MV', 'PV', '4K', '1080P', '1080p', '720P', '720p', '480P',
    'AI', 'AI修复', 'AI上色', '彩色修复', '字幕', '中字',
    '双语字幕', '中英字幕', '无损', 'FLAC', 'flac', 'HQ', 'hq',
    'Live', 'live', '现场', '演唱会', '官方', 'official',
    '重制', 'remaster', 'Remaster', 'REMIX', 'remix', 'Remix',
    '低质量', '低质量版', '高质量', '高质量版', '录像带', 'LD',
    'DVD', 'VHS', '转录', '数字化', '自调色', '音频修复',
}


def _extract_tags(title: str) -> set:
    """提取标题中所有非通用括号标记内容。"""
    raw = re.findall(r'[【\[（(]([^】\]）)]{2,})[】\]）)]', title)
    return set(t for t in raw if t not in _GENERIC_TAGS)


def _group_by_prefix(videos: list[dict], user_keywords: list[str] = None) -> list[dict]:
    """按标题相似度分组：先按用户关键词，再按共享括号标记，最后按公共前缀。"""
    if not videos:
        return []

    result = []  # 最终结果列表
    used = [False] * len(videos)

    # 策略0：按用户自定义关键词分组（最高优先级）
    if user_keywords:
        for kw in user_keywords:
            group = []
            for i in range(len(videos)):
                if not used[i] and kw in videos[i]["title"]:
                    group.append(videos[i])
                    used[i] = True
            if len(group) >= 2:
                result.append({"name": kw, "videos": group})
            elif len(group) == 1:
                # 单个匹配的不强制成组，回退给后续策略
                for i in range(len(videos)):
                    if used[i] and videos[i] is group[0]:
                        used[i] = False
                        break

    # 提取每个视频的非通用括号标记
    vid_info = [(_extract_tags(v["title"]), v) for v in videos]

    # 策略1：按共享括号标记贪心分组
    auto_groups = []  # 自动分组（需要后续命名）

    for i in range(len(vid_info)):
        if used[i] or not vid_info[i][0]:
            continue
        tags_i = vid_info[i][0]
        group = [vid_info[i][1]]
        used[i] = True
        for j in range(i + 1, len(vid_info)):
            if used[j] or not vid_info[j][0]:
                continue
            if tags_i & vid_info[j][0]:  # 有共享标记
                group.append(vid_info[j][1])
                used[j] = True
        if len(group) >= 2:
            auto_groups.append(group)
        else:
            used[i] = False  # 单个不算组，回退

    # 策略2：未分组的用公共前缀匹配
    ungrouped = [vid_info[i][1] for i in range(len(vid_info)) if not used[i]]
    if ungrouped:
        sorted_vids = sorted(ungrouped, key=lambda v: v["title"])
        current_group = [sorted_vids[0]]
        for v in sorted_vids[1:]:
            prefix = _common_prefix(current_group[0]["title"], v["title"])
            if len(prefix.strip()) >= 3:
                current_group.append(v)
            else:
                auto_groups.append(current_group)
                current_group = [v]
        auto_groups.append(current_group)

    # 为自动分组生成名称
    for g in auto_groups:
        if len(g) >= 2:
            tag_sets = [_extract_tags(v["title"]) for v in g]
            shared = tag_sets[0]
            for ts in tag_sets[1:]:
                shared = shared & ts
            if shared:
                name = ' / '.join(sorted(shared))
            else:
                name = g[0]["title"]
                for v in g[1:]:
                    name = _common_prefix(name, v["title"])
                name = name.strip().rstrip("EPep第期集话回 partPartPART -_|·/\\【】[]()（）《》〈〉")
                if not name or len(name) < 2:
                    name = g[0]["title"][:15]
            result.append({"name": name, "videos": g})
        else:
            result.append({"name": g[0]["title"][:20], "videos": g})
    return result


def _common_prefix(a: str, b: str) -> str:
    """返回两个字符串的公共前缀。"""
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return a[:i]


def _get_section_id_for_season(season_id, cookies):
    """从合集列表 API 中查找指定合集的 section_id。"""
    import httpx
    pn = 1
    while True:
        resp = httpx.get(
            "https://member.bilibili.com/x2/creative/web/seasons",
            params={"pn": pn, "ps": 30},
            headers=COLL_HEADERS, cookies=cookies, timeout=15,
        )
        print(f"[COLLECTION] seasons API HTTP {resp.status_code} pn={pn}")
        try:
            data = resp.json()
        except Exception as e:
            print(f"[COLLECTION] seasons API JSON parse error: {e}, body={resp.text[:500]}")
            return None
        print(f"[COLLECTION] seasons API code={data.get('code')} msg={data.get('message')}")
        if data.get("code") != 0:
            return None
        items = data.get("data", {}).get("seasons", [])
        if not items:
            break
        # 打印第一个 item 的完整结构（仅第一页第一个）
        if pn == 1:
            import json as _json
            _first = items[0]
            print(f"[COLLECTION] first item keys: {list(_first.keys())}")
            _s_season = _first.get("season")
            if isinstance(_s_season, dict):
                print(f"[COLLECTION] first item season.id={_s_season.get('id')}")
            _s_secs = _first.get("sections")
            print(f"[COLLECTION] first item sections type={type(_s_secs).__name__}")
            if isinstance(_s_secs, dict):
                _inner = _s_secs.get("sections")
                print(f"[COLLECTION] first item sections.sections type={type(_inner).__name__}, len={len(_inner) if isinstance(_inner, list) else 'N/A'}")
                if isinstance(_inner, list) and _inner:
                    print(f"[COLLECTION] first section: {_json.dumps(_inner[0], ensure_ascii=False)[:300]}")
            elif isinstance(_s_secs, list):
                print(f"[COLLECTION] first item sections(list) len={len(_s_secs)}")

        for s in items:
            # 兼容两种结构：s["id"] 或 s["season"]["id"]
            _sid = s.get("id")
            if not _sid and isinstance(s.get("season"), dict):
                _sid = s["season"].get("id")
            if _sid == season_id:
                # 兼容多种 sections 结构
                _secs = s.get("sections")
                if isinstance(_secs, dict):
                    _sec_list = _secs.get("sections", [])
                elif isinstance(_secs, list):
                    _sec_list = _secs
                else:
                    _sec_list = []
                # 也可能 sections 在 season 子对象里
                if not _sec_list and isinstance(s.get("season"), dict):
                    _secs2 = s["season"].get("sections")
                    if isinstance(_secs2, dict):
                        _sec_list = _secs2.get("sections", [])
                    elif isinstance(_secs2, list):
                        _sec_list = _secs2
                print(f"[COLLECTION] matched season_id={season_id}, _sec_list={_sec_list}")
                if _sec_list:
                    return _sec_list[0]["id"]
                # fallback: 从 part_episodes 中取 sectionId
                _peps = s.get("part_episodes", [])
                if _peps and isinstance(_peps, list):
                    _sid2 = _peps[0].get("sectionId") or _peps[0].get("section_id")
                    if _sid2:
                        print(f"[COLLECTION] got section_id from part_episodes: {_sid2}")
                        return _sid2
                return None
        total = data.get("data", {}).get("total", 0)
        if pn * 30 >= total:
            break
        pn += 1
    print(f"[COLLECTION] season_id={season_id} not found in seasons list")
    return None


@app.route("/api/collection/add-to-season", methods=["POST"])
def collection_add_to_season():
    """批量添加稿件到指定合集。
    Body: {"season_id": int, "videos": [{"aid": int, "cid": int, "title": str}]}
    如果未提供 cid，会自动查询。
    """
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先登录"})
    data = request.json or {}
    season_id = data.get("season_id")
    section_id = data.get("section_id")  # 前端可能直接传
    videos = data.get("videos", [])
    if not season_id or not videos:
        return jsonify({"success": False, "message": "缺少 season_id 或 videos"})

    try:
        import httpx
        cookies = _coll_cookies(sessdata, bili_jct, mid)

        # 优先用前端传来的 section_id，否则从合集列表 API 查找
        if not section_id:
            section_id = _get_section_id_for_season(season_id, cookies)
        print(f"[COLLECTION] add_to_season: season_id={season_id}, section_id={section_id}")
        if not section_id:
            return jsonify({"success": False, "message": f"合集 {season_id} 没有可用的小节"})

        # 补全 cid（如果缺失）
        for v in videos:
            if not v.get("cid"):
                v["cid"] = _fetch_cid(v["aid"], sessdata)

        # 构建 episodes
        episodes = []
        for v in videos:
            episodes.append({
                "aid": v["aid"],
                "cid": v.get("cid", 0),
                "title": v.get("title", ""),
                "charging_pay": 0,
            })

        # 分批添加（每批最多 20 个），批次间冷却 2 秒
        import time as _time
        added = 0
        errors = []
        for i in range(0, len(episodes), 20):
            batch = episodes[i:i + 20]
            rdata = _coll_post_with_retry(
                "https://member.bilibili.com/x2/creative/web/season/section/episodes/add",
                cookies, {"sectionId": section_id, "episodes": batch}, bili_jct,
            )
            print(f"[COLLECTION] add batch {i//20+1}: code={rdata.get('code')} msg={rdata.get('message')}")
            if rdata.get("code") == 0:
                added += len(batch)
            else:
                errors.append(f"批次 {i // 20 + 1}: {rdata.get('message', '未知错误')}")
            # 批次间冷却
            if i + 20 < len(episodes):
                _time.sleep(2)

        return jsonify({
            "success": len(errors) == 0,
            "added": added,
            "total": len(episodes),
            "errors": errors,
            "message": f"成功添加 {added}/{len(episodes)} 个稿件" + (f"，{len(errors)} 个错误" if errors else ""),
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/collection/create-and-add", methods=["POST"])
def collection_create_and_add():
    """创建新合集并添加稿件。
    Body: {"title": str, "desc": str, "videos": [{"aid": int, "cid": int, "title": str}]}
    """
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先登录"})
    data = request.json or {}
    title = data.get("title", "").strip()
    desc = data.get("desc", "")
    videos = data.get("videos", [])
    if not title:
        return jsonify({"success": False, "message": "合集名称不能为空"})

    try:
        import httpx
        cookies = _coll_cookies(sessdata, bili_jct, mid)

        # 获取封面：使用第一个视频的封面
        cover_url = ""
        if videos and videos[0].get("pic"):
            cover_url = videos[0]["pic"]
        if not cover_url:
            # 使用默认封面
            cover_url = "https://i0.hdslb.com/bfs/archive/default_cover.png"

        # 创建合集
        resp = httpx.post(
            "https://member.bilibili.com/x2/creative/web/season/add",
            data={
                "title": title,
                "desc": desc,
                "cover": cover_url,
                "season_price": 0,
                "csrf": bili_jct,
            },
            headers=COLL_HEADERS, cookies=cookies, timeout=30,
        )
        rdata = resp.json()
        if rdata.get("code") != 0:
            return jsonify({"success": False, "message": f"创建合集失败: {rdata.get('message', '未知错误')}"})

        new_season_id = rdata.get("data")
        if not new_season_id:
            return jsonify({"success": False, "message": "创建合集返回数据异常"})

        # 等待合集就绪，获取 section_id
        import time as _time
        section_id = None
        for _ in range(5):
            _time.sleep(1)
            section_id = _get_section_id_for_season(new_season_id, cookies)
            if section_id:
                break

        if not section_id:
            return jsonify({
                "success": True,
                "season_id": new_season_id,
                "added": 0,
                "message": f"合集「{title}」已创建(ID:{new_season_id})，但获取小节失败，请手动添加稿件",
            })

        # 补全 cid
        for v in videos:
            if not v.get("cid"):
                v["cid"] = _fetch_cid(v["aid"], sessdata)

        # 添加稿件，批次间冷却 2 秒
        episodes = [{"aid": v["aid"], "cid": v.get("cid", 0),
                      "title": v.get("title", ""), "charging_pay": 0} for v in videos]
        added = 0
        errors = []
        for i in range(0, len(episodes), 20):
            batch = episodes[i:i + 20]
            add_data = _coll_post_with_retry(
                "https://member.bilibili.com/x2/creative/web/season/section/episodes/add",
                cookies, {"sectionId": section_id, "episodes": batch}, bili_jct,
            )
            if add_data.get("code") == 0:
                added += len(batch)
            else:
                errors.append(f"批次 {i // 20 + 1}: {add_data.get('message', '未知错误')}")
            if i + 20 < len(episodes):
                _time.sleep(2)

        return jsonify({
            "success": True,
            "season_id": new_season_id,
            "added": added,
            "total": len(episodes),
            "errors": errors,
            "message": f"合集「{title}」已创建，成功添加 {added}/{len(episodes)} 个稿件",
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


def _coll_post_with_retry(url, cookies, json_body, bili_jct, max_retries=3, cooldown=3):
    """合集写操作封装：遇到 20111（编辑过于频繁）自动等待重试。"""
    import httpx, time as _time
    for attempt in range(max_retries + 1):
        resp = httpx.post(
            url,
            params={"csrf": bili_jct},
            json=json_body,
            headers={**COLL_HEADERS, "Content-Type": "application/json"},
            cookies=cookies, timeout=30,
        )
        rdata = resp.json()
        code = rdata.get("code")
        if code == 0:
            return rdata
        if code == 20111 and attempt < max_retries:
            wait = cooldown * (attempt + 1)
            print(f"[COLLECTION] 编辑过于频繁，等待 {wait}s 后重试 (第{attempt+1}次)")
            _time.sleep(wait)
            continue
        return rdata
    return rdata


def _fetch_cid(aid: int, sessdata: str) -> int:
    """通过 aid 获取视频 cid。"""
    try:
        import httpx
        resp = httpx.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"aid": aid},
            headers={"User-Agent": COLL_HEADERS["User-Agent"]},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            return data["data"].get("cid", 0)
    except Exception:
        pass
    return 0


@app.route("/api/collection/batch-execute", methods=["POST"])
def collection_batch_execute():
    """批量执行智能匹配结果。
    Body: {
        "actions": [
            {"type": "add", "season_id": int, "videos": [...]},
            {"type": "create", "title": str, "videos": [...]}
        ]
    }
    """
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先登录"})
    data = request.json or {}
    actions = data.get("actions", [])
    if not actions:
        return jsonify({"success": False, "message": "没有要执行的操作"})

    import time as _time
    results = []
    for idx, act in enumerate(actions):
        if act["type"] == "add":
            # 内部调用添加逻辑
            _add_payload = {"season_id": act["season_id"], "videos": act["videos"]}
            if act.get("section_id"):
                _add_payload["section_id"] = act["section_id"]
            with app.test_request_context(
                "/api/collection/add-to-season", method="POST",
                json=_add_payload,
            ):
                resp = collection_add_to_season()
                results.append(resp.get_json())
        elif act["type"] == "create":
            with app.test_request_context(
                "/api/collection/create-and-add", method="POST",
                json={"title": act["title"], "desc": "", "videos": act["videos"]},
            ):
                resp = collection_create_and_add()
                results.append(resp.get_json())
        # 操作间冷却 3 秒，避免编辑过于频繁
        if idx < len(actions) - 1:
            _time.sleep(3)

    ok_count = sum(1 for r in results if r.get("success"))
    return jsonify({
        "success": True,
        "results": results,
        "message": f"执行完成：{ok_count}/{len(results)} 个操作成功",
    })


@app.route("/api/collection/resort-season", methods=["POST"])
def collection_resort_season():
    """按投稿时间重排合集内稿件顺序。
    Body: {"season_id": int, "order": "pub_asc" | "pub_desc"}
    """
    sessdata, bili_jct, mid = _coll_cred()
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先登录"})
    data = request.json or {}
    season_id = data.get("season_id")
    order = data.get("order", "pub_asc")
    if not season_id:
        return jsonify({"success": False, "message": "缺少 season_id"})

    print(f"[COLLECTION-RESORT] === 用户传入 season_id={season_id}, order={order} ===")

    try:
        import httpx
        cookies = _coll_cookies(sessdata, bili_jct, mid)

        # 1) 先从 seasons 列表 API 获取该合集的 section 信息
        #    同时打印所有合集的 id -> title 映射，方便调试
        sec_titles = {}   # sectionId -> title
        sec_types = {}    # sectionId -> type
        sec_ids = []      # 该合集的所有 section_id
        all_seasons_map = {}  # season_id -> title (调试用)
        _pn = 1
        while True:
            _resp = httpx.get(
                "https://member.bilibili.com/x2/creative/web/seasons",
                params={"pn": _pn, "ps": 30},
                headers=COLL_HEADERS, cookies=cookies, timeout=15,
            )
            _rdata = _resp.json()
            if _rdata.get("code") != 0:
                break
            _items = _rdata.get("data", {}).get("seasons", [])
            if not _items:
                break
            for _s in _items:
                _sobj = _s.get("season") if isinstance(_s.get("season"), dict) else _s
                _sid = _sobj.get("id")
                _stitle = _sobj.get("title", "")
                all_seasons_map[_sid] = _stitle
                if _sid == season_id:
                    _secs = _s.get("sections") or _sobj.get("sections") or {}
                    if isinstance(_secs, dict):
                        _sec_list = _secs.get("sections", [])
                    elif isinstance(_secs, list):
                        _sec_list = _secs
                    else:
                        _sec_list = []
                    for _sc in _sec_list:
                        sec_titles[_sc["id"]] = _sc.get("title", "正片")
                        sec_types[_sc["id"]] = _sc.get("type", 1)
                        sec_ids.append(_sc["id"])
                    # 也从 part_episodes 补充 sectionId
                    _peps = _s.get("part_episodes") or []
                    for _ep in _peps:
                        _esid = _ep.get("sectionId") or _ep.get("section_id")
                        if _esid and _esid not in sec_titles:
                            sec_titles[_esid] = "正片"
                            sec_ids.append(_esid)
            _total = _rdata.get("data", {}).get("total", 0)
            if _pn * 30 >= _total:
                break
            _pn += 1

        print(f"[COLLECTION-RESORT] 创作中心所有合集({len(all_seasons_map)}个): {all_seasons_map}")
        print(f"[COLLECTION-RESORT] 目标合集 section_ids={sec_ids}, titles={sec_titles}")

        if not sec_ids:
            return jsonify({"success": False, "message": f"在创作中心找不到 season_id={season_id} 的合集"})

        # 2) 对每个 section_id，调用 season/section API 获取视频列表
        #    用 episode 自带的 sectionId 确定归属（API 可能返回整个合集的视频）
        #    加去重，避免同一视频被多次添加
        from collections import defaultdict
        all_videos = []  # (section_id, section_title, section_type, episode_id, aid)
        seen_ep_ids = set()

        for _sec_id in sec_ids:
            _sec_title = sec_titles.get(_sec_id, "正片")
            _sec_type = sec_types.get(_sec_id, 1)
            resp = httpx.get(
                "https://member.bilibili.com/x2/creative/web/season/section",
                params={"id": _sec_id},
                headers=COLL_HEADERS, cookies=cookies, timeout=15,
            )
            sec_data = resp.json()
            if sec_data.get("code") != 0:
                print(f"[COLLECTION-RESORT] section API id={_sec_id} 失败: {sec_data.get('message')}")
                continue
            _d = sec_data.get("data", {})
            raw_episodes = _d.get("episodes", [])
            _ret_sec = _d.get("section", {})
            print(f"[COLLECTION-RESORT] section API id={_sec_id}: {len(raw_episodes)} episodes, "
                  f"returned section.id={_ret_sec.get('id')}, seasonId={_ret_sec.get('seasonId')}")
            for ep in raw_episodes:
                ep_id = ep.get("id")
                aid = ep.get("aid")
                if not ep_id or not aid:
                    continue
                if ep_id in seen_ep_ids:
                    continue
                seen_ep_ids.add(ep_id)
                # 用 episode 自带的 sectionId 确定真实归属
                ep_sec_id = ep.get("sectionId") or _sec_id
                ep_sec_title = sec_titles.get(ep_sec_id, _sec_title)
                ep_sec_type = sec_types.get(ep_sec_id, _sec_type)
                # 如果发现了新的 section_id，补充到 sec_titles/sec_types
                if ep_sec_id not in sec_titles:
                    sec_titles[ep_sec_id] = ep_sec_title
                    sec_types[ep_sec_id] = ep_sec_type
                    sec_ids.append(ep_sec_id)
                all_videos.append((ep_sec_id, ep_sec_title, ep_sec_type, ep_id, aid))

        if not all_videos:
            return jsonify({"success": False, "message": "合集内没有视频，无需排序"})

        print(f"[COLLECTION-RESORT] total videos to sort: {len(all_videos)}")

        # 2) 并发获取每个视频的投稿时间（pubdate 是 Unix 秒级时间戳，精确到秒）
        from bilibili_api import Credential
        from bilibili_api.video import Video as BiliVideo

        credential = Credential(
            sessdata=sessdata, bili_jct=bili_jct,
            dedeuserid=str(mid), ac_time_value="",
        )

        async def _get_pubdate(item):
            sec_id, sec_title, sec_type, ep_id, aid = item
            try:
                vid = BiliVideo(aid=aid, credential=credential)
                info = await vid.get_info()
                pubdate = info.get("pubdate", 0) or info.get("ctime", 0)
                return (sec_id, sec_title, sec_type, ep_id, aid, pubdate)
            except Exception as e:
                print(f"[COLLECTION-RESORT] get_info failed for aid={aid}: {e}")
                return (sec_id, sec_title, sec_type, ep_id, aid, 0)

        loop = asyncio.new_event_loop()
        try:
            async def _run():
                sem = asyncio.Semaphore(10)
                async def _limited(item):
                    async with sem:
                        return await _get_pubdate(item)
                return await asyncio.gather(*[_limited(v) for v in all_videos])
            results = loop.run_until_complete(_run())
        finally:
            loop.close()

        # 打印排序前的投稿时间
        from datetime import datetime
        for sec_id, sec_title, sec_type, ep_id, aid, pubdate in results:
            ts = datetime.fromtimestamp(pubdate).strftime("%Y-%m-%d %H:%M:%S") if pubdate else "unknown"
            print(f"[COLLECTION-RESORT]   aid={aid} ep_id={ep_id} pubdate={pubdate} ({ts})")

        # 3) 按小节分组，每组内按投稿时间排序
        reverse = (order == "pub_desc")
        from collections import defaultdict
        section_groups = defaultdict(lambda: {"title": "", "type": 1, "videos": []})
        for sec_id, sec_title, sec_type, ep_id, aid, pubdate in results:
            section_groups[sec_id]["title"] = sec_title
            section_groups[sec_id]["type"] = sec_type
            section_groups[sec_id]["videos"].append((ep_id, aid, pubdate))

        # 打印分组结果
        print(f"[COLLECTION-RESORT] 分组结果: {len(section_groups)} 个小节")
        for _sid, _grp in section_groups.items():
            print(f"[COLLECTION-RESORT]   小节 {_sid} ({_grp['title']}): {len(_grp['videos'])} 个视频")

        # 对每个小节排序并调用 edit API
        total_sorted = 0
        errors = []
        for sec_id, grp in section_groups.items():
            eps = grp["videos"]
            sec_title = grp["title"]
            sec_type = grp["type"]
            eps.sort(key=lambda x: x[2], reverse=reverse)
            sorts = [{"id": ep_id, "sort": i} for i, (ep_id, _, _) in enumerate(eps, 1)]
            total_sorted += len(sorts)

            print(f"[COLLECTION-RESORT] submitting section {sec_id} ({sec_title}) type={sec_type}: {len(sorts)} sorts")
            for s in sorts:
                print(f"[COLLECTION-RESORT]   sort: id={s['id']} sort={s['sort']}")

            rdata = _coll_post_with_retry(
                "https://member.bilibili.com/x2/creative/web/season/section/edit",
                cookies, {
                    "section": {
                        "id": sec_id,
                        "type": sec_type,
                        "seasonId": season_id,
                        "title": sec_title,
                    },
                    "sorts": sorts,
                }, bili_jct,
            )
            print(f"[COLLECTION-RESORT] edit response: code={rdata.get('code')} msg={rdata.get('message')} data={rdata.get('data')}")
            # 20081: 正片分组冲突，尝试用 type=0 重试
            if rdata.get("code") == 20081 and sec_type != 0:
                print(f"[COLLECTION-RESORT] 20081 冲突，尝试 type=0 重试")
                rdata = _coll_post_with_retry(
                    "https://member.bilibili.com/x2/creative/web/season/section/edit",
                    cookies, {
                        "section": {
                            "id": sec_id,
                            "type": 0,
                            "seasonId": season_id,
                            "title": sec_title,
                        },
                        "sorts": sorts,
                    }, bili_jct,
                )
                print(f"[COLLECTION-RESORT] retry type=0 response: code={rdata.get('code')} msg={rdata.get('message')}")
            if rdata.get("code") != 0:
                errors.append(f"小节{sec_id}: {rdata.get('message', '未知错误')}")
            # 多小节间冷却
            if len(section_groups) > 1:
                import time as _time
                _time.sleep(2)

        if errors:
            return jsonify({"success": False, "message": f"部分排序失败: {'; '.join(errors)}"})

        return jsonify({
            "success": True,
            "message": f"排序完成：{total_sorted} 个视频已按{'投稿时间倒序' if reverse else '投稿时间正序'}重排",
        })

    except Exception as e:
        import traceback
        return jsonify({"success": False, "message": f"{e}\n{traceback.format_exc()}"})


# =========================================================================
#  七、标签搜索助手
# =========================================================================

_TAG_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tag_cache.json")

# 创作中心「编辑稿件」页的「分区」栏显示的是稿件详情里的 human_type2（新版分区字段），
# 与公开接口的 tid/tid_v2 是两套互不同步的分区体系，同一稿件会显示成不同分区。
# 该字段只能通过创作中心接口（x/vupre/web/archive/view）取，且仅本人稿件有效，故做内存缓存。
_TC_ZONE_CACHE = {}
_TC_ZONE_TTL = 6 * 3600

# B站 tid_v2 -> 一级分区名称映射（新版分区体系）
# 依据视频页 channelKv 官方分区树（31 个一级分区 / 236 个 tid）生成
_V2_TID_MAP = {
    # 人工智能
    1011:"人工智能", 2096:"人工智能", 2097:"人工智能", 2098:"人工智能",
    # 动物
    1024:"动物", 2167:"动物", 2168:"动物", 2169:"动物", 2170:"动物",
    2171:"动物",
    # 汽车
    1013:"汽车", 2106:"汽车", 2107:"汽车", 2108:"汽车", 2109:"汽车",
    2110:"汽车",
    # 影视
    1001:"影视", 2001:"影视", 2002:"影视", 2003:"影视", 2004:"影视",
    2005:"影视", 2006:"影视", 2007:"影视", 2008:"影视",
    # 舞蹈
    1004:"舞蹈", 2028:"舞蹈", 2029:"舞蹈", 2030:"舞蹈", 2031:"舞蹈",
    2032:"舞蹈", 2033:"舞蹈", 2034:"舞蹈", 2035:"舞蹈", 2036:"舞蹈",
    # 动画
    1005:"动画", 2037:"动画", 2038:"动画", 2039:"动画", 2040:"动画",
    2041:"动画", 2042:"动画", 2043:"动画", 2044:"动画", 2045:"动画",
    2046:"动画", 2047:"动画", 2048:"动画", 2049:"动画", 2050:"动画",
    2051:"动画", 2052:"动画", 2053:"动画", 2054:"动画",
    # 情感
    1027:"情感", 2185:"情感", 2186:"情感", 2187:"情感", 2188:"情感",
    # 娱乐
    1002:"娱乐", 2009:"娱乐", 2010:"娱乐", 2011:"娱乐", 2012:"娱乐",
    2013:"娱乐", 2014:"娱乐", 2015:"娱乐",
    # 时尚美妆
    1014:"时尚美妆", 2111:"时尚美妆", 2112:"时尚美妆", 2113:"时尚美妆", 2114:"时尚美妆",
    2115:"时尚美妆", 2116:"时尚美妆", 2117:"时尚美妆", 2118:"时尚美妆", 2119:"时尚美妆",
    # 美食
    1020:"美食", 2149:"美食", 2150:"美食", 2151:"美食", 2152:"美食",
    2153:"美食",
    # 游戏
    1008:"游戏", 2064:"游戏", 2065:"游戏", 2066:"游戏", 2067:"游戏",
    2068:"游戏", 2069:"游戏", 2070:"游戏", 2071:"游戏", 2072:"游戏",
    2073:"游戏", 2074:"游戏", 2075:"游戏", 2076:"游戏", 2077:"游戏",
    2078:"游戏", 2079:"游戏",
    # 健身
    1017:"健身", 2128:"健身", 2129:"健身", 2130:"健身", 2131:"健身",
    2132:"健身",
    # 手工
    1019:"手工", 2143:"手工", 2144:"手工", 2145:"手工", 2146:"手工",
    2147:"手工", 2148:"手工",
    # 健康
    1026:"健康", 2179:"健康", 2180:"健康", 2181:"健康", 2182:"健康",
    2183:"健康", 2184:"健康",
    # 家装房产
    1015:"家装房产", 2120:"家装房产", 2121:"家装房产", 2122:"家装房产", 2123:"家装房产",
    # 资讯
    1009:"资讯", 2080:"资讯", 2081:"资讯", 2082:"资讯", 2083:"资讯",
    # 鬼畜
    1007:"鬼畜", 2059:"鬼畜", 2060:"鬼畜", 2061:"鬼畜", 2062:"鬼畜",
    2063:"鬼畜",
    # 知识
    1010:"知识", 2084:"知识", 2085:"知识", 2086:"知识", 2087:"知识",
    2088:"知识", 2089:"知识", 2090:"知识", 2091:"知识", 2092:"知识",
    2093:"知识", 2094:"知识", 2095:"知识",
    # 生活经验
    1031:"生活经验", 2203:"生活经验", 2204:"生活经验", 2205:"生活经验",
    # 生活兴趣
    1030:"生活兴趣", 2198:"生活兴趣", 2199:"生活兴趣", 2200:"生活兴趣", 2201:"生活兴趣",
    2202:"生活兴趣",
    # 音乐
    1003:"音乐", 2016:"音乐", 2017:"音乐", 2018:"音乐", 2019:"音乐",
    2020:"音乐", 2021:"音乐", 2022:"音乐", 2023:"音乐", 2024:"音乐",
    2025:"音乐", 2026:"音乐", 2027:"音乐",
    # 神秘学
    1028:"神秘学", 2189:"神秘学", 2190:"神秘学", 2191:"神秘学", 2192:"神秘学",
    2193:"神秘学",
    # 户外潮流
    1016:"户外潮流", 2124:"户外潮流", 2125:"户外潮流", 2126:"户外潮流", 2127:"户外潮流",
    # 绘画
    1006:"绘画", 2055:"绘画", 2056:"绘画", 2057:"绘画", 2058:"绘画",
    # 亲子
    1025:"亲子", 2172:"亲子", 2173:"亲子", 2174:"亲子", 2175:"亲子",
    2176:"亲子", 2177:"亲子", 2178:"亲子",
    # 三农
    1023:"三农", 2162:"三农", 2163:"三农", 2164:"三农", 2165:"三农",
    2166:"三农",
    # 小剧场
    1021:"小剧场", 2154:"小剧场", 2155:"小剧场", 2156:"小剧场", 2157:"小剧场",
    # 体育运动
    1018:"体育运动", 2133:"体育运动", 2134:"体育运动", 2135:"体育运动", 2136:"体育运动",
    2137:"体育运动", 2138:"体育运动", 2139:"体育运动", 2140:"体育运动", 2141:"体育运动",
    2142:"体育运动",
    # 科技数码
    1012:"科技数码", 2099:"科技数码", 2100:"科技数码", 2101:"科技数码", 2102:"科技数码",
    2103:"科技数码", 2104:"科技数码", 2105:"科技数码",
    # 旅游出行
    1022:"旅游出行", 2158:"旅游出行", 2159:"旅游出行", 2160:"旅游出行", 2161:"旅游出行",
    # vlog
    1029:"vlog", 2194:"vlog", 2195:"vlog", 2196:"vlog", 2197:"vlog",
}

# 旧版 tid -> 一级分区名称映射（兜底）
_TID_MAP = {
    24:"动画",25:"动画",47:"动画",27:"动画",210:"动画",86:"动画",
    32:"番剧",33:"番剧",51:"番剧",152:"番剧",
    153:"国创",168:"国创",169:"国创",195:"国创",170:"国创",
    28:"音乐",31:"音乐",30:"音乐",29:"音乐",130:"音乐",193:"音乐",194:"音乐",59:"音乐",
    20:"舞蹈",154:"舞蹈",156:"舞蹈",164:"舞蹈",
    17:"游戏",171:"游戏",172:"游戏",65:"游戏",173:"游戏",
    201:"知识",124:"知识",122:"知识",39:"知识",208:"知识",209:"知识",228:"知识",207:"知识",
    95:"科技",230:"科技",231:"科技",232:"科技",233:"科技",
    235:"运动",249:"运动",250:"运动",245:"运动",246:"运动",247:"运动",248:"运动",
    223:"汽车",240:"汽车",227:"汽车",229:"汽车",
    21:"生活",161:"生活",162:"生活",163:"生活",254:"生活",253:"生活",252:"生活",251:"生活",239:"生活",138:"生活",
    76:"美食",212:"美食",213:"美食",214:"美食",215:"美食",216:"美食",
    218:"动物圈",219:"动物圈",220:"动物圈",221:"动物圈",222:"动物圈",75:"动物圈",
    22:"鬼畜",26:"鬼畜",126:"鬼畜",127:"鬼畜",
    157:"时尚",158:"时尚",159:"时尚",192:"时尚",
    202:"资讯",203:"资讯",204:"资讯",205:"资讯",206:"资讯",
    71:"娱乐",137:"娱乐",131:"娱乐",
    182:"影视",183:"影视",85:"影视",184:"影视",
    37:"纪录片",178:"纪录片",
    147:"电影",145:"电影",146:"电影",
    185:"电视剧",187:"电视剧",
}


def _load_tag_cache() -> dict:
    if os.path.exists(_TAG_CACHE_FILE):
        try:
            with open(_TAG_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_scan": None, "videos": {}}


def _save_tag_cache(cache: dict):
    os.makedirs(os.path.dirname(_TAG_CACHE_FILE), exist_ok=True)
    with open(_TAG_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ---- 标签扫描后台任务状态 ----
_ts_scan_lock = threading.Lock()
_ts_scan_state = {"running": False, "progress": "", "done": 0, "total": 0, "error": None, "result": None}


def _ts_log(msg):
    """安全日志，不依赖 stdout。"""
    try:
        print(msg)
    except Exception:
        pass


@app.route("/api/tag-search/status")
def tag_search_status():
    """返回缓存状态：视频数、标签数、上次扫描时间。"""
    try:
        sessdata, bili_jct, mid = _coll_cred()
        cache = _load_tag_cache()
        all_tags = set()
        for v in cache.get("videos", {}).values():
            all_tags.update(v.get("tags", []))
        return jsonify({
            "success": True,
            "logged_in": bool(sessdata and mid),
            "mid": mid,
            "video_count": len(cache.get("videos", {})),
            "tag_count": len(all_tags),
            "last_scan": cache.get("last_scan"),
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/tag-search/scan", methods=["POST"])
def tag_search_scan():
    """启动后台扫描任务，立即返回。前端通过 /scan-status 轮询进度。"""
    try:
        sessdata, bili_jct, mid = _coll_cred()
        if not sessdata or not mid:
            return jsonify({"success": False, "message": "请先在「自动互动」页面登录账号"})

        with _ts_scan_lock:
            if _ts_scan_state["running"]:
                return jsonify({"success": False, "message": "扫描任务正在进行中，请稍候"})

        body = request.json or {}
        mode = body.get("mode", "incremental")

        with _ts_scan_lock:
            _ts_scan_state.update(running=True, progress="准备中...", done=0, total=0, error=None, result=None)

        t = threading.Thread(target=_ts_scan_worker, args=(sessdata, bili_jct, mid, mode), daemon=True)
        t.start()
        return jsonify({"success": True, "message": "扫描任务已启动"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/tag-search/scan-status")
def tag_search_scan_status():
    """轮询扫描进度。"""
    with _ts_scan_lock:
        s = dict(_ts_scan_state)
    return jsonify(s)


def _ts_scan_worker(sessdata, bili_jct, mid, mode):
    """后台线程：执行标签扫描。"""
    try:
        from bilibili_api import Credential
        from bilibili_api.user import User, VideoOrder
        from bilibili_api.video import Video as BiliVideo

        credential = Credential(
            sessdata=sessdata, bili_jct=bili_jct,
            dedeuserid=str(mid), ac_time_value="",
        )
        u = User(int(mid), credential=credential)

        # 1) 拉取全部投稿
        with _ts_scan_lock:
            _ts_scan_state["progress"] = "正在获取投稿列表..."
        loop = asyncio.new_event_loop()
        try:
            async def _fetch_all_videos():
                all_vids = []
                pn = 1
                while True:
                    page = await u.get_videos(pn=pn, ps=50, order=VideoOrder.PUBDATE)
                    vlist = page.get("list", {}).get("vlist", [])
                    if not vlist:
                        break
                    all_vids.extend(vlist)
                    if len(vlist) < 50:
                        break
                    pn += 1
                return all_vids
            all_vids = loop.run_until_complete(_fetch_all_videos())
        finally:
            loop.close()

        _ts_log(f"[TAG-SEARCH] 空间投稿总数: {len(all_vids)}")

        # 空间防刷屏会把合集内除首个外的稿件从空间接口隐藏，用公开合集接口补齐，避免漏扫
        try:
            season_cred = {"sessdata": sessdata, "bili_jct": bili_jct, "dedeuserid": str(mid)}
            season_vids = _fetch_season_videos(season_cred, mid, limit=0, public=True)
            if season_vids:
                before = len(all_vids)
                all_vids = _merge_videos(all_vids, season_vids)
                _ts_log(f"[TAG-SEARCH] 合集补齐: +{len(all_vids) - before}（合计 {len(all_vids)}）")
        except Exception as e:
            _ts_log(f"[TAG-SEARCH] 合集补齐失败（不影响扫描）: {e}")

        # 2) 确定需扫描的 aid
        cache = _load_tag_cache()
        cached_videos = cache.get("videos", {})
        if mode == "full":
            to_scan = all_vids
        else:
            to_scan = [v for v in all_vids if str(v.get("aid")) not in cached_videos]

        _ts_log(f"[TAG-SEARCH] 模式={mode}, 需扫描: {len(to_scan)}")

        if not to_scan:
            from datetime import datetime
            cache["last_scan"] = datetime.now().isoformat()
            _save_tag_cache(cache)
            with _ts_scan_lock:
                _ts_scan_state.update(running=False, progress="没有新投稿需要扫描",
                                      result={"scanned": 0, "total_cached": len(cached_videos)})
            return

        with _ts_scan_lock:
            _ts_scan_state["total"] = len(to_scan)
            _ts_scan_state["progress"] = f"正在获取标签 0/{len(to_scan)}..."

        # 3) 并发获取标签（带风控重试冷却）
        _RISK_CODES = {-412, -509, -799, -352, -403}
        _RISK_KW = ["风控", "频繁", "blocked", "too many", "412", "509", "799", "352"]
        _cooldown_until = [0.0]  # mutable for closure
        _consecutive_fail = [0]
        _aborted = [False]
        _CONSECUTIVE_FAIL_LIMIT = 30

        def _is_risk(e):
            code = getattr(e, "code", None)
            if code is not None:
                try:
                    if int(code) in _RISK_CODES:
                        return True
                except (ValueError, TypeError):
                    pass
            msg = str(e).lower()
            return any(kw in msg for kw in _RISK_KW)

        loop2 = asyncio.new_event_loop()
        try:
            async def _fetch_tags_batch(vids):
                sem = asyncio.Semaphore(5)
                results = []
                done_count = 0
                saved_count = 0

                async def _wait_cooldown():
                    while _cooldown_until[0] > time.time():
                        remain = int(_cooldown_until[0] - time.time())
                        with _ts_scan_lock:
                            _ts_scan_state["progress"] = f"风控冷却中，剩余 {remain} 秒..."
                        await asyncio.sleep(1)

                async def _one(v):
                    nonlocal done_count
                    if _aborted[0]:
                        return None
                    async with sem:
                        if _aborted[0]:
                            return None
                        await _wait_cooldown()
                        if _aborted[0]:
                            return None
                        aid = v.get("aid")
                        bvid = v.get("bvid", "")
                        title = v.get("title", "")
                        pic = v.get("pic", "")
                        created = v.get("created", 0)
                        tags = []
                        ok = False
                        for attempt in range(3):
                            if _aborted[0]:
                                break
                            try:
                                vid = BiliVideo(aid=aid, credential=credential)
                                tag_list = await vid.get_tags()
                                if tag_list:
                                    tags = [t.get("tag_name", "") for t in tag_list if t.get("tag_name")]
                                ok = True
                                _consecutive_fail[0] = 0
                                break
                            except Exception as e:
                                if _is_risk(e):
                                    cd = 30 + attempt * 30
                                    _cooldown_until[0] = time.time() + cd
                                    _ts_log(f"[TAG-SEARCH] 风控检测 aid={aid}, 冷却{cd}s")
                                    await asyncio.sleep(cd)
                                    continue
                                if attempt < 2:
                                    await asyncio.sleep(2 ** attempt)
                        if not ok:
                            _consecutive_fail[0] += 1
                            if _consecutive_fail[0] >= _CONSECUTIVE_FAIL_LIMIT:
                                _aborted[0] = True
                        else:
                            _consecutive_fail[0] = 0
                        done_count += 1
                        if done_count % 10 == 0 or done_count == len(vids):
                            with _ts_scan_lock:
                                _ts_scan_state["done"] = done_count
                                _ts_scan_state["progress"] = f"正在获取标签 {done_count}/{len(vids)}..."
                        return {
                            "aid": str(aid), "bvid": bvid, "title": title,
                            "pic": pic, "created": created, "tags": tags,
                            "_ok": ok,
                        }

                # 分批执行，每批之间加间隔
                batch_size = 20
                for i in range(0, len(vids), batch_size):
                    if _aborted[0]:
                        break
                    batch = vids[i:i+batch_size]
                    batch_results = await asyncio.gather(*[_one(v) for v in batch])
                    # 增量写入缓存（请求成功的才缓存，失败的下次增量扫描会重试）
                    for item in batch_results:
                        if item and item.get("_ok"):
                            item.pop("_ok", None)
                            cached_videos[item["aid"]] = item
                            saved_count += 1
                    # 每 5 批保存一次缓存
                    if (i // batch_size) % 5 == 0 and saved_count > 0:
                        cache["videos"] = cached_videos
                        _save_tag_cache(cache)
                    results.extend([r for r in batch_results if r])
                    # 批次间隔 1-2 秒
                    if not _aborted[0] and i + batch_size < len(vids):
                        await asyncio.sleep(1.5)
                return results

            scan_results = loop2.run_until_complete(_fetch_tags_batch(to_scan))
        finally:
            loop2.close()

        # 4) 最终写入缓存
        from datetime import datetime
        for item in scan_results:
            item.pop("_ok", None)
            if item["aid"] not in cached_videos:
                cached_videos[item["aid"]] = item
        cache["videos"] = cached_videos
        cache["last_scan"] = datetime.now().isoformat()
        _save_tag_cache(cache)

        all_tags = set()
        for v in cached_videos.values():
            all_tags.update(v.get("tags", []))

        scanned_ok = sum(1 for r in scan_results if r.get("tags"))
        if _aborted[0]:
            msg = (f"扫描因风控中断：已获取 {scanned_ok}/{len(to_scan)} 个视频的标签并已缓存，"
                   f"请稍后使用「增量扫描」继续获取剩余稿件")
            with _ts_scan_lock:
                _ts_scan_state.update(
                    running=False, progress=msg,
                    done=len(scan_results), total=len(to_scan),
                    result={"scanned": scanned_ok, "total_cached": len(cached_videos),
                            "total_tags": len(all_tags), "aborted": True},
                )
        else:
            with _ts_scan_lock:
                _ts_scan_state.update(
                    running=False,
                    progress=f"扫描完成：新增 {scanned_ok} 个视频的标签",
                    done=len(to_scan), total=len(to_scan),
                    result={"scanned": scanned_ok, "total_cached": len(cached_videos),
                            "total_tags": len(all_tags)},
                )

    except Exception as e:
        _ts_log(f"[TAG-SEARCH] 扫描异常: {e}")
        # 异常时也尝试保存已有缓存
        try:
            cache["videos"] = cached_videos
            _save_tag_cache(cache)
        except Exception:
            pass
        with _ts_scan_lock:
            _ts_scan_state.update(running=False, progress=f"扫描失败: {e}", error=str(e))


@app.route("/api/tag-search/search", methods=["POST"])
def tag_search_search():
    """根据标签搜索视频。"""
    try:
        body = request.json or {}
        search_tags = [t.strip().lower() for t in (body.get("tags") or []) if t.strip()]
        mode = body.get("mode", "and")

        if not search_tags:
            return jsonify({"success": False, "message": "请输入至少一个标签"})

        cache = _load_tag_cache()
        videos = cache.get("videos", {})
        results = []

        for aid, v in videos.items():
            v_tags_lower = [t.lower() for t in v.get("tags", [])]
            if mode == "and":
                match = all(st in v_tags_lower for st in search_tags)
            else:
                match = any(st in v_tags_lower for st in search_tags)
            if match:
                results.append(v)

        results.sort(key=lambda x: x.get("created", 0), reverse=True)

        return jsonify({"success": True, "count": len(results), "videos": results})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/tag-search/all-tags")
def tag_search_all_tags():
    """返回缓存中所有标签名及其出现次数，用于联想补全。"""
    try:
        cache = _load_tag_cache()
        tag_counts = {}
        for v in cache.get("videos", {}).values():
            for t in v.get("tags", []):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
        return jsonify({"success": True, "tags": [{"name": n, "count": c} for n, c in sorted_tags]})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/tag-search/refresh-one", methods=["POST"])
def tag_search_refresh_one():
    """刷新单个视频的标签缓存。"""
    try:
        sessdata, bili_jct, mid = _coll_cred()
        if not sessdata or not mid:
            return jsonify({"success": False, "message": "未登录"})

        body = request.json or {}
        aid = body.get("aid")
        if not aid:
            return jsonify({"success": False, "message": "缺少 aid"})
        from bilibili_api import Credential
        from bilibili_api.video import Video as BiliVideo

        credential = Credential(
            sessdata=sessdata, bili_jct=bili_jct,
            dedeuserid=str(mid), ac_time_value="",
        )

        loop = asyncio.new_event_loop()
        try:
            async def _do():
                vid = BiliVideo(aid=int(aid), credential=credential)
                tag_list = await vid.get_tags()
                return [t.get("tag_name", "") for t in (tag_list or []) if t.get("tag_name")]
            tags = loop.run_until_complete(_do())
        finally:
            loop.close()

        cache = _load_tag_cache()
        aid_str = str(aid)
        if aid_str in cache.get("videos", {}):
            cache["videos"][aid_str]["tags"] = tags
            _save_tag_cache(cache)

        return jsonify({"success": True, "tags": tags})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route("/api/tag-search/stats", methods=["POST"])
def tag_search_stats():
    """批量获取视频统计数据。"""
    try:
        sessdata, bili_jct, mid = _coll_cred()
        if not sessdata or not mid:
            return jsonify({"success": False, "message": "未登录"})

        body = request.json or {}
        aids = body.get("aids", [])
        if not aids:
            return jsonify({"success": False, "message": "未选择视频"})

        aids = aids[:100]
        from bilibili_api import Credential
        from bilibili_api.video import Video as BiliVideo

        credential = Credential(
            sessdata=sessdata, bili_jct=bili_jct,
            dedeuserid=str(mid), ac_time_value="",
        )

        loop = asyncio.new_event_loop()
        try:
            async def _fetch_stats():
                import httpx

                sem = asyncio.Semaphore(10)
                # 创作中心接口有频率限制（-702），并发压到 3 并带退避重试
                tc_sem = asyncio.Semaphore(3)
                tc_cookies = _coll_cookies(sessdata, bili_jct, mid)

                async def _tc_zone(client, aid):
                    """取创作中心口径的分区名（human_type2），取不到返回 None。"""
                    key = str(aid)
                    cached = _TC_ZONE_CACHE.get(key)
                    if cached and time.time() - cached[1] < _TC_ZONE_TTL:
                        return cached[0]
                    async with tc_sem:
                        for attempt in range(3):
                            try:
                                resp = await client.get(
                                    "https://member.bilibili.com/x/vupre/web/archive/view",
                                    params={"aid": int(aid)},
                                    headers=COLL_HEADERS, cookies=tc_cookies, timeout=15,
                                )
                                data = resp.json()
                                if data.get("code") == -702:
                                    await asyncio.sleep(1.5 * (attempt + 1))
                                    continue
                                if data.get("code") == 0:
                                    archive = (data.get("data") or {}).get("archive") or {}
                                    name = (archive.get("human_type2") or {}).get("name") or None
                                    _TC_ZONE_CACHE[key] = (name, time.time())
                                    return name
                                break
                            except Exception:
                                await asyncio.sleep(1.0 * (attempt + 1))
                    return None

                async def _one(aid, client):
                    async with sem:
                        try:
                            vid = BiliVideo(aid=int(aid), credential=credential)
                            info = await vid.get_info()
                            stat = info.get("stat", {})
                            _tid = info.get("tid", 0)
                            _tid_v2 = info.get("tid_v2", 0)
                            _tname = info.get("tname", "") or info.get("tname_v2", "") or _V2_TID_MAP.get(_tid_v2, "") or _TID_MAP.get(_tid, str(_tid) if _tid else "")
                            # 与创作中心「分区」栏口径对齐；非本人稿件取不到时回退到上面推算的分区
                            _tc_name = await _tc_zone(client, aid)
                            if _tc_name:
                                _tname = _tc_name
                            return {
                                "aid": str(aid),
                                "bvid": info.get("bvid", ""),
                                "title": info.get("title", ""),
                                "tname": _tname,
                                "view": stat.get("view", 0),
                                "like": stat.get("like", 0),
                                "coin": stat.get("coin", 0),
                                "favorite": stat.get("favorite", 0),
                                "share": stat.get("share", 0),
                                "danmaku": stat.get("danmaku", 0),
                                "reply": stat.get("reply", 0),
                            }
                        except Exception as e:
                            return {"aid": str(aid), "error": str(e)}

                async with httpx.AsyncClient() as client:
                    tasks = [_one(a, client) for a in aids]
                    results = await asyncio.gather(*tasks)
                return results

            stats_list = loop.run_until_complete(_fetch_stats())
        finally:
            loop.close()

        # 汇总
        total = {"view": 0, "like": 0, "coin": 0, "favorite": 0, "share": 0, "danmaku": 0, "reply": 0}
        for s in stats_list:
            if "error" not in s:
                for k in total:
                    total[k] += s.get(k, 0)

        return jsonify({
            "success": True,
            "count": len(stats_list),
            "total": total,
            "videos": stats_list,
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# =========================================================================
#  九、前端入口
# =========================================================================

# =========================================================================
#  进程关闭清理
# =========================================================================
# 在 pyappify / 手动 kill / Ctrl+C 等场景下，确保：
#   1. 触发所有 schedule 任务的 stop_event（auto schedule / booster schedule / redpocket / livehelper）
#   2. 触发所有 active task 的 stop_event（auto_stop_events / history_stop_events / booster_tasks）
#   3. 等后台线程退出（带超时，避免 hang）
#
# Windows 上 SIGTERM 不可靠（部分 Tauri 启动器会直接 TerminateProcess），
# 所以同时注册 atexit + signal.SIGINT + signal.SIGBREAK（Windows Ctrl+Break）。
# 即使信号都失效，os._exit 之前我们已做完最关键的 stop + kill 子进程。

_shutdown_done = False
_shutdown_lock = threading.Lock()


def _shutdown(label: str = "shutdown"):
    """统一关闭逻辑，幂等"""
    global _shutdown_done
    with _shutdown_lock:
        if _shutdown_done:
            return
        _shutdown_done = True
    print(f"[{label}] 开始清理后台任务与子进程...", flush=True)

    # 1) 触发所有 schedule 级别的 stop_event
    try:
        if _auto_schedule.get("stop_event"):
            _auto_schedule["stop_event"].set()
    except Exception as e:
        print(f"[{label}] set auto schedule stop_event failed: {e}", flush=True)
    try:
        if _booster_schedule.get("stop_event"):
            _booster_schedule["stop_event"].set()
    except Exception as e:
        print(f"[{label}] set booster schedule stop_event failed: {e}", flush=True)
    try:
        if _wallet_state.get("stop_event"):
            _wallet_state["stop_event"].set()
    except Exception as e:
        print(f"[{label}] set wallet stop_event failed: {e}", flush=True)

    # 2) 触发所有 active task 的 stop_event
    try:
        for ev in list(auto_stop_events.values()):
            ev.set()
    except Exception as e:
        print(f"[{label}] set auto stop_events failed: {e}", flush=True)
    try:
        for ev in list(history_stop_events.values()):
            ev.set()
    except Exception as e:
        print(f"[{label}] set history stop_events failed: {e}", flush=True)
    try:
        with booster_lock:
            for t in booster_tasks.values():
                ev = t.get("stop_event")
                if ev:
                    ev.set()
    except Exception as e:
        print(f"[{label}] set booster stop_events failed: {e}", flush=True)

    # redpocket / livehelper 通过自己的 stop 端点或共享 stop_event 处理
    # 这里直接尝试调用对应模块的 stop 函数（如果存在）
    for mod_name in ("bili_redpocket", "bili_livehelper"):
        try:
            mod = sys.modules.get(mod_name)
            if mod and hasattr(mod, "stop_all"):
                mod.stop_all()
        except Exception as e:
            print(f"[{label}] {mod_name}.stop_all failed: {e}", flush=True)

    # 3) 等后台线程退出（最多 5 秒）
    def _join_all(deadline: float):
        for t in threading.enumerate():
            if t is threading.current_thread():
                continue
            if not t.is_alive():
                continue
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                break
            t.join(timeout=remaining)

    _join_all(time.time() + 5.0)

    print(f"[{label}] 清理完成", flush=True)


def _port_in_use(port: int) -> bool:
    """用「能否连上」判断端口是否有监听者（bind 测试在 Windows 上受 TIME_WAIT/SO_REUSEADDR 干扰，不可靠）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _kill_stale_app_instances(port: int):
    """启动前清理本安装目录残留的旧实例进程，保证同一安装只有一个服务在跑。

    背景：旧版本 app.run(debug=True) 启用 werkzeug reloader，会拆成「reloader 父进程 +
    真实服务子进程」两个 python 进程；pyappify 关窗只杀它启动的那个，另一个变成孤儿——
    继续跑所有定时任务并占住端口，且收不到任何关闭清理信号。更新到新版后，用户机器上
    可能仍留着这样的孤儿，所以新实例必须在绑定端口前清场：
      1) 按「cmdline 指向本目录 app.py 的 python 进程」匹配并杀掉（跳过自己），
         相对路径按目标进程自己的 cwd 解析，避免误杀其它目录的同名脚本；
      2) 兜底：端口仍被 python 进程占用时按端口补杀（5678 为本应用专用端口）；
      3) 端口被其它程序占用或清理后仍不可用时，报错退出，绝不与旧实例共存
         （否则用户打开的界面可能连到旧进程，看到的状态是错的）。
    """
    my_pid = os.getpid()
    target_script = str((ROOT / "app.py").resolve()).lower()
    root_dir = str(ROOT).lower()

    # 自己的祖先链（python 垫片/启动器等）绝不能杀：它们的 cmdline 可能同样以 app.py 结尾
    # （如 WindowsApps python 别名、pyappify 包装层），误杀会导致自己随 Job 对象一起被终止
    my_ancestors = set()
    try:
        import psutil
        _proc = psutil.Process(my_pid)
        while True:
            _proc = _proc.parent()
            if _proc is None:
                break
            my_ancestors.add(_proc.pid)
    except Exception:
        pass

    victims = []
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
            try:
                info = proc.info
                if info["pid"] == my_pid or info["pid"] in my_ancestors:
                    continue
                if "python" not in (info["name"] or "").lower():
                    continue
                args = [str(a) for a in (info["cmdline"] or [])]
                if not any(a.lower().endswith("app.py") for a in args):
                    continue
                cwd = info.get("cwd") or ""
                matched = False
                for a in args:
                    if not a.lower().endswith("app.py"):
                        continue
                    p = Path(a)
                    if not p.is_absolute() and cwd:
                        p = Path(cwd) / a
                    try:
                        if str(p.resolve()).lower() == target_script:
                            matched = True
                            break
                    except OSError:
                        continue
                if not matched and cwd:
                    # cmdline 是相对路径且读不到足够信息时，cwd 在本目录即视为本应用实例
                    try:
                        matched = str(Path(cwd).resolve()).lower() == root_dir
                    except OSError:
                        matched = False
                if matched:
                    victims.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        for proc in victims:
            try:
                proc.kill()
            except Exception:
                pass
        if victims:
            psutil.wait_procs(victims, timeout=5)
            print(f"[startup] 已清理 {len(victims)} 个残留旧实例进程", flush=True)
    except ImportError:
        print("[startup] psutil 未安装，跳过残留实例清理", flush=True)

    # 端口兜底校验
    if _port_in_use(port):
        try:
            import psutil
            holder_pids = [c.pid for c in psutil.net_connections(kind="inet")
                           if c.status == psutil.CONN_LISTEN and c.laddr
                           and c.laddr.port == port and c.pid and c.pid != my_pid]
            for pid in holder_pids:
                try:
                    proc = psutil.Process(pid)
                    pname = proc.name() or ""
                    if "python" in pname.lower():
                        proc.kill()
                        print(f"[startup] 端口 {port} 被 python 进程 {pid} 占用，已补杀", flush=True)
                    else:
                        print(f"[startup][ERROR] 端口 {port} 被其它程序占用（{pname}，PID {pid}），请关闭后重试", flush=True)
                        sys.exit(1)
                except psutil.NoSuchProcess:
                    continue
        except ImportError:
            pass
        time.sleep(1.5)
        if _port_in_use(port):
            print(f"[startup][ERROR] 端口 {port} 仍被占用，无法启动。请重启电脑或手动结束相关 python 进程", flush=True)
            sys.exit(1)


def _signal_handler(signum, frame):
    print(f"[signal] 收到信号 {signum}", flush=True)
    _shutdown(label=f"signal-{signum}")
    # 给清理留一点时间，再 os._exit 强退（避免被 hang 在 flask shutdown 流程上）
    try:
        time.sleep(0.3)
    except Exception:
        pass
    os._exit(0)


# 注册关闭钩子（idempotent）
atexit.register(_shutdown, label="atexit")
try:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
except (ValueError, OSError, AttributeError):
    # 某些环境（子线程、被打包成 exe）不允许注册信号，忽略
    pass
# Windows: Ctrl+Break 也常见于 pyappify / 控制台
if hasattr(signal, "SIGBREAK"):
    try:
        signal.signal(signal.SIGBREAK, _signal_handler)
    except (ValueError, OSError, AttributeError):
        pass


# =========================================================================
#  粉丝灯牌管理模块 (bili-medal)
# =========================================================================

def _medal_cookies():
    """从 auto 主账号凭证构建直播接口所需 cookies。"""
    cred = _read_auto_cred()
    if not cred:
        return None, None
    cookies = {
        "SESSDATA": cred.get("sessdata", ""),
        "bili_jct": cred.get("bili_jct", ""),
        "DedeUserID": str(cred.get("dedeuserid", "")),
    }
    return cookies, cred


@app.route("/api/medal/list")
def medal_list():
    """拉取当前主账号的全部粉丝灯牌 + 锁定列表。"""
    from medal_helper import fetch_medals, read_locks
    cookies, cred = _medal_cookies()
    if not cookies:
        return jsonify({"success": False, "message": "未登录，请先在「自动互动」模块扫码登录", "medals": [], "locks": []})
    uid = str(cred.get("dedeuserid", ""))
    loop = asyncio.new_event_loop()
    try:
        medals = loop.run_until_complete(fetch_medals(cookies))
    except Exception as e:
        return jsonify({"success": False, "message": str(e), "medals": [], "locks": read_locks(uid)})
    finally:
        loop.close()
    return jsonify({
        "success": True,
        "uid": uid,
        "uname": cred.get("name", "") or _fetch_bili_uname(cred.get("sessdata", ""), uid),
        "medals": medals,
        "locks": read_locks(uid),
    })


@app.route("/api/medal/remove", methods=["POST"])
def medal_remove():
    """批量移除灯牌（锁定项由前端排除，后端二次校验）。"""
    from medal_helper import batch_remove, read_locks
    data = request.json or {}
    ids = [str(x) for x in (data.get("target_ids") or [])]
    if not ids:
        return jsonify({"success": False, "message": "未选择任何灯牌"})
    cookies, cred = _medal_cookies()
    if not cookies:
        return jsonify({"success": False, "message": "未登录，请先在「自动互动」模块扫码登录"})
    uid = str(cred.get("dedeuserid", ""))
    locked = set(read_locks(uid))
    ids = [x for x in ids if x not in locked]
    if not ids:
        return jsonify({"success": False, "message": "所选灯牌均已锁定"})
    loop = asyncio.new_event_loop()
    try:
        out = loop.run_until_complete(batch_remove(cookies, cred.get("bili_jct", ""), ids))
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    finally:
        loop.close()
    return jsonify({"success": True, **out})


@app.route("/api/medal/lock", methods=["POST"])
def medal_lock():
    """设置单个灯牌锁定状态。"""
    from medal_helper import set_lock
    data = request.json or {}
    cred = _read_auto_cred()
    if not cred:
        return jsonify({"success": False, "message": "未登录"})
    uid = str(cred.get("dedeuserid", ""))
    locks = set_lock(uid, str(data.get("target_id", "")), bool(data.get("locked")))
    return jsonify({"success": True, "locks": locks})


# =========================================================================
#  三-H、贝壳钱包监控 — 轮询账单明细，有新增即 Server酱3 推送
# =========================================================================
WALLET_CONFIG_FILE = CONFIG_DIR / "wallet_monitor.yaml"

# 贝壳账单列表接口（web 端账单页 pay-v2/shell/index 使用，纯 Cookie 认证）
WALLET_API = "https://pay.bilibili.com/payplatform/cashier/bk/trans/list"
WALLET_SDK_VERSION = "1.5.6"
WALLET_PAGE_SIZE = 30
WALLET_MAX_PAGES = 6          # 单次最多翻 6 页，避免异常情况下无限拉取
WALLET_BASELINE_LIMIT = 800   # 基线条目上限，超出丢弃最早记录（远超回看窗口内的条数）
WALLET_DEFAULT_INTERVAL = 10  # 分钟
WALLET_DEFAULT_LOOKBACK = 60  # 天

WALLET_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://pay.bilibili.com",
    "referer": "https://pay.bilibili.com/pay-v2/shell/index",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

_wallet_state = {
    "running": False,
    "thread": None,
    "stop_event": threading.Event(),
    "last_run": None,
    "next_run": None,
    "run_count": 0,
    "last_error": "",
}

# 防止定时循环与手动 run-once 并发执行同一明细导致重复推送
_wallet_check_lock = threading.Lock()

# 状态码 → 文案（接口本身也返回 statusDesc，缺省时兜底）
_WALLET_STATUS_TEXT = {0: "审核中", 1: "到账成功", 2: "已失效", 6: "提现成功"}


def _load_wallet_config():
    """读取贝壳监控配置（含基线 seen）。"""
    import yaml
    if WALLET_CONFIG_FILE.exists():
        try:
            with open(WALLET_CONFIG_FILE, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


def _save_wallet_config(cfg):
    """写入贝壳监控配置（含基线 seen，落盘以保证重启后不重复推送）。"""
    import yaml
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(WALLET_CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


def _wallet_build_cookies(cred: dict) -> dict:
    """由主账号凭证构造账单接口所需的 Cookie。"""
    cookies = {"SESSDATA": cred.get("sessdata", "")}
    if cred.get("bili_jct"):
        cookies["bili_jct"] = cred["bili_jct"]
    uid = str(cred.get("dedeuserid") or cred.get("login_uid") or "")
    if uid:
        cookies["DedeUserID"] = uid
    if cred.get("buvid3"):
        cookies["buvid3"] = cred["buvid3"]
    return cookies


def _wallet_fetch_page(cookies: dict, page: int, begin_time: str, end_time: str) -> dict:
    """拉取单页账单。返回接口 data 字段。"""
    import httpx
    payload = {
        "currentPage": page,
        "pageSize": WALLET_PAGE_SIZE,
        "beginTime": begin_time,
        "endTime": end_time,
        "traceId": int(time.time() * 1000),
        "sdkVersion": WALLET_SDK_VERSION,
    }
    resp = httpx.post(WALLET_API, json=payload, cookies=cookies, headers=WALLET_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(str(data.get("msg") or data.get("message") or "接口返回失败"))
    return data.get("data") or {}


def _wallet_fetch_items(cookies: dict, lookback_days: int) -> list:
    """拉取回看窗口内的全部账单明细。"""
    now = datetime.now()
    end_time = now.strftime("%Y-%m-%d %H:%M:%S")
    begin_time = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    items = []
    page = 1
    while page <= WALLET_MAX_PAGES:
        data = _wallet_fetch_page(cookies, page, begin_time, end_time)
        items.extend(data.get("result") or [])
        try:
            total_page = int((data.get("page") or {}).get("totalPage") or 1)
        except (TypeError, ValueError):
            total_page = 1
        if page >= total_page:
            break
        page += 1
        time.sleep(0.5)  # 翻页间隔，避免触发风控
    return items


def _wallet_push(send_key: str, title: str, desp: str):
    """Server酱推送。返回 (是否成功, 错误信息)。"""
    import httpx
    key = (send_key or "").strip()
    if not key:
        return False, "未配置 SendKey"
    if key.startswith("sctp"):
        # Server酱3：SendKey 形如 sctp1234tXXXX，uid 为 sctp 与 t 之间的数字
        m = re.match(r"^sctp(\d+)t", key)
        if not m:
            return False, "SendKey 格式不正确（应形如 sctp1234t...）"
        url = "https://%s.push.ft07.com/send/%s.send" % (m.group(1), key)
    else:
        # Server酱 Turbo（微信推送）
        url = "https://sctapi.ftqq.com/%s.send" % key
    try:
        resp = httpx.post(url, data={"title": title, "desp": desp}, timeout=15)
        data = resp.json()
    except Exception as e:
        return False, "推送请求失败: %s" % e
    try:
        code = int(data.get("code", -1))
    except (TypeError, ValueError):
        code = -1
    if code == 0:
        return True, ""
    return False, str(data.get("message") or data.get("msg") or data)


def _wallet_item_status(it: dict) -> str:
    """取明细的状态文案。"""
    desc = str(it.get("statusDesc") or "").strip()
    if desc:
        return desc
    try:
        return _WALLET_STATUS_TEXT.get(int(it.get("status")), str(it.get("status")))
    except (TypeError, ValueError):
        return "未知状态"


def _wallet_item_amount(it: dict) -> str:
    """取明细的金额展示（带正负号）。"""
    try:
        amt = float(it.get("brokerage") or 0)
    except (TypeError, ValueError):
        amt = 0.0
    sign = "-" if it.get("tallyDirect") == "DECREASE" else "+"
    return "%s%.2f" % (sign, amt)


def _wallet_notify(send_key: str, it: dict, log_target: str) -> bool:
    """推送单条明细。"""
    label = str(it.get("label") or "")
    title = str(it.get("title") or "")
    status_text = _wallet_item_status(it)
    amount = _wallet_item_amount(it)
    ctime = str(it.get("ctime") or "")
    order_no = str(it.get("orderNo") or "")
    lines = [
        "**分类**：%s" % (label or "-"),
        "**明细**：%s" % (title or "-"),
        "**金额**：%s 元" % amount,
        "**状态**：%s" % status_text,
        "**时间**：%s" % (ctime or "-"),
        "**订单号**：%s" % (order_no or "-"),
    ]
    recv = it.get("receivedAmount")
    if recv is not None:
        try:
            if abs(float(recv) - abs(float(it.get("brokerage") or 0))) > 0.001:
                lines.insert(3, "**实际到账**：%.2f 元" % float(recv))
        except (TypeError, ValueError):
            pass
    ok, err = _wallet_push(send_key, "【贝壳】%s 元 · %s" % (amount, status_text), "\n\n".join(lines))
    if ok:
        _append_log(log_target, "已推送: %s 元 %s（%s）" % (amount, status_text, title))
    else:
        _append_log(log_target, "[推送失败] %s：%s" % (title, err))
    return ok


def _wallet_check_once(stop_event, log_target: str = "wallet-monitor") -> bool:
    """执行一次检查（互斥：定时与手动并发时后者直接跳过，避免同一明细重复推送）。"""
    if not _wallet_check_lock.acquire(blocking=False):
        _append_log(log_target, "[SYSTEM] 上一次检查尚未结束，跳过本次")
        return False
    try:
        return _wallet_check_inner(stop_event, log_target)
    finally:
        _wallet_check_lock.release()


def _wallet_check_inner(stop_event, log_target: str) -> bool:
    """执行一次检查：拉取明细 → 与基线比对 → 推送新增。返回是否成功拉取。"""
    cred = _read_auto_cred()
    if not cred.get("sessdata"):
        _append_log(log_target, "[SYSTEM] 未找到主账号凭证，请先扫码登录")
        return False

    cfg = _load_wallet_config()
    send_key = str(cfg.get("send_key") or "").strip()
    if not send_key:
        _append_log(log_target, "[SYSTEM] 未配置 Server酱 SendKey，无法推送")
        return False

    try:
        lookback = int(cfg.get("lookback_days") or WALLET_DEFAULT_LOOKBACK)
    except (TypeError, ValueError):
        lookback = WALLET_DEFAULT_LOOKBACK

    seen = dict(cfg.get("seen") or {})
    first_run = not seen

    try:
        items = _wallet_fetch_items(_wallet_build_cookies(cred), lookback)
    except Exception as e:
        _wallet_state["last_error"] = str(e)
        _append_log(log_target, "[SYSTEM] 获取账单失败: %s" % e)
        return False

    _wallet_state["last_error"] = ""
    _append_log(log_target, "共获取 %d 条明细（回看 %d 天）%s"
                % (len(items), lookback, "；首次运行仅建立基线，不推送" if first_run else ""))

    # 判重键：同一 orderNo 的状态会变化（审核中 → 到账成功），故用 orderNo + status 组合
    fresh = []
    for it in items:
        key = "%s|%s" % (it.get("orderNo", ""), it.get("status", ""))
        if key in seen:
            continue
        fresh.append((key, it))

    if first_run:
        now_ts = int(time.time())
        for key, _it in fresh:
            seen[key] = now_ts
        _append_log(log_target, "已建立基线 %d 条" % len(seen))
    else:
        # 按时间正序推送，消息读起来更自然
        for key, it in reversed(fresh):
            if stop_event.is_set():
                _append_log(log_target, "[SYSTEM] 收到停止信号，中断推送")
                break
            if _wallet_notify(send_key, it, log_target):
                seen[key] = int(time.time())
            # 推送失败则不记入基线，下一轮自动重试
        if not fresh:
            _append_log(log_target, "无新增明细")

    # 裁剪基线，避免配置文件无限增长
    if len(seen) > WALLET_BASELINE_LIMIT:
        for k in list(seen.keys())[:len(seen) - WALLET_BASELINE_LIMIT]:
            seen.pop(k, None)

    cfg["seen"] = seen
    _save_wallet_config(cfg)
    return True


def _wallet_monitor_loop():
    """贝壳钱包监控主循环。"""
    while not _wallet_state["stop_event"].is_set():
        cfg = _load_wallet_config()
        try:
            interval = int(cfg.get("interval_minutes") or WALLET_DEFAULT_INTERVAL) * 60
        except (TypeError, ValueError):
            interval = WALLET_DEFAULT_INTERVAL * 60
        if interval < 60:
            interval = 60
        _append_log("wallet-monitor", "=== 第%d次检查 ===" % (_wallet_state["run_count"] + 1))
        _wallet_check_once(_wallet_state["stop_event"])
        _wallet_state["run_count"] += 1
        _wallet_state["last_run"] = time.time()
        _wallet_state["next_run"] = time.time() + interval
        if _wallet_state["stop_event"].is_set():
            break
        _append_log("wallet-monitor", "=== 等待 %d 分钟后执行下一次 ===" % (interval // 60))
        # 分段等待，以便能快速响应停止
        for _ in range(interval):
            if _wallet_state["stop_event"].is_set():
                break
            time.sleep(1)
    _wallet_state["running"] = False
    _append_log("wallet-monitor", "贝壳钱包监控已停止")


@app.route("/api/wallet/config", methods=["GET"])
def wallet_config_get():
    cfg = _load_wallet_config()
    cred = _read_auto_cred()
    return jsonify({
        "send_key": cfg.get("send_key", ""),
        "interval_minutes": cfg.get("interval_minutes", WALLET_DEFAULT_INTERVAL),
        "lookback_days": cfg.get("lookback_days", WALLET_DEFAULT_LOOKBACK),
        "baseline_count": len(cfg.get("seen") or {}),
        "has_login": bool(cred.get("sessdata")),
        "login_uid": str(cred.get("dedeuserid") or cred.get("login_uid") or ""),
    })


@app.route("/api/wallet/config", methods=["POST"])
def wallet_config_save():
    data = request.json or {}
    send_key = str(data.get("send_key", "")).strip()
    try:
        interval = int(data.get("interval_minutes", WALLET_DEFAULT_INTERVAL))
        lookback = int(data.get("lookback_days", WALLET_DEFAULT_LOOKBACK))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "配置项必须为整数"}), 400
    if interval < 1 or lookback < 1:
        return jsonify({"success": False, "message": "配置项必须为正整数"}), 400
    if send_key and not (send_key.startswith("sctp") or send_key.startswith("SCT")):
        return jsonify({"success": False, "message": "SendKey 应以 sctp（Server酱3）或 SCT（Server酱Turbo）开头"}), 400
    cfg = _load_wallet_config()
    cfg["send_key"] = send_key
    cfg["interval_minutes"] = interval
    cfg["lookback_days"] = lookback
    _save_wallet_config(cfg)
    return jsonify({"success": True})


@app.route("/api/wallet/test-push", methods=["POST"])
def wallet_test_push():
    """发送一条测试推送，验证 SendKey 是否可用。"""
    data = request.json or {}
    send_key = str(data.get("send_key", "")).strip()
    if not send_key:
        return jsonify({"success": False, "message": "请先填写 SendKey"})
    ok, err = _wallet_push(send_key, "【贝壳】推送测试", "收到这条消息说明 Server酱 配置正确。")
    if ok:
        return jsonify({"success": True})
    return jsonify({"success": False, "message": err})


@app.route("/api/wallet/start", methods=["POST"])
def wallet_start():
    if _wallet_state["running"]:
        return jsonify({"success": False, "message": "贝壳钱包监控已在运行"})

    _wallet_state["stop_event"] = threading.Event()
    _wallet_state["running"] = True
    _wallet_state["run_count"] = 0
    _wallet_state["last_run"] = None
    _wallet_state["next_run"] = None
    _wallet_state["last_error"] = ""
    with log_lock:
        log_buffers["wallet-monitor"] = []
    _append_log("wallet-monitor", "贝壳钱包监控已启动")

    t = threading.Thread(target=_wallet_monitor_loop, daemon=True)
    _wallet_state["thread"] = t
    t.start()
    return jsonify({"success": True})


@app.route("/api/wallet/stop", methods=["POST"])
def wallet_stop():
    if not _wallet_state["running"]:
        return jsonify({"success": True, "message": "未在运行"})
    _wallet_state["stop_event"].set()
    return jsonify({"success": True})


@app.route("/api/wallet/run-once", methods=["POST"])
def wallet_run_once():
    """手动触发一次检查（不影响定时循环）。"""
    if _wallet_state["running"]:
        stop_event = _wallet_state["stop_event"]
    else:
        _wallet_state["stop_event"] = threading.Event()
        _wallet_state["running"] = "once"
        _wallet_state["last_run"] = None
        with log_lock:
            log_buffers["wallet-monitor"] = []
        _append_log("wallet-monitor", "=== 手动检查一次 ===")
        stop_event = _wallet_state["stop_event"]

    def _once():
        _wallet_check_once(stop_event)
        _wallet_state["last_run"] = time.time()
        if _wallet_state["running"] == "once":
            _wallet_state["running"] = False
            _append_log("wallet-monitor", "=== 手动检查已结束 ===")

    threading.Thread(target=_once, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/wallet/status")
def wallet_status():
    cfg = _load_wallet_config()
    return jsonify({
        "running": _wallet_state["running"],
        "interval_minutes": cfg.get("interval_minutes", WALLET_DEFAULT_INTERVAL),
        "lookback_days": cfg.get("lookback_days", WALLET_DEFAULT_LOOKBACK),
        "send_key_set": bool(str(cfg.get("send_key") or "").strip()),
        "baseline_count": len(cfg.get("seen") or {}),
        "last_run": _wallet_state["last_run"],
        "next_run": _wallet_state["next_run"],
        "run_count": _wallet_state["run_count"],
        "last_error": _wallet_state["last_error"],
        "log": _get_log("wallet-monitor"),
    })


@app.route("/")
def index():
    return render_template("index.html")


APP_PORT = 5678

if __name__ == "__main__":
    import webbrowser
    # 恢复上次保存的并发上限（配置项持久化在 booster_config.yaml）
    try:
        _booster_gate.set_limit(int(_load_booster_config().get("schedule", {}).get(
            "max_concurrent_tasks", DEFAULT_MAX_CONCURRENT_BOOST_TASKS)))
    except Exception:
        pass
    # 绑定端口前先清掉旧版本残留的实例进程（孤儿/端口占用），否则新实例起不来或连到旧进程
    _kill_stale_app_instances(APP_PORT)
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        webbrowser.open(f"http://localhost:{APP_PORT}")
    # 禁止 debug/reloader：debug=True 会拆成父+子两个 python 进程，pyappify 关窗只杀父进程，
    # 真实服务子进程会变成孤儿，继续跑定时任务且收不到任何关闭清理（这就是「关了还在刷」的根源）
    app.run(debug=False, use_reloader=False, host="0.0.0.0", port=APP_PORT)
