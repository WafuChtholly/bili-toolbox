"""
B站工具箱 — 统一 WebUI
整合五大场景：自动互动 / 播放量提升(proxy) / 播放量提升(Playwright) / 直播间红包助手 / 话题助手
"""
import asyncio
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
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# 路径 & 导入设置
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
# 注意：bili-booster 目录不能直接加到 sys.path，否则其 app.py 会与本文件冲突
# booster 模块在需要时通过 importlib 动态加载
sys.path.insert(0, str(ROOT / "bili-auto"))
sys.path.insert(0, str(ROOT / "bili-redpocket"))

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# =========================================================================
#  一、通用工具
# =========================================================================

# 简单的内存日志存储，前端轮询拉取
log_buffers: dict[str, list[str]] = {}
log_lock = threading.Lock()


def _append_log(task_id: str, line: str):
    with log_lock:
        log_buffers.setdefault(task_id, []).append(line)


def _get_log(task_id: str) -> str:
    with log_lock:
        return "\n".join(log_buffers.get(task_id, []))


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
    """用 Playwright 逐个播放给定的 BV 列表。"""
    if not bvids:
        _append_log(task_id, "[SYSTEM] 无 BV 可播放，跳过")
        return
    player_dir = str(ROOT / "bili-player")
    if player_dir not in sys.path:
        sys.path.insert(0, player_dir)
    from player import play_video

    # 从 auto 凭证构建 Playwright cookie（优先使用 auto 模块的登录凭证）
    auto_cookies = None
    cred_data = _read_auto_cred()
    if cred_data.get("sessdata"):
        auto_cookies = [
            {"name": "SESSDATA", "value": cred_data["sessdata"], "domain": ".bilibili.com", "path": "/"},
            {"name": "bili_jct", "value": cred_data.get("bili_jct", ""), "domain": ".bilibili.com", "path": "/"},
        ]
        buvid3 = cred_data.get("buvid3", "")
        login_uid = str(cred_data.get("dedeuserid", "") or cred_data.get("login_uid", ""))
        if buvid3:
            auto_cookies.append({"name": "buvid3", "value": buvid3, "domain": ".bilibili.com", "path": "/"})
        if login_uid:
            auto_cookies.append({"name": "DedeUserID", "value": login_uid, "domain": ".bilibili.com", "path": "/"})
        _append_log(task_id, "[SYSTEM] 播放使用 auto 模块凭证")

    # play_video 现在是异步函数，需要使用 asyncio.run()
    import asyncio

    async def _play_all():
        for i, bvid in enumerate(bvids):
            if stop_event.is_set():
                _append_log(task_id, "[SYSTEM] 收到停止信号，停止播放")
                break
            _append_log(task_id, f"[SYSTEM] 播放 ({i+1}/{len(bvids)}): {bvid}")
            try:
                await play_video(bvid, stop_event=stop_event, log_fn=lambda msg: _append_log(task_id, msg), cookies=auto_cookies)
            except Exception as e:
                _append_log(task_id, f"[SYSTEM] 播放 {bvid} 失败: {e}")
            # 视频间短暂等待
            if not stop_event.is_set() and i < len(bvids) - 1:
                import random as _r
                wait = _r.randint(3, 8)
                _append_log(task_id, f"[SYSTEM] 等待 {wait} 秒后播放下一个...")
                await asyncio.sleep(wait)

    asyncio.run(_play_all())
    _append_log(task_id, f"[SYSTEM] 播放完成")


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


@app.route("/api/auto/login/config")
def auto_login_config():
    """检查 auto 模块登录状态"""
    try:
        data = _read_auto_cred()
        if data.get("sessdata"):
            uid = data.get("mid") or data.get("login_uid") or data.get("dedeuserid") or data.get("uid", "")
            return jsonify({"login_uid": str(uid) if uid else ""})
    except Exception:
        pass
    return jsonify({"login_uid": ""})


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
    # 加载现有配置，配置文件优先，缺失字段才用前端数据填充
    cfg = _load_auto_config()
    for k, v in data.items():
        cfg.setdefault(k, v)
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
        from bilibili_api.utils.network import Credential
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
        from bilibili_api.utils.network import Credential
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

booster_tasks = {}
booster_lock = threading.Lock()

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


def _run_booster_task(task_id: str, bv_list: list[str], target: int, stop_event: threading.Event = None):
    with booster_lock:
        booster_tasks[task_id]["status"] = "running"

    # 通过 log_fn 回调直接写入 task buffer，不再重定向 sys.stdout
    def log_fn(msg: str):
        _append_log(task_id, f"[BOOSTER] {msg}")

    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("booster", str(ROOT / "bili-booster" / "booster.py"))
        booster = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(booster)

        bv_input = ",".join(bv_list)
        booster.main(bv_input, str(target), stop_event=stop_event, log_fn=log_fn)
        with booster_lock:
            booster_tasks[task_id]["status"] = "completed"
    except Exception as e:
        _append_log(task_id, f"[ERROR] {e}")
        with booster_lock:
            booster_tasks[task_id]["status"] = "error"
    finally:
        with booster_lock:
            booster_tasks[task_id]["end"] = time.time()


@app.route("/api/booster/run", methods=["POST"])
def booster_run():
    data = request.json
    bv_str = data.get("bv", "")
    target = data.get("target", 0)
    bv_list = [b.strip() for b in bv_str.split(",") if b.strip()]
    if not bv_list or not target:
        return jsonify({"error": "缺少 BV号 或 目标播放数"}), 400

    tid = str(uuid.uuid4())[:8]
    stop_event = threading.Event()
    with booster_lock:
        booster_tasks[tid] = {
            "status": "queued",
            "start": time.time(),
            "end": None,
            "bv": bv_str,
            "target": target,
            "stop_event": stop_event,
        }
    with log_lock:
        log_buffers[tid] = []
    t = threading.Thread(target=_run_booster_task, args=(tid, bv_list, int(target), stop_event), daemon=True)
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


@app.route("/api/booster/status/<task_id>")
def booster_status(task_id):
    st = booster_tasks.get(task_id)
    if not st:
        return jsonify({"error": "not found"}), 404
    safe = {k: v for k, v in st.items() if k != "stop_event"}
    return jsonify({**safe, "output": _get_log(task_id)})


@app.route("/api/booster/my-videos")
def booster_my_videos():
    """获取登录账号最近的投稿视频列表（通过 bilibili_api 库，自动处理 wbi/buvid）"""
    cred = _read_auto_cred()
    sessdata = cred.get("sessdata", "")
    mid = cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or ""
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先在「自动互动」页面登录账号"})

    try:
        from bilibili_api.utils.network import Credential
        from bilibili_api.user import User, VideoOrder

        # Credential 会自动处理 SESSDATA 编码和 buvid 获取
        credential = Credential(
            sessdata=sessdata,
            bili_jct=cred.get("bili_jct", ""),
            dedeuserid=str(mid),
            ac_time_value=cred.get("ac_time_value", ""),
        )
        u = User(int(mid), credential=credential)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(u.get_videos(ps=30, order=VideoOrder.PUBDATE))
        finally:
            loop.close()

        vlist = result.get("list", {}).get("vlist", [])
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
        return jsonify({"success": True, "videos": videos, "mid": mid})
    except Exception as e:
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[ERROR] player_my_videos: {error_msg}")
        traceback.print_exc()
        return jsonify({"success": False, "message": error_msg})


@app.route("/api/booster/tasks")
def booster_all():
    with booster_lock:
        return jsonify(booster_tasks)


# =========================================================================
#  三-B、B站播放量提升 — Playwright 模拟播放 (bili-player)
# =========================================================================

player_tasks = {}
player_lock = threading.Lock()


def _run_player_task(task_id: str, bv_list: list[str], rounds: int, stop_event: threading.Event = None):
    with player_lock:
        player_tasks[task_id]["status"] = "running"

    # 通过 log_fn 回调直接写入 task buffer，不再重定向 sys.stdout
    def log_fn(msg: str):
        _append_log(task_id, msg)

    try:
        # 检测 Playwright Chromium 是否已安装，未安装则自动安装
        _append_log(task_id, "[SYSTEM] 检查 Playwright Chromium ...")
        import subprocess as _sp
        check = _sp.run(
            [sys.executable, "-c",
             "import os; import playwright; d=os.path.dirname(playwright.__file__); "
             "browsers=os.path.join(d,'.local-browsers'); "
             "exit(0 if os.path.isdir(browsers) and any('chromium' in x for x in os.listdir(browsers)) else 1)"],
            capture_output=True, timeout=10,
        )
        if check.returncode != 0:
            _append_log(task_id, "[SYSTEM] Chromium 未安装，正在自动安装（约 150MB）...")
            install = _sp.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, timeout=600,
            )
            if install.returncode != 0:
                _append_log(task_id, "[ERROR] Chromium 安装失败，请手动执行: python -m playwright install chromium")
                with player_lock:
                    player_tasks[task_id]["status"] = "error"
                return
            _append_log(task_id, "[SYSTEM] Chromium 安装完成！")

        import importlib.util
        spec = importlib.util.spec_from_file_location("player", str(ROOT / "bili-player" / "player.py"))
        player = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(player)

        bv_input = ",".join(bv_list)
        # main() 现在是异步函数，需要用 asyncio.run() 执行
        import asyncio
        asyncio.run(player.main(bv_input, rounds=rounds, stop_event=stop_event, log_fn=log_fn))
        with player_lock:
            player_tasks[task_id]["status"] = "completed"
    except Exception as e:
        _append_log(task_id, f"[ERROR] {e}")
        with player_lock:
            player_tasks[task_id]["status"] = "error"
    finally:
        with player_lock:
            player_tasks[task_id]["end"] = time.time()


@app.route("/api/player/run", methods=["POST"])
def player_run():
    data = request.json
    bv_str = data.get("bv", "")
    rounds = int(data.get("rounds", 1))
    bv_list = [b.strip() for b in bv_str.split(",") if b.strip()]
    if not bv_list:
        return jsonify({"error": "缺少 BV号"}), 400

    tid = str(uuid.uuid4())[:8]
    stop_event = threading.Event()
    with player_lock:
        player_tasks[tid] = {
            "status": "queued",
            "start": time.time(),
            "end": None,
            "bv": bv_str,
            "rounds": rounds,
            "stop_event": stop_event,
        }
    with log_lock:
        log_buffers[tid] = []
    t = threading.Thread(target=_run_player_task, args=(tid, bv_list, rounds, stop_event), daemon=True)
    t.start()
    return jsonify({"task_id": tid})


@app.route("/api/player/stop", methods=["POST"])
def player_stop():
    data = request.json or {}
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"error": "缺少 task_id"}), 400
    with player_lock:
        st = player_tasks.get(task_id)
        if not st:
            return jsonify({"error": "任务不存在"}), 404
        se = st.get("stop_event")
        if se:
            se.set()
        st["status"] = "stopping"
    return jsonify({"success": True})


@app.route("/api/player/status/<task_id>")
def player_status(task_id):
    st = player_tasks.get(task_id)
    if not st:
        return jsonify({"error": "not found"}), 404
    safe = {k: v for k, v in st.items() if k != "stop_event"}
    return jsonify({**safe, "output": _get_log(task_id)})


@app.route("/api/player/tasks")
def player_all():
    with player_lock:
        safe = {}
        for k, v in player_tasks.items():
            safe[k] = {kk: vv for kk, vv in v.items() if kk != "stop_event"}
        return jsonify(safe)


@app.route("/api/player/my-videos")
def player_my_videos():
    """获取主账号（auto 模块）最近的投稿视频列表，player 登录仅用于播放"""
    cred = _read_auto_cred()
    sessdata = cred.get("sessdata", "")
    mid = cred.get("dedeuserid") or cred.get("mid") or cred.get("login_uid") or ""
    if not sessdata or not mid:
        return jsonify({"success": False, "message": "请先在「自动互动」页面登录主账号"})

    try:
        from bilibili_api.utils.network import Credential
        from bilibili_api.user import User, VideoOrder

        credential = Credential(
            sessdata=sessdata,
            bili_jct=cred.get("bili_jct", ""),
            dedeuserid=str(mid),
            ac_time_value=cred.get("ac_time_value", ""),
        )
        u = User(int(mid), credential=credential)

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(u.get_videos(ps=30, order=VideoOrder.PUBDATE))
        finally:
            loop.close()

        vlist = result.get("list", {}).get("vlist", [])
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
        return jsonify({"success": True, "videos": videos, "mid": mid})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


PLAYER_DIR = str(ROOT / "bili-player")
PLAYER_CONFIG = os.path.join(PLAYER_DIR, "config.yaml")


def _update_player_config(sessdata, bili_jct, buvid3, login_uid):
    import yaml
    cfg = {}
    if os.path.exists(PLAYER_CONFIG):
        with open(PLAYER_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    cfg.setdefault("bilibili", {}).update({
        "sessdata": sessdata,
        "bili_jct": bili_jct,
        "login_uid": int(login_uid) if login_uid else 0,
    })
    if buvid3:
        cfg["bilibili"]["buvid3"] = buvid3
    with open(PLAYER_CONFIG, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


@app.route("/api/player/config")
def player_config():
    import yaml
    if os.path.exists(PLAYER_CONFIG):
        with open(PLAYER_CONFIG, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return jsonify({
            "sessdata": cfg.get("bilibili", {}).get("sessdata", ""),
            "login_uid": cfg.get("bilibili", {}).get("login_uid", ""),
        })
    return jsonify({"sessdata": "", "login_uid": ""})


@app.route("/api/player/login/qrcode")
def player_qr():
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
        import traceback
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"[ERROR] player_qr: {error_msg}")
        traceback.print_exc()
        return jsonify({"success": False, "message": error_msg})


@app.route("/api/player/login/poll/<qrcode_key>")
def player_poll(qrcode_key):
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
            cookies = dict(resp.cookies.items())
            sessdata = cookies.get("SESSDATA", "")
            bili_jct = cookies.get("bili_jct", "")
            buvid3 = cookies.get("buvid3", "")
            login_uid = cookies.get("DedeUserID", "")

            _update_player_config(sessdata, bili_jct, buvid3, login_uid)
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


# =========================================================================
#  四、B站直播间红包助手 (bili-redpocket)
# =========================================================================

REDPOCKET_DIR = str(ROOT / "bili-redpocket")
REDPOCKET_SCRIPT = os.path.join(REDPOCKET_DIR, "auto_send_red_pocket.py")
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


# ---- 红包房间管理（直接读写源文件中的 WATCH_ROOMS） ----

def _read_watch_rooms():
    """解析 auto_send_red_pocket.py 中的 WATCH_ROOMS"""
    if not os.path.exists(REDPOCKET_SCRIPT):
        return []
    with open(REDPOCKET_SCRIPT, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r"WATCH_ROOMS\s*=\s*\[(.*?)\]", content, re.DOTALL)
    if not match:
        return []
    rooms_text = match.group(1)
    rooms = []
    pattern = (
        r"\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
        r'\s*"([^"]+)"\s*'
        r'(?:,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*(\d+)\s*,\s*"([^"]*)"\s*)?'
        r"\)"
    )
    for m in re.finditer(pattern, rooms_text):
        rooms.append({
            "room_id": int(m.group(1)),
            "red_pocket_id": int(m.group(2)),
            "duration": int(m.group(3)),
            "count": int(m.group(4)),
            "danmu_msg": m.group(5) or "",
            "uname": m.group(6) or "",
            "title": m.group(7) or "",
            "face": m.group(8) or "",
            "uid": int(m.group(9)) if m.group(9) else 0,
            "cover_from_user": m.group(10) or "",
        })
    return rooms


def _write_watch_rooms(rooms):
    """将 WATCH_ROOMS 写回 auto_send_red_pocket.py"""
    if not os.path.exists(REDPOCKET_SCRIPT):
        return
    with open(REDPOCKET_SCRIPT, "r", encoding="utf-8") as f:
        content = f.read()

    lines = []
    for r in rooms:
        parts = [
            str(r["room_id"]),
            str(r["red_pocket_id"]),
            str(r.get("duration", 600)),
            str(r.get("count", 1)),
            f'"{r.get("danmu_msg", "")}"',
        ]
        if r.get("uname"):
            parts += [f'"{r["uname"]}"', f'"{r.get("title", "")}"', f'"{r.get("face", "")}"',
                       str(r.get("uid", 0)), f'"{r.get("cover_from_user", "")}"']
        lines.append("    (" + ", ".join(parts) + ")")

    new_block = "WATCH_ROOMS = [\n" + ",\n".join(lines) + "\n]"
    content = re.sub(r"WATCH_ROOMS\s*=\s*\[(.*?)\]", new_block, content, flags=re.DOTALL)
    with open(REDPOCKET_SCRIPT, "w", encoding="utf-8") as f:
        f.write(content)


@app.route("/api/redpocket/rooms")
def redpocket_rooms():
    return jsonify({"rooms": _read_watch_rooms()})


@app.route("/api/redpocket/room", methods=["POST"])
def redpocket_add_room():
    data = request.json
    rooms = _read_watch_rooms()
    rooms.append({
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
    })
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
    rooms[index].update({
        "room_id": int(data.get("room_id", rooms[index]["room_id"])),
        "red_pocket_id": int(data.get("red_pocket_id", rooms[index]["red_pocket_id"])),
        "duration": int(data.get("duration", rooms[index].get("duration", 600))),
        "count": int(data.get("count", rooms[index].get("count", 1))),
        "danmu_msg": data.get("danmu_msg", rooms[index].get("danmu_msg", "")),
    })
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
    import yaml

    config_path = os.path.join(REDPOCKET_DIR, "config.yaml")
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
    import yaml

    config_path = os.path.join(REDPOCKET_DIR, "config.yaml")
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
    """读取 config.yaml 中的 livehelper 配置"""
    import yaml
    config_path = os.path.join(REDPOCKET_DIR, "config.yaml")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("livehelper", {})
    return {}


def _write_livehelper_config(lh_cfg):
    """更新 config.yaml 中的 livehelper 配置"""
    import yaml
    config_path = os.path.join(REDPOCKET_DIR, "config.yaml")
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
    import yaml
    config_path = os.path.join(REDPOCKET_DIR, "config.yaml")
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
#  五、前端入口
# =========================================================================

@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    import webbrowser
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        webbrowser.open("http://localhost:5678")
    app.run(debug=True, host="0.0.0.0", port=5678)
