"""
粉丝节养猫助手核心逻辑
移植自油猴脚本「B站养猫助手（粉丝牌搜索选择版）」（bilibli养猫助手独立签到摸猫版.js）。
功能：拉取粉丝牌 → 选择要养的猫 → 领养/签到/投喂手幅/喂食/摸自己猫/摸前20/摸全部猫咪。

说明：
- 所有函数均为模块级异步函数，接收 cookies dict（SESSDATA/bili_jct/DedeUserID）、
  log 回调与 stop_event（threading.Event）。
- 不包含原 JS 中的自动关注逻辑。
"""
# 兼容 Python 3.8 (Win7)
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sys
import threading
import time
import uuid

import httpx

# ==================== 常量 ====================
ACT_ID = 110505
RANK_ID = 300155
CLAIM_GIFT_ID = 35829  # 亲密度礼物（领取后进入包裹，需送出）
MEDAL_API = "https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/panel"
ACTIVITY_BASE = "https://api.live.bilibili.com/xlive/custom-activity-interface/activities2026"
RANK_API = "https://api.live.bilibili.com/xlive/custom-activity-interface/baseActivity/Rank"
BAG_LIST_API = "https://api.live.bilibili.com/xlive/web-room/v1/gift/bag_list"
SEND_BAG_API = "https://api.live.bilibili.com/xlive/revenue/v1/gift/sendBag"
PAGE_SIZE = 50
MAX_RETRY = 2
ROOM_DELAY_MIN = 3.0
ROOM_DELAY_MAX = 5.0

DEFAULT_OPTIONS = {
    "sign": True,        # 签到
    "feedBanner": False, # 投喂粉丝手幅*1（1电池）
    "feed": True,        # 喂食（消耗猫粮）
    "pet": True,         # 摸自己猫
    "petTop20": False,   # 摸前20猫咪
    "petAll": False,     # 摸全部猫咪
    "lightMedal": False, # 弹幕保牌：熄灭发10句点亮，未熄灭发1句保持
    "claimGift": False,  # 领取亲密度礼物并送出（gift_id 35829）
}

# 弹幕保牌用语池（熄灭时随机取 10 句，需 >= 10）
DANMAKU_POOL = [
    "[喝彩]",
    "[喝彩][喝彩]",
    "[喝彩][喝彩][喝彩]"
]

# 浏览器 UA：B 站风控会拦截 python-httpx 默认 UA（返回 412）
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class CatFatalError(Exception):
    """致命错误（如登录态失效 code=-101），任务应立即终止。"""


# ==================== 工具函数 ====================
async def _sleep(seconds: float, stop_event: threading.Event | None = None) -> bool:
    """可被 stop_event 打断的 sleep，返回 True 表示被中断。"""
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    loop = asyncio.get_event_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        if stop_event.is_set():
            return True
        await asyncio.sleep(min(0.5, max(0.0, end - loop.time())))
    return stop_event.is_set()


def _no_log(msg: str):
    pass


async def _get_json(client: httpx.AsyncClient, url: str) -> dict:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def _post_json(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    resp = await client.post(url, json=payload)
    resp.raise_for_status()
    return resp.json()


async def run_action(name: str, fn, log=None, stop_event=None) -> dict:
    """带重试的动作包装器：最多重试 MAX_RETRY 次；code=-101 视为登录态失效（致命）。"""
    log = log or _no_log
    last_err: Exception | None = None
    for attempt in range(MAX_RETRY + 1):
        try:
            result = await fn()
            if isinstance(result, dict) and result.get("code") == -101:
                raise CatFatalError(result.get("message") or "登录态失效")
            return result
        except CatFatalError:
            raise
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRY:
                log(f"⚠️ {name} 第 {attempt + 1} 次失败：{e}，2 秒后重试...")
                await asyncio.sleep(2)
    raise last_err  # type: ignore[misc]


def _live_headers() -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": BROWSER_UA,
        "Origin": "https://live.bilibili.com",
        "Referer": "https://live.bilibili.com/",
    }


# ==================== 粉丝牌拉取 ====================
async def _fetch_panel_items(client: httpx.AsyncClient, log=None, stop_event=None) -> list:
    """分页拉取粉丝牌面板原始条目（去重后返回 item 列表）。"""
    log = log or _no_log
    items_all: list = []
    seen = set()
    page = 1
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        url = f"{MEDAL_API}?page={page}&page_size={PAGE_SIZE}"
        res = await run_action(
            f"拉取粉丝牌第 {page} 页",
            lambda u=url: _get_json(client, u),
            log=log,
            stop_event=stop_event,
        )
        if not res or res.get("code") != 0:
            msg = (res or {}).get("message") if isinstance(res, dict) else None
            raise RuntimeError(f"获取粉丝牌失败：{msg or '未知错误'}")

        panel = res.get("data") or {}
        items = []
        if page == 1 and isinstance(panel.get("special_list"), list):
            items.extend(panel["special_list"])
        if isinstance(panel.get("list"), list):
            items.extend(panel["list"])

        for item in items:
            medal = item.get("medal") or {}
            ruid = medal.get("target_id")
            if ruid and str(ruid) not in seen:
                seen.add(str(ruid))
                items_all.append(item)

        log(f"  -> 第 {page} 页完成，累计 {len(items_all)} 个粉丝牌")
        has_more = (panel.get("page_info") or {}).get("has_more")
        if not has_more:
            break
        page += 1
        await asyncio.sleep(1)
    return items_all


def _panel_client(cookies: dict) -> httpx.AsyncClient:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": BROWSER_UA,
        "Origin": "https://link.bilibili.com",
        "Referer": "https://link.bilibili.com/p/center/index",
    }
    return httpx.AsyncClient(cookies=cookies, headers=headers, timeout=15)


def _item_summary(item: dict) -> dict:
    """从面板原始条目提取粉丝牌摘要（含直播间 room_id 与开播状态）。"""
    medal = item.get("medal") or {}
    room = item.get("room_info") or {}
    return {
        "ruid": str(medal.get("target_id")),
        "target_name": (item.get("anchor_info") or {}).get("nick_name") or "未知主播",
        "medal_name": medal.get("medal_name") or "",
        "is_lighted": int(medal.get("is_lighted") or 0),
        "room_id": int(room.get("room_id") or 0),
        "living_status": int(room.get("living_status") or 0),
    }


async def fetch_all_medals(cookies: dict, log=None, stop_event=None) -> list:
    """分页拉取全部粉丝牌，返回 [{ruid, target_name, medal_name, is_lighted, room_id, living_status}, ...]"""
    async with _panel_client(cookies) as client:
        items = await _fetch_panel_items(client, log=log, stop_event=stop_event)
    return [_item_summary(item) for item in items]


async def fetch_medal_light_map(cookies: dict, log=None, stop_event=None) -> dict:
    """分页拉取粉丝牌面板，返回 {ruid: {lighted, room_id, living}} 的最新状态。"""
    async with _panel_client(cookies) as client:
        items = await _fetch_panel_items(client, log=log, stop_event=stop_event)
    return {
        s["ruid"]: {"lighted": s["is_lighted"], "room_id": s["room_id"], "living": s["living_status"]}
        for s in (_item_summary(item) for item in items)
    }


# ==================== 养猫活动 API ====================
async def api_select_cat(client: httpx.AsyncClient, csrf: str, ruid: str) -> dict:
    url = f"{ACTIVITY_BASE}/Q3FansS1MiaoZaiSelectCat?csrf={csrf}"
    return await _post_json(client, url, {"act_id": ACT_ID, "ruid": str(ruid), "cat_type": 2})


async def api_sign_in(client: httpx.AsyncClient, csrf: str, ruid: str) -> dict:
    url = f"{ACTIVITY_BASE}/Q3FansS1MiaoZaiSignIn?csrf={csrf}"
    return await _post_json(client, url, {"act_id": ACT_ID, "ruid": str(ruid)})


async def api_feed_cat(client: httpx.AsyncClient, csrf: str, uid: str, ruid: str) -> dict:
    url = f"{ACTIVITY_BASE}/Q3FansS1MiaoZaiFeedCat?csrf={csrf}"
    return await _post_json(client, url, {"act_id": ACT_ID, "ruid": str(ruid), "target_uid": str(uid)})


async def api_pet_cat(client: httpx.AsyncClient, csrf: str, uid: str, ruid: str) -> dict:
    url = f"{ACTIVITY_BASE}/Q3FansS1MiaoZaiPetCat?csrf={csrf}"
    return await _post_json(client, url, {"act_id": ACT_ID, "ruid": str(ruid), "target_uid": str(uid)})


async def api_claim_gift(client: httpx.AsyncClient, csrf: str, ruid: str) -> dict:
    """领取亲密度礼物（按亲密度折算，自动放入礼物包裹）。"""
    url = f"{ACTIVITY_BASE}/Q3FansS1MiaoZaiClaimGift?csrf={csrf}"
    return await _post_json(client, url, {"act_id": ACT_ID, "ruid": str(ruid)})


async def get_room_id_by_ruid(client: httpx.AsyncClient, ruid: str) -> int:
    """通过主播 uid 查询直播间 room_id。"""
    try:
        master = await _get_json(
            client, f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={ruid}")
        if master.get("code") == 0 and (master.get("data") or {}).get("room_id"):
            return master["data"]["room_id"]
    except Exception:
        pass
    res = await _get_json(
        client, f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={ruid}")
    if res.get("code") == 0 and (res.get("data") or {}).get("roomid"):
        return res["data"]["roomid"]
    raise RuntimeError(f"未找到主播 {ruid} 的直播间")


async def get_living_status_by_ruid(client: httpx.AsyncClient, ruid: str) -> int:
    """查询主播开播状态：1=开播，0=未开播。"""
    try:
        res = await _get_json(
            client,
            f"https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids?uids[]={ruid}")
        if res.get("code") == 0:
            info = (res.get("data") or {}).get(str(ruid)) or {}
            return int(info.get("live_status") or 0)
    except Exception:
        pass
    return 0


async def api_like_room(client: httpx.AsyncClient, csrf: str, room_id: int, ruid: str) -> bool:
    """直播间点赞 1 次（粉丝牌任务：开播时点赞30次）。"""
    resp = await client.post(
        "https://api.live.bilibili.com/xlive/web-ucenter/v1/interact/likeInteract",
        data={"click_time": "1", "roomid": str(room_id), "csrf": csrf, "csrf_token": csrf},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": BROWSER_UA,
            "Referer": f"https://live.bilibili.com/{room_id}",
        },
    )
    resp.raise_for_status()
    return (resp.json() or {}).get("code") == 0


async def api_feed_banner(client: httpx.AsyncClient, csrf: str, uid: str, ruid: str) -> dict:
    """投喂粉丝手幅*1（消耗1电池），需先查 room_id。"""
    room_id = await get_room_id_by_ruid(client, ruid)
    params = {
        "uid": str(uid),
        "ruid": str(ruid),
        "send_ruid": "0",
        "gift_id": "35469",
        "gift_num": "1",
        "price": "100",
        "biz_id": str(room_id),
        "biz_code": "live",
        "storm_beat_id": "0",
        "metadata": "",
        "coin_type": "gold",
        "platform": "pc",
        "csrf": csrf,
        "csrf_token": csrf,
        "rnd": str(int(time.time())),
    }
    resp = await client.post(
        "https://api.live.bilibili.com/xlive/revenue/v1/gift/sendGold",
        data=params,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": BROWSER_UA,
            "Referer": f"https://live.bilibili.com/{room_id}",
        },
    )
    resp.raise_for_status()
    return resp.json()


async def _livehelper_credential(cookies: dict):
    """把 cookies 写入 blivelisten 全局配置（与弹幕发送走同一路径），返回 get_credential() 实例。"""
    _setup_blive_credential(cookies or {})
    _ensure_redpocket_importable()
    from blivelisten.utils.utils import get_credential
    return get_credential()


async def _fetch_wbi_keys(cookies: dict) -> tuple:
    """裸 aiohttp 调 nav 接口拿 wbi keys + bili_ticket。"""
    import aiohttp, hmac as _hmac
    sessdata = (cookies or {}).get("SESSDATA") or (cookies or {}).get("sessdata") or ""
    bili_jct = (cookies or {}).get("bili_jct") or (cookies or {}).get("BILI_JCT") or ""
    buvid3 = (cookies or {}).get("buvid3") or (cookies or {}).get("BUVID3") or ""
    buvid4 = (cookies or {}).get("buvid4") or (cookies or {}).get("BUVID4") or ""
    ac_time = (cookies or {}).get("ac_time_value") or ""
    if not buvid3:
        import uuid as _u
        buvid3 = f"XY{_u.uuid4().hex.upper()}"

    cookie_parts = [f"SESSDATA={sessdata}", f"bili_jct={bili_jct}", f"buvid3={buvid3}"]
    if buvid4:
        cookie_parts.append(f"buvid4={buvid4}")
    if ac_time:
        cookie_parts.append(f"ac_time_value={ac_time}")
    cookie_str = "; ".join(cookie_parts)
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer": "https://www.bilibili.com",
        "Cookie": cookie_str,
    }

    # 1. nav 拿 wbi_img
    async with aiohttp.ClientSession() as sess:
        async with sess.get("https://api.bilibili.com/x/web-interface/nav",
                            headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            nav = json.loads(await resp.text())
    if nav.get("code") != 0:
        raise RuntimeError(f"获取 wbi keys 失败：{nav.get('message') or '未知错误'}")
    wbi = (nav.get("data") or {}).get("wbi_img") or {}
    img_key = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub_key = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]

    # 2. bili_ticket（JWT，3 天有效）
    import time as _t
    ts = int(_t.time())
    hexsign = _hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
    ticket_url = (f"https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
                  f"?key_id=ec02&hexsign={hexsign}&context[ts]={ts}&csrf={bili_jct}")
    async with aiohttp.ClientSession() as sess:
        async with sess.post(ticket_url, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
            tk = json.loads(await resp.text())
    bili_ticket = (tk.get("data") or {}).get("ticket") or ""

    return img_key, sub_key, bili_ticket, cookie_str


async def get_bag_gift_list(client: httpx.AsyncClient, gift_id: int, room_id: int = 0,
                            cookies: dict | None = None) -> list:
    """查询礼物包裹中指定 gift_id 的条目。

    真实浏览器抓包发现：必须传 room_id + receive_users + mobi_app + web_location，
    且 cookie 头里要带 bili_ticket + ac_time_value；任一缺失会被 40000 风控。
    """
    _ensure_redpocket_importable()
    sessdata = (cookies or {}).get("SESSDATA") or (cookies or {}).get("sessdata") or ""
    if not sessdata:
        log(f"⚠️ [亲密度礼][diag] get_bag_gift_list cookies 实际值: {dict(cookies or {})}")
        raise RuntimeError("未登录：SESSDATA 为空")

    from blivelisten.utils.wbi import enc_wbi
    img_key, sub_key, bili_ticket, cookie_str = await _fetch_wbi_keys(cookies or {})

    if bili_ticket:
        cookie_str = cookie_str + f"; bili_ticket={bili_ticket}"

    # 业务参数：room_id 必须传（B 站按房间过滤可用礼物）；receive_users/mobi_app/web_location
    # 与 web 端一致
    import aiohttp
    params = {"room_id": str(room_id), "receive_users": "[]"}
    signed = enc_wbi(params, img_key, sub_key)
    qs = (f"wts={signed['wts']}&w_rid={signed['w_rid']}"
          f"&room_id={room_id}&receive_users=%5B%5D"
          f"&mobi_app=web&web_location=444.8")
    url = f"{BAG_LIST_API}?{qs}"
    headers = {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "origin": "https://live.bilibili.com",
        "referer": "https://live.bilibili.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": BROWSER_UA,
        "Cookie": cookie_str,
    }
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            data = json.loads(text)
    if data.get("code") != 0:
        raise RuntimeError(f"获取礼物包裹失败：{data.get('message') or '未知错误'} (code={data.get('code')})")
    items = (data.get("data") or {}).get("list") or []
    out = []
    for it in items:
        try:
            if int(it.get("gift_id") or 0) == gift_id:
                out.append({
                    "bag_id": int(it.get("bag_id") or 0),
                    "gift_num": int(it.get("gift_num") or 0),
                    "gift_name": it.get("gift_name") or "",
                })
        except (TypeError, ValueError):
            continue
    return out


async def api_send_bag_gift(client: httpx.AsyncClient, csrf: str, uid: str, ruid: str,
                            room_id: int, bag_id: int, gift_id: int, gift_num: int,
                            cookies: dict | None = None) -> dict:
    """把包裹中的礼物送进直播间。裸 aiohttp POST（sendBag 端点对自动化开放）。"""
    _ensure_redpocket_importable()
    sessdata = (cookies or {}).get("SESSDATA") or (cookies or {}).get("sessdata") or ""
    bili_jct = (cookies or {}).get("bili_jct") or (cookies or {}).get("BILI_JCT") or ""
    buvid3 = (cookies or {}).get("buvid3") or (cookies or {}).get("BUVID3") or ""
    buvid4 = (cookies or {}).get("buvid4") or (cookies or {}).get("BUVID4") or ""
    ac_time = (cookies or {}).get("ac_time_value") or ""
    if not buvid3:
        import uuid as _u
        buvid3 = f"XY{_u.uuid4().hex.upper()}"
    if not sessdata or not bili_jct:
        raise RuntimeError("未登录：SESSDATA/bili_jct 为空")

    import aiohttp
    url = SEND_BAG_API
    form = {
        "uid": str(uid),
        "ruid": str(ruid),
        "send_ruid": "0",
        "gift_id": str(gift_id),
        "gift_num": str(gift_num),
        "bag_id": str(bag_id),
        "price": "0",
        "biz_id": str(room_id),
        "biz_code": "live",
        "platform": "pc",
        "storm_beat_id": "0",
        "metadata": "",
        "csrf": bili_jct,
        "csrf_token": bili_jct,
        "rnd": str(int(time.time())),
    }
    cookie_parts = [f"SESSDATA={sessdata}", f"bili_jct={bili_jct}", f"buvid3={buvid3}"]
    if buvid4:
        cookie_parts.append(f"buvid4={buvid4}")
    if ac_time:
        cookie_parts.append(f"ac_time_value={ac_time}")
    cookie_str = "; ".join(cookie_parts)
    headers = {
        "User-Agent": BROWSER_UA,
        "Referer": f"https://live.bilibili.com/{room_id}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_str,
    }
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, data=form, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = await resp.text()
            return json.loads(text)


# ==================== 弹幕发送（参考 bili-redpocket/livehelper.py 的实现） ====================
def _ensure_redpocket_importable():
    """把 bili-redpocket 目录加入 sys.path，便于导入 livehelper / blivelisten。"""
    rp = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bili-redpocket"))
    if rp not in sys.path:
        sys.path.insert(0, rp)


def _setup_blive_credential(cookies: dict):
    """参考 livehelper.init_credential：将当前账号凭证写入 blivelisten 配置。"""
    import uuid as _uuid
    from blivelisten.utils import config as blive_cfg
    sessdata = cookies.get("SESSDATA") or cookies.get("sessdata") or ""
    bili_jct = cookies.get("bili_jct") or cookies.get("BILI_JCT") or ""
    buvid3 = cookies.get("buvid3") or ""
    if not buvid3:
        buvid3 = f"XY{_uuid.uuid4().hex.upper()}"
    blive_cfg.set_credential(sessdata, bili_jct, buvid3)
    try:
        login_uid = int(cookies.get("DedeUserID") or cookies.get("dedeuserid") or 0)
    except (TypeError, ValueError):
        login_uid = 0
    blive_cfg.set("LOGIN_UID", login_uid)


async def send_danmaku_like_livehelper(cookies: dict, room_id: int, text: str) -> bool:
    """复用 livehelper 的弹幕发送（LiveRoom.send_danmaku，自动识别粉丝团表情标签）。"""
    _ensure_redpocket_importable()
    _setup_blive_credential(cookies)
    import livehelper
    from blivelisten.core.live import LiveRoom
    from blivelisten.utils.utils import get_credential
    live_room = LiveRoom(int(room_id), get_credential())
    return await livehelper.send_danmaku(live_room, text)


# ==================== 猫咪榜单 ====================
async def get_room_cat_list(client: httpx.AsyncClient, target_ruid: str, limit: int = 0,
                            log=None, stop_event=None) -> list:
    """获取某主播房间的猫咪榜单，返回 [{uid, name}, ...]。limit=20 表示前20名，0 表示全量。"""
    log = log or _no_log
    dimension = json.dumps({"ruid": str(target_ruid)}, separators=(",", ":"))
    cat_list: list = []
    seen = set()
    page_size = 20
    page = 0
    log(f"  -> 正在获取房间猫咪榜单 ({'前20名' if limit == 20 else '全量'})...")

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        start = page * page_size
        end = (limit - 1) if (limit > 0 and start + page_size > limit) else (start + page_size - 1)
        url = (f"{RANK_API}?act_id={ACT_ID}&rank_id={RANK_ID}&front_rank_type=3"
               f"&dimension_v2={dimension}&start={start}&end={end}")
        try:
            res = await run_action("拉取排行榜", lambda u=url: _get_json(client, u), log=log)
            items = (res.get("data") or {}).get("list") if res.get("code") == 0 else None
            if res.get("code") == 0 and isinstance(items, list) and items:
                for item in items:
                    uid = str(item.get("item_id") or "")
                    if uid and uid not in seen:
                        seen.add(uid)
                        nick = uid
                        try:
                            extra = json.loads(item.get("extra") or "{}")
                            nick = extra.get("nick_name") or nick
                        except Exception:
                            pass
                        cat_list.append({"uid": uid, "name": nick})
                if (limit > 0 and len(cat_list) >= limit) or len(items) < page_size:
                    break
                page += 1
                await asyncio.sleep(0.3)
            else:
                break
        except Exception as e:
            log(f"  -> ⚠️ 获取猫咪榜单失败: {e}")
            break
    return cat_list


# ==================== 群摸他人猫咪 ====================
async def run_mass_petting(client: httpx.AsyncClient, csrf: str, my_uid: str,
                           ruid: str, limit: int = 0, log=None, stop_event=None) -> None:
    log = log or _no_log
    cat_list = await get_room_cat_list(client, ruid, limit, log=log, stop_event=stop_event)
    others = [c for c in cat_list if c["uid"] != str(my_uid)]
    if not others:
        log("  -> [群摸] 排行榜未获取到其他用户的猫咪。")
        return
    log(f"  -> [群摸] 发现 {len(others)} 只他人猫咪，开始抚摸...")
    for i, cat in enumerate(others):
        if stop_event is not None and stop_event.is_set():
            break
        log(f"     [{i + 1}/{len(others)}] 正在摸 {cat['name']} ({cat['uid']})...")
        for _ in range(3):
            if stop_event is not None and stop_event.is_set():
                break
            try:
                await api_pet_cat(client, csrf, cat["uid"], ruid)
            except Exception:
                pass
            await asyncio.sleep(0.4)
        await asyncio.sleep(0.3)
    log("  -> [群摸] ✅ 完成他人猫咪抚摸。")


# ==================== 单房间主流程 ====================
async def run_room(client: httpx.AsyncClient, csrf: str, uid: str, ruid: str, name: str,
                   options: dict, log=None, stop_event=None,
                   light_info: dict | None = None,
                   cookies: dict | None = None) -> bool:
    """对单个主播房间执行养猫流程，返回是否完整执行完毕。"""
    log = log or _no_log
    opts = {**DEFAULT_OPTIONS, **(options or {})}

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    # 0. 保牌：开播点 30 赞；未开播时牌子点亮发 10 句弹幕、牌子熄灭跳过
    if opts.get("lightMedal"):
        linfo = (light_info or {}).get(str(ruid)) or {}
        lit = bool(linfo.get("lighted", 1))
        room_id = int(linfo.get("room_id") or 0)
        living = int(linfo.get("living", -1))
        try:
            if living < 0:  # 状态未知（缓存回退），实时查询开播状态
                if not room_id:
                    room_id = await get_room_id_by_ruid(client, ruid)
                living = await get_living_status_by_ruid(client, ruid)
            if living != 0:
                # 开播：无论牌子亮灭，点赞 30 次
                if not room_id:
                    room_id = await get_room_id_by_ruid(client, ruid)
                log(f"  -> [保牌] {name} 开播中（牌子{'点亮' if lit else '熄灭'}），准备点赞 30 次")
                ok_cnt = 0
                for i in range(1, 31):
                    if _stopped():
                        return False
                    try:
                        ok = await api_like_room(client, csrf, room_id, ruid)
                    except Exception as e:
                        log(f"  -> [保牌] 🛑 点赞失败（{e}），跳过保牌")
                        break
                    if ok:
                        ok_cnt += 1
                        log(f"  -> [保牌] ({i}/30) ✅ 点赞成功")
                    else:
                        log("  -> [保牌] 🛑 点赞失败，跳过保牌")
                        break
                    if i < 30 and await _sleep(random.uniform(0.5, 1.5), stop_event):
                        return False
                if ok_cnt:
                    log(f"  -> [保牌] ✅ 点赞完成 {ok_cnt}/30")
            elif lit:
                # 未开播 + 牌子点亮：发 10 句弹幕
                count = min(10, len(DANMAKU_POOL))
                log(f"  -> [保牌] {name} 牌子点亮、未开播，准备发送 {count} 句弹幕")
                if not room_id:
                    room_id = await get_room_id_by_ruid(client, ruid)
                for i, msg in enumerate(random.sample(DANMAKU_POOL, count), 1):
                    if _stopped():
                        return False
                    ok = await send_danmaku_like_livehelper(cookies or {}, room_id, msg)
                    if ok:
                        log(f"  -> [保牌] ({i}/{count}) ✅ {msg}")
                    else:
                        log("  -> [保牌] 🛑 弹幕发送失败（风控限制等），跳过保牌")
                        break
                    if i < count and await _sleep(random.uniform(3.0, 6.0), stop_event):
                        return False
            else:
                log(f"  -> [保牌] {name} 牌子熄灭且未开播，跳过")
        except Exception as e:
            log(f"  -> [保牌] ⚠️ 跳过：{e}")
        if await _sleep(1, stop_event):
            return False

    log(f"  -> [领养] {name} ({ruid})")
    select_res = await run_action("领养", lambda: api_select_cat(client, csrf, ruid),
                                  log=log, stop_event=stop_event)
    if select_res and select_res.get("code") == 0:
        log("  -> [领养] ✅ 成功")
    elif select_res:
        log(f"  -> [领养] ⚠️ {select_res.get('message') or '非零返回'}")
    if await _sleep(1, stop_event):
        return False

    # 1. 签到
    if opts.get("sign"):
        log(f"  -> [签到] {name} ({ruid})")
        sign_res = await run_action("签到", lambda: api_sign_in(client, csrf, ruid),
                                    log=log, stop_event=stop_event)
        if sign_res and sign_res.get("code") == 0:
            data = sign_res.get("data") or {}
            raw_food = data.get("food_balance")
            food = 1 if raw_food is None else (float(raw_food) or 0)
            log(f"  -> [签到] ✅ 获得 {food} 份猫粮")
        elif sign_res:
            log(f"  -> [签到] ⚠️ {sign_res.get('message') or '非零返回'}")
        if await _sleep(1, stop_event):
            return False

    # 2. 投喂粉丝手幅*1
    if opts.get("feedBanner"):
        log("  -> [手幅] 正在投喂粉丝手幅*1 (消耗1电池)...")
        banner_res = await run_action("投喂粉丝手幅",
                                      lambda: api_feed_banner(client, csrf, uid, ruid),
                                      log=log, stop_event=stop_event)
        if banner_res and banner_res.get("code") == 0:
            log("  -> [手幅] ✅ 投喂成功！")
        elif banner_res:
            log(f"  -> [手幅] ⚠️ {banner_res.get('message') or '投喂失败'}")
        if await _sleep(1, stop_event):
            return False

    # 3. 喂食（消耗猫粮）
    if opts.get("feed"):
        log("  -> [喂食] 开始消耗猫粮...")
        guard = 0
        while guard < 50 and not _stopped():
            feed_res = await run_action("喂食", lambda: api_feed_cat(client, csrf, uid, ruid),
                                        log=log, stop_event=stop_event)
            if feed_res and feed_res.get("code") == 0:
                d = feed_res.get("data") or {}
                delta = d.get("growth_delta") or 0
                log(f"  -> [喂食] ✅ 成长 +{delta} (Lv.{d.get('cat_level') or 1} 进度:{d.get('growth') or 0})")
                for lv in d.get("level_up_list") or []:
                    log(f"     🎉 升级：{lv.get('title') or '恭喜升级'}")
                if (d.get("food_balance") or 0) <= 0:
                    break
            elif feed_res:
                log(f"  -> [喂食] ⚠️ {feed_res.get('message') or '停止喂食'}")
                break
            guard += 1
            if await _sleep(1.5, stop_event):
                return False
        if await _sleep(1, stop_event):
            return False

    # 4. 摸自己猫
    if opts.get("pet"):
        log("  -> [摸自己] 开始摸猫，目标 50 经验...")
        total_exp = 0
        for i in range(1, 16):
            if _stopped():
                break
            pet_res = await run_action("摸猫", lambda: api_pet_cat(client, csrf, uid, ruid),
                                       log=log, stop_event=stop_event)
            if pet_res and pet_res.get("code") == 0:
                d = pet_res.get("data") or {}
                delta = d.get("growth_delta") or 0
                total_exp += delta
                log(f"  -> [摸自己] 第 {i} 次: 成长 +{delta} (本轮 {total_exp}/50 | "
                    f"Lv.{d.get('cat_level') or 1} 进度:{d.get('growth') or 0})")
                for lv in d.get("level_up_list") or []:
                    log(f"     🎉 升级：{lv.get('title') or '恭喜升级'}")
                if total_exp >= 50:
                    log("  -> [摸自己] 🎯 经验已满，结束抚摸。")
                    break
            elif pet_res:
                log(f"  -> [摸自己] 🛑 {pet_res.get('message') or '停止摸猫'}")
                break
            if await _sleep(2, stop_event):
                return False
        if _stopped():
            return False

    # 5. 摸前20猫咪
    if opts.get("petTop20") and not opts.get("petAll"):
        await run_mass_petting(client, csrf, uid, ruid, 20, log=log, stop_event=stop_event)
        if _stopped():
            return False

    # 6. 摸全部猫咪
    if opts.get("petAll"):
        await run_mass_petting(client, csrf, uid, ruid, 0, log=log, stop_event=stop_event)
        if _stopped():
            return False

    # 7. 领取亲密度礼物 → 送出包裹中的活动礼物
    if opts.get("claimGift"):
        claim_res = await run_action("领取亲密度礼物",
                                     lambda: api_claim_gift(client, csrf, ruid),
                                     log=log, stop_event=stop_event)
        # 判定本次是否成功领取到礼物
        claimed_ok = False
        if claim_res and claim_res.get("code") == 0:
            d = claim_res.get("data") or {}
            if d.get("code") == 0:
                log(f"  -> [亲密度礼] ✅ 领取成功：亲密度 {d.get('gift_intimacy') or 0}"
                    f"（claim_status: {d.get('gift_claim_status')}），礼物已放入包裹")
                claimed_ok = True
            else:
                # 业务失败（已领完/不在活动期等），用 💡 提示而不是 ⚠️
                log(f"  -> [亲密度礼] 💡 暂无可领取：{d.get('msg') or '领取失败'}")
        elif claim_res:
            log(f"  -> [亲密度礼] ⚠️ {claim_res.get('message') or '非零返回'}")
        if await _sleep(1, stop_event):
            return False
        # 领取失败时跳过查包裹+送礼（没礼物可送）
        if not claimed_ok:
            return not _stopped()
        # 把包裹中 gift_id=CLAIM_GIFT_ID 的礼物全部送进该直播间
        try:
            room_id = int(((light_info or {}).get(str(ruid)) or {}).get("room_id") or 0)
            if not room_id:
                room_id = await get_room_id_by_ruid(client, ruid)
            bag_items = await run_action("查询礼物包裹",
                                         lambda: get_bag_gift_list(client, CLAIM_GIFT_ID, room_id, cookies),
                                         log=log, stop_event=stop_event)
            if not bag_items:
                log("  -> [亲密度礼] 包裹中没有可送出的活动礼物。")
            else:
                total = sum(it["gift_num"] for it in bag_items)
                log(f"  -> [亲密度礼] 包裹中有 {total} 个活动礼物，开始送出...")
                sent = 0
                for it in bag_items:
                    if _stopped():
                        return False
                    send_res = await run_action(
                        "送出包裹礼物",
                        lambda b=it: api_send_bag_gift(client, csrf, uid, ruid, room_id,
                                                       b["bag_id"], CLAIM_GIFT_ID, b["gift_num"], cookies),
                        log=log, stop_event=stop_event)
                    if send_res and send_res.get("code") == 0:
                        sent += it["gift_num"]
                        log(f"  -> [亲密度礼] ✅ 送出 {it['gift_name'] or '活动礼物'} x{it['gift_num']}")
                    elif send_res:
                        log(f"  -> [亲密度礼] ⚠️ {send_res.get('message') or '送礼失败'}")
                    if await _sleep(1, stop_event):
                        return False
                if sent:
                    log(f"  -> [亲密度礼] 🎉 共送出 {sent} 个礼物")
        except Exception as e:
            log(f"  -> [亲密度礼] ⚠️ 送礼环节失败：{e}")
        if await _sleep(1, stop_event):
            return False

    return not _stopped()


# ==================== 养猫主循环 ====================
async def run_cat_loop(
    cookies: dict,
    options: dict,
    selected_ruids: list,
    medals: list,
    progress: dict,
    save_progress=None,
    log=None,
    stop_event: threading.Event | None = None,
) -> dict:
    """养猫主循环。返回汇总 {"done": n, "skipped_completed": n, "failed": n, "stopped": bool}。

    cookies: {"SESSDATA":..., "bili_jct":..., "DedeUserID":...}
    progress: {"date": "YYYY-MM-DD", "completed_ruids": [...]}
    save_progress: callable(progress) 每完成一个房间后回调持久化
    """
    log = log or _no_log
    csrf = cookies.get("bili_jct", "")
    uid = str(cookies.get("DedeUserID", ""))
    if not csrf or not uid:
        raise RuntimeError("缺少 bili_jct 或 DedeUserID，无法执行养猫")

    opts = {**DEFAULT_OPTIONS, **(options or {})}
    if not any([opts.get("sign"), opts.get("feedBanner"), opts.get("feed"),
                opts.get("pet"), opts.get("petTop20"), opts.get("petAll"),
                opts.get("lightMedal"), opts.get("claimGift")]):
        raise RuntimeError("未勾选任何功能，请至少勾选一项功能开关")

    selected = [str(r) for r in (selected_ruids or [])]
    if not selected:
        raise RuntimeError("请先选择至少一个粉丝牌")

    medal_map = {str(m.get("ruid")): m for m in (medals or [])}
    valid_selected = [r for r in selected if r in medal_map]
    invalid_count = len(selected) - len(valid_selected)
    completed = set(str(r) for r in progress.get("completed_ruids", []))
    pending = [r for r in valid_selected if r not in completed]
    completed_selected = len(valid_selected) - len(pending)
    if invalid_count > 0:
        log(f"⚠️ 有 {invalid_count} 个已选项在当前粉丝牌缓存中已失效，本次跳过。")
    log(f"🚀 开始养猫：已选 {len(selected)}，今日已完成 {completed_selected}，本次待执行 {len(pending)}。")
    _opt_labels = {
        "sign": "签到", "feedBanner": "投喂手幅", "feed": "喂食", "pet": "摸自己",
        "petTop20": "摸前20", "petAll": "摸全部", "lightMedal": "弹幕保牌", "claimGift": "亲密度礼",
    }
    enabled = [label for key, label in _opt_labels.items() if opts.get(key)]
    log(f"🔧 本次启用功能：{'、'.join(enabled) if enabled else '（无）'}")

    summary = {"done": 0, "skipped_completed": completed_selected,
               "failed": 0, "stopped": False}

    # 弹幕保牌：优先拉取最新状态（点亮/开播/room_id），失败时回退到缓存
    light_info: dict | None = None
    if opts.get("lightMedal"):
        log("💡 [保牌] 正在获取最新牌子状态（点亮/开播）...")
        try:
            light_info = await fetch_medal_light_map(cookies, log=_no_log, stop_event=stop_event)
        except Exception as e:
            log(f"⚠️ [保牌] 获取最新状态失败，使用缓存：{e}")
            light_info = {
                str(m.get("ruid")): {
                    "lighted": int(m.get("is_lighted", 1)),
                    "room_id": int(m.get("room_id") or 0),
                    "living": -1,
                } for m in (medals or [])
            }
        live_set = [r for r in valid_selected if (light_info.get(r) or {}).get("living", -1) == 1]
        off_set = [r for r in valid_selected if (light_info.get(r) or {}).get("living", -1) == 0]
        dark_off = sum(1 for r in off_set if not (light_info.get(r) or {}).get("lighted", 1))
        unk_cnt = len(valid_selected) - len(live_set) - len(off_set)
        log(f"💡 [保牌] 开播 {len(live_set)} 个（点赞30次）；未开播 {len(off_set)} 个，其中点亮 {len(off_set) - dark_off} 个（发10句弹幕）、熄灭 {dark_off} 个（跳过）"
            + (f"；状态未知 {unk_cnt} 个（运行时实时查询）" if unk_cnt else "") + "。")

    headers = _live_headers()
    # web-room 组接口（如礼物包裹 bag_list）有 buvid 风控，凭证通常只有登录三件套，
    # 缺 buvid3 会被拒（"网络异常，请稍后再试~"）。与弹幕发送（_setup_blive_credential）同一兜底策略。
    _cookies = dict(cookies)
    if not _cookies.get("buvid3"):
        _cookies["buvid3"] = f"XY{uuid.uuid4().hex.upper()}"
    async with httpx.AsyncClient(cookies=_cookies, headers=headers, timeout=15) as client:
        index = 0
        for ruid in pending:
            if stop_event is not None and stop_event.is_set():
                log("🛑 已收到停止指令，保存进度后退出。")
                summary["stopped"] = True
                break
            index += 1
            name = medal_map.get(ruid, {}).get("target_name") or ruid
            log(f"📍 [{index}/{len(pending)}] 正在处理：{name} ({ruid})")
            try:
                finished = await run_room(client, csrf, uid, ruid, name, opts,
                                          log=log, stop_event=stop_event,
                                          light_info=light_info,
                                          cookies=_cookies)
                if stop_event is not None and stop_event.is_set():
                    log(f"🛑 当前房间 {name} 未完整执行，未标记为已完成。")
                    summary["stopped"] = True
                elif finished:
                    completed.add(ruid)
                    progress["completed_ruids"] = sorted(completed)
                    if save_progress:
                        save_progress(progress)
                    summary["done"] += 1
                    log(f"✅ 已完成 {name}，进度已保存。")
                else:
                    log(f"⚠️ 房间 {name} 未完整执行，未标记为已完成。")
            except CatFatalError as e:
                log(f"❌ 登录态失效，停止任务：{e}")
                summary["stopped"] = True
                raise
            except Exception as e:
                summary["failed"] += 1
                log(f"⚠️ 房间 {name} ({ruid}) 处理失败，已跳过：{e}")

            if index < len(pending) and not (stop_event is not None and stop_event.is_set()):
                delay = random.uniform(ROOM_DELAY_MIN, ROOM_DELAY_MAX)
                log(f"⏳ 休息 {delay:.1f} 秒后继续...")
                if await _sleep(delay, stop_event):
                    log("🛑 已收到停止指令，保存进度后退出。")
                    summary["stopped"] = True
                    break

        if not (stop_event is not None and stop_event.is_set()):
            log("🎉 已选养猫任务全部执行完毕！")
    return summary
