import asyncio
from loguru import logger
from .core.live import LiveDanmaku, LiveRoom
from .exception import LiveException
from .utils.network import request
from .utils.utils import get_credential


class UpConnector:
    """
    主播连接器类，用于连接直播间
    """
    def __init__(self, uid, uname=None, room_id=None):
        self.uid = uid
        self.uname = uname
        self.room_id = room_id
        self.__live_room = None
        self.__room = None
        self.__connecting = False
        self.__is_reconnect = False
        self.__loop = asyncio.get_event_loop()

    async def connect(self):
        """
        连接直播间
        """
        if self.__connecting:
            logger.warning(f"{self.uname} ( UID: {self.uid} ) 的直播间正在连接中, 跳过重复连接")
            return False
        self.__connecting = True

        # 获取用户信息和直播间信息
        if not all([self.uname, self.room_id]):
            user_info_url = f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={self.uid}"
            user_info = await request("GET", user_info_url)
            self.uname = user_info["info"]["uname"]
            if user_info["room_id"] == 0:
                raise LiveException(f"UP 主 {self.uname} ( UID: {self.uid} ) 还未开通直播间")
            self.room_id = user_info["room_id"]

        logger.opt(colors=True).info(f"准备连接到 <cyan>{self.uname}</> 的直播间 <cyan>{self.room_id}</>")

        self.__live_room = LiveRoom(self.room_id, get_credential())
        self.__room = LiveDanmaku(self.room_id, credential=get_credential())
        # 开始连接
        self.__loop.create_task(self.__room.connect())
        return True

    async def disconnect(self):
        """
        断开连接直播间
        """
        if self.__room is not None:
            await self.__room.disconnect()
            self.__is_reconnect = False
            logger.success(f"已断开连接 {self.uname} 的直播间 {self.room_id}")
