#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LiveHelper — 扫码登录 → 链接直播间 → 开播后定时发送随机语录
与 bili-redpocket 共享同一套 config.yaml 中的凭证
"""
import asyncio
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger

from blivelisten.core.live import LiveDanmaku, LiveRoom
from blivelisten.utils import config as blivelisten_config
from blivelisten.utils.Danmaku import Danmaku
from blivelisten.utils.utils import get_credential
from config import settings

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')


def get_log_dir():
    log_dir = 'logs'
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


get_log_dir()

logger.remove()
logger.add(sink=sys.stdout, level=settings.LOG_LEVEL, format='{time:YYYY-MM-DD HH:mm:ss} | [LIVEHELPER] {message}')
logger.add(
    sink=os.path.join('logs', '{time:YYYYMMDD}.log'),
    level=settings.LOG_LEVEL,
    encoding='utf-8',
    rotation='00:00',
    retention='30 days',
    format='{time:YYYY-MM-DD HH:mm:ss.SSS} | [LIVEHELPER] {level: <8} | {name}:{function}:{line} - {message}'
)


def init_credential(settings_obj=None):
    """从 config.yaml 读取凭证并设置到 blivelisten 配置中"""
    cfg = settings_obj or settings
    bilibili_config = cfg.BILIBILI_CONFIG
    sessdata = bilibili_config.get('sessdata')
    bili_jct = bilibili_config.get('bili_jct')
    buvid3 = bilibili_config.get('buvid3') or ''
    login_uid = bilibili_config.get('login_uid')

    if sessdata and bili_jct:
        if not buvid3:
            import uuid
            buvid3 = f'XY{uuid.uuid4().hex.upper()}'
        blivelisten_config.set_credential(sessdata, bili_jct, buvid3)
        blivelisten_config.set("LOGIN_UID", login_uid)
        logger.info("已从 config.yaml 读取凭证")
        return True
    else:
        logger.error("config.yaml 中缺少 sessdata / bili_jct，请先在 WebUI 中扫码登录")
        return False


async def send_danmaku(live_room: LiveRoom, text: str):
    """发送一条弹幕"""
    try:
        danmaku = Danmaku(text=text)
        await live_room.send_danmaku(danmaku)
        logger.info(f"发送弹幕成功: {text}")
        return True
    except Exception as e:
        logger.error(f"发送弹幕失败 [{text}]: {e}")
        return False


async def quote_sender_loop(room_id: int, quotes: list, interval: int, jitter: int,
                            skip_duplicate: bool, sent_history: list, stop_signal: asyncio.Event):
    """定时发送随机语录，直到收到停止信号"""
    credential = get_credential()
    live_room = LiveRoom(room_id, credential)

    logger.info(f"语录发送任务已启动，间隔: {interval}秒")
    while not stop_signal.is_set():
        if not quotes:
            await asyncio.sleep(5)
            continue

        # 选一句不重复的语录
        candidates = quotes
        if skip_duplicate and len(sent_history) < len(quotes):
            candidates = [q for q in quotes if q not in sent_history]
            if not candidates:
                candidates = quotes
                sent_history.clear()

        text = random.choice(candidates)
        ok = await send_danmaku(live_room, text)
        if ok and skip_duplicate:
            sent_history.append(text)
            if len(sent_history) > len(quotes):
                sent_history.pop(0)

        sleep_time = max(5, interval + random.randint(-jitter, jitter))
        try:
            await asyncio.wait_for(stop_signal.wait(), timeout=sleep_time)
        except asyncio.TimeoutError:
            pass  # 正常倒计时结束
        except asyncio.CancelledError:
            break

    logger.info("语录发送任务已停止")


async def main():
    cfg = reload_config()
    live_helper_cfg = cfg.LIVEHELPER_CONFIG
    if not live_helper_cfg.get('enabled', True):
        logger.info("livehelper 已禁用，退出")
        return

    room_id = live_helper_cfg.get('room_id')
    if not room_id:
        logger.error("未配置 livehelper.room_id，请在 config.yaml 中设置")
        return

    if not init_credential(cfg):
        return

    quotes = live_helper_cfg.get('quotes', [])
    interval = live_helper_cfg.get('interval_seconds', 60)
    jitter = live_helper_cfg.get('interval_jitter_seconds', 10)
    skip_duplicate = live_helper_cfg.get('skip_duplicate', True)

    credential = get_credential()
    room_id = int(room_id)

    # 第一步：获取房间信息和主播 UID
    live_room = LiveRoom(room_id, credential)
    room_info = await live_room.get_room_play_info()
    uid = room_info.get("uid")
    if not uid:
        logger.error(f"无法获取房间 {room_id} 的主播 UID")
        return
    logger.info(f"房间 {room_id} 的主播 UID: {uid}")

    # 第二步：连接 WebSocket
    room_ws = LiveDanmaku(room_id, credential=credential)
    ws_task = asyncio.create_task(room_ws.connect())

    # 给 WebSocket 一点时间建立连接
    await asyncio.sleep(3)

    # 第三步：状态管理
    sender_stop_signal = asyncio.Event()
    sender_task: asyncio.Task | None = None
    sent_history: list[str] = []

    async def on_live(event):
        nonlocal sender_task, sender_stop_signal
        logger.info(f"直播间 [{room_id}] 开播了！")
        if sender_task and not sender_task.done():
            logger.info("已有发送任务在运行，跳过")
            return
        sender_stop_signal = asyncio.Event()
        sender_task = asyncio.create_task(
            quote_sender_loop(room_id, quotes, interval, jitter, skip_duplicate, sent_history, sender_stop_signal)
        )
        logger.info("已启动语录发送任务")

    async def on_preparing(event):
        nonlocal sender_task
        logger.info(f"直播间 [{room_id}] 下播了")
        if sender_task and not sender_task.done():
            sender_stop_signal.set()
            sender_task.cancel()
            try:
                await sender_task
            except (asyncio.CancelledError, Exception):
                pass
            sender_task = None
            logger.info("语录发送任务已停止")

    room_ws.on("LIVE")(on_live)
    room_ws.on("PREPARING")(on_preparing)

    # 第四步：检查初始开播状态
    live_status = room_info.get("live_status", 0)
    if live_status == 1:
        logger.info(f"直播间 [{room_id}] 当前正在直播，立即启动语录发送")
        sender_stop_signal = asyncio.Event()
        sender_task = asyncio.create_task(
            quote_sender_loop(room_id, quotes, interval, jitter, skip_duplicate, sent_history, sender_stop_signal)
        )
    else:
        logger.info(f"直播间 [{room_id}] 未开播，等待开播事件中...")

    # 第五步：主循环，监控停止信号和任务状态
    try:
        stop_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop_signal")
        while True:
            await asyncio.sleep(2)
            if os.path.exists(stop_file):
                logger.info("收到停止信号，正在优雅关闭...")
                break

            # 检查 WebSocket 是否异常断开
            if ws_task.done():
                exc = ws_task.exception()
                if exc:
                    logger.error(f"WebSocket 连接异常退出: {exc}")
                    break

            # 检查发送任务是否异常退出
            if sender_task and sender_task.done() and not sender_task.cancelled():
                exc = sender_task.exception()
                if exc:
                    logger.error(f"语录发送任务异常退出: {exc}")
                    sender_task = None
                    # 不退出主循环，等待下次开播事件重新创建
    except asyncio.CancelledError:
        logger.info("主程序已取消")
    except Exception as e:
        logger.critical(f"主程序异常: {e}", exc_info=True)
    finally:
        if sender_task and not sender_task.done():
            sender_stop_signal.set()
            sender_task.cancel()
            try:
                await sender_task
            except Exception:
                pass
        if not ws_task.done():
            ws_task.cancel()
            await room_ws.disconnect()
        logger.info("所有资源已关闭")


def reload_config():
    import importlib
    import config as cfg_mod
    importlib.reload(cfg_mod)
    return cfg_mod.settings


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断 (Ctrl+C)")
    except Exception as e:
        logger.critical(f"顶层错误: {e}", exc_info=True)