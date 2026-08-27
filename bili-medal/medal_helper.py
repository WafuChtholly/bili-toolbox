# -*- coding: utf-8 -*-
"""B站粉丝灯牌管理模块（WebUI 后端）。

移植自 bilibili-fans-medal-manager.user.js：
分页拉取粉丝灯牌、批量移除、锁定保护（锁定项不参与批量移除）。
凭证复用 bili-auto 模块的主账号（credential.json）。
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bili-medal"

MEDAL_API = "https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/panel"
REMOVE_API = "https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/web_room/del_medal"

PAGE_SIZE = 50
MAX_PAGE = 90

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _headers() -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": BROWSER_UA,
        "Origin": "https://link.bilibili.com",
        "Referer": "https://link.bilibili.com/p/center/index",
    }


def _no_log(msg: str) -> None:
    pass


# ==================== 灯牌拉取 ====================

async def fetch_medals(cookies: dict, log=None, stop_event: threading.Event | None = None) -> list:
    """分页拉取全部粉丝灯牌。

    返回 [{target_id, medal_name, anchor_name, level, lit}, ...]
    """
    log = log or _no_log
    medals: list = []
    seen: set = set()

    def push_item(item: dict) -> None:
        info = item.get("medal") or item.get("medal_info") or {}
        anchor = item.get("anchor_info") or {}
        target_id = str(
            item.get("target_id")
            or info.get("target_id")
            or anchor.get("uid")
            or item.get("uid")
            or ""
        )
        if not target_id or target_id in seen:
            return
        seen.add(target_id)
        medals.append({
            "target_id": target_id,
            "medal_name": info.get("medal_name") or info.get("name") or item.get("medal_name") or "未知勋章",
            "anchor_name": (
                anchor.get("uname") or anchor.get("nick_name")
                or item.get("target_name") or info.get("target_name") or "未知主播"
            ),
            "level": int(info.get("level") or info.get("medal_level") or item.get("level") or 0),
            "lit": bool(info.get("is_lighted") == 1 or item.get("is_lighted") == 1),
        })

    async with httpx.AsyncClient(cookies=cookies, headers=_headers(), timeout=15) as client:
        for page in range(1, MAX_PAGE + 1):
            if stop_event is not None and stop_event.is_set():
                break
            if page > 1:
                await asyncio.sleep(0.15)
            res = await client.get(f"{MEDAL_API}?page={page}&page_size={PAGE_SIZE}")
            json_data = res.json()
            if json_data.get("code") != 0 or not json_data.get("data"):
                if page == 1:
                    raise RuntimeError("获取灯牌数据失败：" + str(json_data.get("message") or json_data.get("code")))
                break

            data = json_data["data"]
            if page == 1:
                for item in data.get("special_list") or []:
                    push_item(item)
            for item in data.get("list") or []:
                push_item(item)

            log(f"  -> 第 {page} 页完成，累计 {len(medals)} 个灯牌")

            page_info = data.get("page_info") or {}
            total_page = int(page_info.get("total_page") or 0)
            list_len = len(data.get("list") or [])
            if total_page:
                if page >= total_page:
                    break
                if page > 1 and list_len == 0:
                    break
            elif list_len < PAGE_SIZE:
                break
    return medals


# ==================== 灯牌移除 ====================

async def remove_medal(client: httpx.AsyncClient, csrf: str, target_id: str) -> dict:
    body = {
        "target_id": target_id,
        "csrf_token": csrf,
        "csrf": csrf,
    }
    try:
        res = await client.post(REMOVE_API, data=body)
        return res.json()
    except Exception:
        return {"code": -1, "message": "网络异常"}


async def batch_remove(
    cookies: dict,
    csrf: str,
    target_ids: list,
    log=None,
    stop_event: threading.Event | None = None,
) -> dict:
    """逐个移除灯牌，返回 {ok: n, fail: n, results: [...]}。"""
    log = log or _no_log
    results: list = []
    ok = 0
    fail = 0
    async with httpx.AsyncClient(cookies=cookies, headers=_headers(), timeout=15) as client:
        for i, tid in enumerate(target_ids):
            if stop_event is not None and stop_event.is_set():
                log("⏹️ 用户中断，停止移除")
                break
            if i > 0:
                await asyncio.sleep(0.2)
            res = await remove_medal(client, csrf, str(tid))
            is_ok = res.get("code") == 0
            if is_ok:
                ok += 1
            else:
                fail += 1
            results.append({
                "target_id": str(tid),
                "ok": is_ok,
                "message": str(res.get("message") or ""),
            })
            log(f"  -> 移除 {tid}: {'成功' if is_ok else '失败 ' + str(res.get('message'))}")
    return {"ok": ok, "fail": fail, "results": results}


# ==================== 锁定存储（按账号隔离） ====================

def _locks_file(uid) -> Path:
    return DATA_DIR / f"locked_medals_{uid}.json"


def read_locks(uid) -> list:
    f = _locks_file(uid)
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def write_locks(uid, ids: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _locks_file(uid).write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")


def set_lock(uid, target_id, locked: bool) -> list:
    """设置单个灯牌锁定状态，返回最新锁定列表。"""
    locks = set(str(x) for x in read_locks(uid))
    target_id = str(target_id)
    if locked:
        locks.add(target_id)
    else:
        locks.discard(target_id)
    result = sorted(locks)
    write_locks(uid, result)
    return result
