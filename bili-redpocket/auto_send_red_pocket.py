#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from typing import *
from loguru import logger
import httpx

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

# 需要监听并自动发红包的直播间配置
# 格式: [(room_id, red_pocket_id, danmu_msg), ...]
WATCH_ROOMS = [
    (27263119, 189, 600, 1, "老板大气！点点红包抽礼物", "鸣潮", "《鸣潮》3.4版本前瞻通讯", "https://i2.hdslb.com/bfs/face/7258e7c765f82c5952c2accbab6fc5c1e16e663b.jpg", 1955897084, "https://i0.hdslb.com/bfs/live/new_room_cover/e4e08ee054dc1fe85789717d4fbc9538b06ee020.jpg")
]

ROOM_AREA_CACHE = {}

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
    """开播事件，收到开播事件后发送红包"""
    room_real_id = event["room_real_id"]
    logger.info(f"开播事件: [{room_real_id}]")

    if "live_time" not in event["data"]:
        logger.warning(f"[{room_real_id}] 缺少live_time，跳过")
        return

    try:
        # 查找该房间是否需要发送红包
        target_config = None
        for room_config in WATCH_ROOMS:
            if room_config[0] == room_real_id:
                target_config = room_config
                break

        if target_config is None:
            logger.debug(f"[{room_real_id}] 不在发送列表，跳过")
            return

        if len(target_config) >= 4:
            room_id = target_config[0]
            red_pocket_id = target_config[1]
            duration = target_config[2] if len(target_config) >= 3 else 300
            count = target_config[3] if len(target_config) >= 4 else 1
            danmu_msg = target_config[4] if len(target_config) >= 5 else "老板大气！点点红包抽礼物"
        elif len(target_config) >= 3:
            room_id = target_config[0]
            red_pocket_id = target_config[1]
            duration = target_config[2] if len(target_config) >= 3 else 300
            count = 1
            danmu_msg = target_config[3] if len(target_config) >= 4 else "老板大气！点点红包抽礼物"
        else:
            room_id, red_pocket_id, danmu_msg = target_config
            duration = 300
            count = 1
        logger.info(f"检测到目标房间 [{room_id}] 开播，准备发送人气红包，时长: {duration}秒，数量: {count}")

        # 优先从缓存获取房间和分区信息
        cached = ROOM_AREA_CACHE.get(room_real_id, {})
        target_uid = cached.get("target_uid")
        parent_area_id = cached.get("parent_area_id", 0)
        area_id = cached.get("area_id", 0)

        # 兜底：缓存未命中时实时获取
        if target_uid is None:
            live_room = LiveRoom(room_real_id)
            room_info = await live_room.get_room_play_info()
            target_uid = room_info["uid"]
            logger.info(f"缓存未命中，实时获取房间信息: room_id={room_real_id}, target_uid={target_uid}")

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
                    logger.info(f"实时获取分区信息成功: parent_area_id={parent_area_id}, area_id={area_id}")

        # 发送红包
        credential = get_credential()
        gift_live_room = LiveRoom(room_real_id, credential)
        for i in range(count):
            for attempt in range(3):
                try:
                    result = await gift_live_room.send_red_pocket(
                        red_pocket_id=red_pocket_id,
                        danmu_id=5,
                        danmu_msg=danmu_msg,
                        context_type=1,
                        parent_area_id=parent_area_id,
                        area_id=area_id,
                        duration=duration
                    )
                    logger.info(f"发送红包成功 [{i+1}/{count}]: {result}")
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"发送红包失败 [{i+1}/{count}]，第 {attempt + 1} 次重试: {e}")
                        await asyncio.sleep(0.5)
                    else:
                        logger.error(f"发送红包失败 [{i+1}/{count}]，已重试 3 次: {e}")
                        raise
        logger.info(f"全部 {count} 个红包发送完成")

    except Exception as e:
        logger.error(f"发送红包失败 [{room_real_id}]: {e}", exc_info=True)

async def on_preparing(event):
    """下播事件"""
    logger.info(f"下播事件: [{event['room_real_id']}]")

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
    config.set_credential(
        sessdata=bilibili_config.get('sessdata'),
        bili_jct=bilibili_config.get('bili_jct'),
        buvid3=bilibili_config.get('buvid3')
    )
    config.set("LOGIN_UID", bilibili_config.get('login_uid'))

    logger.info("BLiveListen凭证已设置。")

async def main():
    await init_resources()

    connectors = {}
    try:
        logger.info(f"开始连接需要监听的直播间: {WATCH_ROOMS}")

        for room_id, *_ in WATCH_ROOMS:
            live_room = LiveRoom(room_id)
            room_info = await live_room.get_room_play_info()
            uid = room_info["uid"]
            await add_connector(uid, room_id, connectors)
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
        await close_all_connectors(connectors)
        logger.info("所有资源已关闭。程序已终止。")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断 (Ctrl+C)。")
    except Exception as e:
        logger.critical(f"顶层错误: {e}", exc_info=True)
