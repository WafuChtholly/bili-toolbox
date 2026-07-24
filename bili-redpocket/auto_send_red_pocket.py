#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from typing import *
from pathlib import Path
from loguru import logger
import httpx
import yaml

from blivelisten.connect import UpConnector
from blivelisten.core.live import LiveRoom
from blivelisten.utils import config
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
logger.add(sink=sys.stdout, level=settings.LOG_LEVEL, format='{time:YYYY-MM-DD HH:mm:ss} | [REDPOCKET] {message}')
logger.add(
    sink=os.path.join('logs', '{time:YYYYMMDD}.log'),
    level=settings.LOG_LEVEL,
    encoding='utf-8',
    rotation='00:00',
    retention='30 days',
    format='{time:YYYY-MM-DD HH:mm:ss.SSS} | [REDPOCKET] {level: <8} | {name}:{function}:{line} - {message}'
)

# 需要监听并自动发红包的直播间配置，存储在 data/redpocket_rooms.yaml
ROOMS_CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "redpocket_rooms.yaml"


def _load_watch_rooms() -> list:
    """从配置文件加载 WATCH_ROOMS，返回元组列表"""
    try:
        if not ROOMS_CONFIG_FILE.exists():
            logger.warning(f"未找到房间配置文件: {ROOMS_CONFIG_FILE}")
            return []
        with open(ROOMS_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rooms = []
        for r in data.get("rooms", []):
            room_id = int(r["room_id"])
            red_pocket_id = int(r.get("red_pocket_id", 189))
            duration = int(r.get("duration", 600))
            count = int(r.get("count", 1))
            danmu_msg = r.get("danmu_msg", "")
            uname = r.get("uname", "")
            title = r.get("title", "")
            face = r.get("face", "")
            uid = int(r.get("uid", 0))
            cover = r.get("cover_from_user", "")

            is_battery = red_pocket_id == 0 or r.get("total_battery") is not None
            if is_battery:
                danmu_msg = ""
                tb = int(r.get("total_battery", 20))
                an = int(r.get("award_num", 10))
                jr = int(r.get("join_requirement", 0))
                t = (room_id, red_pocket_id, duration, count, danmu_msg,
                     uname, title, face, uid, cover, "battery", tb, an, jr)
            else:
                t = (room_id, red_pocket_id, duration, count, danmu_msg,
                     uname, title, face, uid, cover)
            rooms.append(t)
        return rooms
    except Exception as e:
        logger.error(f"加载房间配置文件失败: {e}")
        return []


WATCH_ROOMS = _load_watch_rooms()

ROOM_AREA_CACHE = {}

# 每个房间的发送循环状态
ROOM_SEND_TASKS: Dict[int, asyncio.Task] = {}     # room_real_id -> Task
ROOM_STOP_EVENTS: Dict[int, asyncio.Event] = {}    # room_real_id -> Event


def _find_room_config(room_real_id: int) -> tuple | None:
    """在 WATCH_ROOMS 中查找该房间的配置"""
    for room_config in WATCH_ROOMS:
        if room_config[0] == room_real_id:
            return room_config
    return None


def _parse_room_config(room_config: tuple) -> dict:
    """解析房间配置元组，返回结构化 dict"""
    room_id = room_config[0]
    red_pocket_id = room_config[1]
    duration = room_config[2] if len(room_config) >= 3 else 300
    max_count = room_config[3] if len(room_config) >= 4 else 0  # 0 = 无上限
    danmu_msg = room_config[4] if len(room_config) >= 5 else "老板大气！点点红包抽礼物"

    is_battery = red_pocket_id == 0
    battery_info = None
    if is_battery:
        danmu_msg = ""
        if len(room_config) >= 13 and isinstance(room_config[10], str) and room_config[10] == "battery":
            battery_info = {
                "total_battery": int(room_config[11]) * 100,
                "award_num": int(room_config[12]),
                "join_requirement": int(room_config[13])
            }
    return {
        "room_id": room_id,
        "red_pocket_id": red_pocket_id,
        "duration": duration,
        "max_count": max_count,  # 0 = 无上限
        "danmu_msg": danmu_msg,
        "is_battery": is_battery,
        "battery_info": battery_info,
    }


async def _get_room_area_info(room_real_id: int) -> tuple:
    """获取房间分区信息，优先从缓存获取"""
    cached = ROOM_AREA_CACHE.get(room_real_id, {})
    target_uid = cached.get("target_uid")
    parent_area_id = cached.get("parent_area_id", 0)
    area_id = cached.get("area_id", 0)

    if target_uid is not None:
        return target_uid, parent_area_id, area_id

    # 缓存未命中，实时获取
    live_room = LiveRoom(room_real_id)
    room_info = await live_room.get_room_play_info()
    target_uid = room_info["uid"]

    params = {"uids[]": target_uid}
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
            params=params,
            headers=settings.BROWSER_HEADERS
        )
        data = response.json()
        if data.get("code") == 0 and str(target_uid) in data["data"]:
            parent_area_id = data["data"][str(target_uid)]["area_v2_parent_id"]
            area_id = data["data"][str(target_uid)]["area_v2_id"]

    return target_uid, parent_area_id, area_id


async def _send_one_red_pocket(room_real_id: int, config: dict) -> bool:
    """发送一个红包，带重试逻辑"""
    credential = get_credential()
    gift_live_room = LiveRoom(room_real_id, credential)
    target_uid, parent_area_id, area_id = await _get_room_area_info(room_real_id)

    for attempt in range(3):
        try:
            result = await gift_live_room.send_red_pocket(
                red_pocket_id=config["red_pocket_id"],
                danmu_id=0 if config["is_battery"] else 5,
                danmu_msg=config["danmu_msg"],
                context_type=1,
                parent_area_id=parent_area_id,
                area_id=area_id,
                duration=config["duration"],
                battery_info=config["battery_info"]
            )
            logger.info(f"发送红包成功 [{room_real_id}]: {result}")
            return True
        except Exception as e:
            if attempt < 2:
                logger.warning(f"发送红包失败 [{room_real_id}]，第 {attempt + 1} 次重试: {e}")
                await asyncio.sleep(0.5)
            else:
                logger.error(f"发送红包失败 [{room_real_id}]，已重试 3 次: {e}")
                return False


async def _send_red_pocket_loop(room_real_id: int, config: dict):
    """
    红包发送循环（后台任务）
    发送一个红包 → 等待红包持续时间结束 → 发送下一个 → 直到达到上限或下播
    """
    stop_event = ROOM_STOP_EVENTS[room_real_id]
    max_count = config["max_count"]
    duration = config["duration"]
    sent_count = 0

    limit_str = "无上限" if max_count == 0 else str(max_count)
    logger.info(f"启动红包发送循环 [{room_real_id}]，发送上限: {limit_str}，红包时长: {duration}秒")

    try:
        while not stop_event.is_set():
            if max_count > 0 and sent_count >= max_count:
                logger.info(f"[{room_real_id}] 已达到发送上限 ({max_count}个)，停止发送")
                break

            ok = await _send_one_red_pocket(room_real_id, config)
            if not ok:
                logger.error(f"[{room_real_id}] 红包发送失败，停止发送循环")
                break

            sent_count += 1
            progress = f"{sent_count}/{max_count}" if max_count > 0 else f"{sent_count}/∞"
            logger.info(f"[{room_real_id}] 已发送 {progress} 个红包，等待 {duration} 秒后发送下一个...")

            # 等待红包持续时间结束（+5秒缓冲），再发下一个
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=duration + 5)
                logger.info(f"[{room_real_id}] 收到停止信号，停止发送循环")
                break
            except asyncio.TimeoutError:
                # 红包持续时间结束，继续发送下一个
                continue
    finally:
        # 清理状态（确保无论任务被取消还是正常结束都执行）
        ROOM_SEND_TASKS.pop(room_real_id, None)
        ROOM_STOP_EVENTS.pop(room_real_id, None)
        logger.info(f"红包发送循环已结束 [{room_real_id}]，共发送 {sent_count} 个红包")


async def _start_room_red_pocket_loop(room_real_id: int, config: dict):
    """启动某个房间的红包发送循环（如已有则跳过）"""
    if room_real_id in ROOM_SEND_TASKS and not ROOM_SEND_TASKS[room_real_id].done():
        logger.info(f"[{room_real_id}] 已有发送循环在运行，跳过")
        return

    stop_event = asyncio.Event()
    ROOM_STOP_EVENTS[room_real_id] = stop_event
    task = asyncio.create_task(_send_red_pocket_loop(room_real_id, config))
    ROOM_SEND_TASKS[room_real_id] = task


async def _stop_room_red_pocket_loop(room_real_id: int):
    """停止某个房间的红包发送循环"""
    if room_real_id in ROOM_STOP_EVENTS:
        ROOM_STOP_EVENTS[room_real_id].set()
    if room_real_id in ROOM_SEND_TASKS:
        task = ROOM_SEND_TASKS[room_real_id]
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---- 事件处理器 ----

async def on_verification_success(event):
    """验证成功事件"""
    room_real_id = event['room_real_id']
    logger.info(f"直播间 [{room_real_id}] 验证成功")

    try:
        live_room = LiveRoom(room_real_id)
        room_info = await live_room.get_room_play_info()
        target_uid = room_info["uid"]
        logger.info(f"验证成功后获取房间信息成功: room_id={room_real_id}, target_uid={target_uid}")

        params = {"uids[]": target_uid}
        parent_area_id = 0
        area_id = 0
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
                params=params,
                headers=settings.BROWSER_HEADERS
            )
            data = response.json()
            if data.get("code") == 0 and str(target_uid) in data["data"]:
                parent_area_id = data["data"][str(target_uid)]["area_v2_parent_id"]
                area_id = data["data"][str(target_uid)]["area_v2_id"]
                logger.info(f"验证成功后获取分区信息成功: parent_area_id={parent_area_id}, area_id={area_id}")

        ROOM_AREA_CACHE[room_real_id] = {
            "target_uid": target_uid,
            "parent_area_id": parent_area_id,
            "area_id": area_id
        }
    except Exception as e:
        logger.error(f"验证成功后获取分区信息失败 [{room_real_id}]: {e}", exc_info=True)


async def on_live(event):
    """开播事件：启动红包发送循环"""
    room_real_id = event["room_real_id"]
    logger.info(f"开播事件: [{room_real_id}]")

    if "live_time" not in event["data"]:
        logger.warning(f"[{room_real_id}] 缺少live_time，跳过")
        return

    target_config = _find_room_config(room_real_id)
    if target_config is None:
        logger.debug(f"[{room_real_id}] 不在发送列表，跳过")
        return

    config = _parse_room_config(target_config)
    await _start_room_red_pocket_loop(room_real_id, config)


async def on_preparing(event):
    """下播事件：停止发送循环"""
    room_real_id = event["room_real_id"]
    logger.info(f"下播事件: [{room_real_id}]")
    await _stop_room_red_pocket_loop(room_real_id)


async def register_event_handlers(connector):
    """注册事件处理器"""
    connector._UpConnector__room.on("VERIFICATION_SUCCESSFUL")(on_verification_success)
    connector._UpConnector__room.on("LIVE")(on_live)
    connector._UpConnector__room.on("PREPARING")(on_preparing)


async def add_connector(uid, room_id, connectors):
    """添加连接器"""
    try:
        connector = UpConnector(uid=uid, room_id=room_id)
        if await connector.connect():
            await register_event_handlers(connector)
            connectors[uid] = connector
            logger.info(f"成功添加主播监听: UID={uid}, RoomID={room_id}")
            return True
    except Exception as e:
        logger.error(f"添加主播监听失败 UID={uid} (房间 {room_id}): {e}", exc_info=True)
    return False


async def close_all_connectors(connectors):
    """关闭所有连接器"""
    logger.info("正在停止所有连接...")
    for uid, connector in connectors.items():
        await connector.disconnect()


async def init_resources():
    """初始化资源"""
    bilibili_config = settings.BILIBILI_CONFIG
    sessdata = bilibili_config.get('sessdata')
    bili_jct = bilibili_config.get('bili_jct')
    buvid3 = bilibili_config.get('buvid3')
    if not buvid3:
        # 生成默认 buvid3，避免 Credential 缺失该字段
        import uuid
        buvid3 = str(uuid.uuid4()).upper()
    config.set_credential(
        sessdata=sessdata,
        bili_jct=bili_jct,
        buvid3=buvid3
    )
    config.set("LOGIN_UID", bilibili_config.get('login_uid'))

    logger.info("BLiveListen凭证已设置。")


async def main():
    await init_resources()

    connectors = {}
    try:
        logger.info(f"开始连接需要监听的直播间: {WATCH_ROOMS}")

        for room_config in WATCH_ROOMS:
            room_id = room_config[0]
            live_room = LiveRoom(room_id)
            room_info = await live_room.get_room_play_info()
            uid = room_info["uid"]
            live_status = room_info.get("live_status", 0)

            await add_connector(uid, room_id, connectors)

            # 如果直播间已在直播中，立即启动红包发送循环
            if live_status == 1:
                logger.info(f"直播间 [{room_id}] 当前正在直播，启动红包发送循环")
                config = _parse_room_config(room_config)
                await _start_room_red_pocket_loop(room_id, config)

            await asyncio.sleep(2)

        logger.info(f"所有直播间已连接并运行，当前监听 {len(connectors)} 个主播。")

        stop_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop_signal")
        while True:
            await asyncio.sleep(2)
            if os.path.exists(stop_file):
                logger.info("收到停止信号，正在优雅关闭连接...")
                break

    except asyncio.CancelledError:
        logger.info("主程序已取消。正在优雅关闭...")
    except Exception as e:
        logger.critical(f"主程序中发生未处理的错误: {e}", exc_info=True)
    finally:
        # 停止所有发送循环
        for room_id in list(ROOM_SEND_TASKS.keys()):
            await _stop_room_red_pocket_loop(room_id)
        await close_all_connectors(connectors)
        logger.info("所有资源已关闭。程序已终止。")


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断 (Ctrl+C)。")
    except Exception as e:
        logger.critical(f"顶层错误: {e}", exc_info=True)