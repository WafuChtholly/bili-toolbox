# config.py
# -*- coding: utf-8 -*-
import datetime
from zoneinfo import ZoneInfo
from typing import Union
import yaml
import os
from pathlib import Path

# 获取上海时区
shanghai_tz = ZoneInfo('Asia/Shanghai')
# B站登录账号的cookie的SESSDATA字段的值（保留兼容，实际从配置读取）
SESSDATA = '4f5da40b%2C1782701147%2C9435c%2Ac2CjC390H1q-XtSsM3HgusNh3re9iOtCyJWkRejVw0vw54S5ODv3kIa2bodjzX_WJKAEwSVmc4b1k3blItUVJ0ZE5MUjUyeVo2NFlNcXBOc2RlMEViR2d1aUxQRWdDUEp4Z3AwaEhCN0xvWmxTQ0FKU0t5NVM4WC1sYXdEcmFnbGRmUmxoY1hOdDBBIIEC'

# 配置文件统一放到项目 data 目录，避免 cookie 泄露在源码目录
CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "redpocket_config.yaml"
OLD_CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"


def _ensure_config():
    """迁移旧配置到 data 目录"""
    if not CONFIG_FILE.exists() and OLD_CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(OLD_CONFIG_FILE), str(CONFIG_FILE))


class Settings:
    def __init__(self):
        _ensure_config()

        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        else:
            config = {}

        # Bilibili映射
        self.BILIBILI_CONFIG = config.get('bilibili', {})

        # LiveHelper映射
        self.LIVEHELPER_CONFIG = config.get('livehelper', {})

        # 网络映射
        self.BROWSER_HEADERS = config.get('network', {}).get('browser_headers', {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

        # 日志映射
        self.LOG_LEVEL = config.get('logging', {}).get('level', 'INFO')

# 单例模式
settings = Settings()

# 时间戳转换辅助函数
def convert_timestamp_to_utc_datetime(timestamp: Union[int, float]) -> datetime.datetime:
    """
    将Unix时间戳（秒或毫秒）转换为带UTC时区的datetime对象。

    B站API通常返回毫秒级时间戳，但有时也会返回秒级时间戳。
    我们通过判断时间戳的大小来区分：
    - 如果时间戳 > 10000000000（约2286年的秒级时间戳），则认为是毫秒时间戳
    - 否则认为是秒级时间戳
    """
    try:
        # 判断是否为毫秒时间戳
        # 10000000000 对应 1970-04-26（秒级）
        # 10000000000000 对应 2286-11-20（毫秒级）
        if timestamp > 10000000000:
            # 毫秒时间戳，除以1000转换为秒
            timestamp_seconds = timestamp / 1000
        else:
            # 秒级时间戳
            timestamp_seconds = timestamp

        # 转换为UTC datetime对象
        return datetime.datetime.fromtimestamp(timestamp_seconds, tz=shanghai_tz)

    except (OSError, ValueError, OverflowError) as e:
        # 如果转换失败，记录错误并返回当前UTC时间
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to convert timestamp {timestamp}: {e}")
        return datetime.datetime.now(tz=shanghai_tz)
