# -*- coding: utf-8 -*-
"""试探 C：纯 HTTP 复刻移动端 click/h5，看「booster + 代理 + 移动端入口」能加多少播放。

背景
----
浏览器实测（probe_mobile_playback.py）：移动端 H5 匿名会话每开一个全新设备就 +1 播放，
上报走的是 POST /x/click-interface/click/h5（**不是** booster 的 click/web/h5），
payload 带 did=<8位设备号>、lv=0、ftime/stime、spmid=333.401。

对照实测（同一稿件、同一代理池、同一时段）：
  click/web/h5（booster 原入口） 7 次 code=0 → 播放量 +0
  click/h5（移动端）            7 次 code=0 → 播放量 +7
=> 移动端入口是独立于网页端的一份额度；网页端那份额度被 booster 用完后移动端照样能加。

booster 式多轮
----
--rounds N 时完全照 booster 的「拉代理池 → 探测存活 → 点击」流程，只是端点换成移动端：
  每轮：get_total_proxies() → probe_once 筛选存活代理 → 每个存活 IP 打一次移动端 click/h5
不设播放上限，逐轮打印播放量增量，最后给累计结果。

判定
----
- code=0 占比高 且 播放量上涨  → 移动端纯 HTTP 可计数。
- code=0 但播放量不涨          → 接受但不计数（IP 被判重 / 该入口额度也满了）。
- code=-352/-412 占比高        → 直接风控拦截，换代理再看。

用法
----
    python tools/probe_mobile_click_http.py BV1smJ26xEXP --rounds 5            # booster 式 5 轮
    python tools/probe_mobile_click_http.py BV1smJ26xEXP --rounds 5 --batch 800 --filter-workers 200
    python tools/probe_mobile_click_http.py BV19SJ16NEwS --clicks 30 --direct   # 直连单轮小样
"""
from __future__ import annotations

import argparse
import random
import re
import string
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
_BOOSTER_DIR = str(ROOT / "bili-booster")
if _BOOSTER_DIR not in sys.path:
    sys.path.insert(0, _BOOSTER_DIR)

import booster  # noqa: E402  （复用 fetch_video_info / build_random_cookies / get_total_proxies / probe_once）

# 移动端 iPhone Safari UA（与 probe_mobile_playback 一致）
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
              "Mobile/15E148 Safari/604.1")
# 桌面 Chrome UA（网页端 click/web/h5）
_DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# 云视听小电视（TV 端）UA
_TV_UA = ("Mozilla/5.0 (Linux; Android 9; SHIELD Android TV Build/PPR1.180610.011) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/80.0.3987.99 "
          "Mobile Safari/537.36 BiliApp/1.0.0 os/tv")

_NOW_URL = "http://api.bilibili.com/x/report/click/now"

# 候选「端」：URL + 请求头画像（UA/Referer/Origin）+ spmid。
# 已实测：mobile 与 web 是两份独立额度；tv 是「换域名」候选（优先级最高）。
ENDPOINTS = {
    "mobile": dict(
        url="http://api.bilibili.com/x/click-interface/click/h5",
        ua=_MOBILE_UA, referer="https://m.bilibili.com/video/{bvid}",
        origin="https://m.bilibili.com", spmid="333.401",
        note="移动端 click/h5（已知：独立 ~200 额度）"),
    "web": dict(
        url="http://api.bilibili.com/x/click-interface/click/web/h5",
        ua=_DESKTOP_UA, referer="https://www.bilibili.com/video/{bvid}",
        origin="https://www.bilibili.com", spmid="333.788",
        note="网页端 click/web/h5（booster 原入口，已知 ~200 上限）"),
    "tv": dict(
        url="https://api.snm0516.aisee.tv/x/click-interface/click/h5",
        ua=_TV_UA, referer="https://www.bilibili.com/video/{bvid}",
        origin="https://www.bilibili.com", spmid="333.1007",
        note="云视听小电视 TV 域名，复用 click/h5 —— 换域名是否独立额度"),
    "tv_webua": dict(
        url="https://api.snm0516.aisee.tv/x/click-interface/click/h5",
        ua=_DESKTOP_UA, referer="https://www.bilibili.com/video/{bvid}",
        origin="https://www.bilibili.com", spmid="333.788",
        note="TV 域名 + 网页 UA（若 tv 失败，验证是否只是 UA 问题）"),
    "report_h5": dict(
        url="http://api.bilibili.com/x/report/click/h5",
        ua=_MOBILE_UA, referer="https://m.bilibili.com/video/{bvid}",
        origin="https://m.bilibili.com", spmid="333.401",
        note="老版上报口 report/click/h5（探测中 code=0）"),
    "report_web_h5": dict(
        url="http://api.bilibili.com/x/report/click/web/h5",
        ua=_MOBILE_UA, referer="https://m.bilibili.com/video/{bvid}",
        origin="https://m.bilibili.com", spmid="333.401",
        note="老版上报口 report/click/web/h5（探测中 code=0）"),
    "ios": dict(
        url="http://api.bilibili.com/x/click-interface/click/ios",
        ua=_MOBILE_UA, referer="https://m.bilibili.com/video/{bvid}",
        origin="https://m.bilibili.com", spmid="333.401",
        note="App 端 click/ios（探测中 14001，需 appkey+sign）"),
    "android": dict(
        url="http://api.bilibili.com/x/click-interface/click/android",
        ua=_MOBILE_UA, referer="https://m.bilibili.com/video/{bvid}",
        origin="https://m.bilibili.com", spmid="333.401",
        note="App 端 click/android（探测中 14001，需 appkey+sign）"),
}


def line(ch="-", n=64):
    print(ch * n)


def section(title):
    print()
    line("=")
    print(title)
    line("=")


def extract_bvid(text):
    m = re.search(r"(BV[0-9A-Za-z]{10})", text or "")
    return m.group(1) if m else (text or "").strip()


def get_view_count(bvid):
    """读播放量；测量失败返回 None（与 0 区分）。"""
    try:
        resp = httpx.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers={"User-Agent": _MOBILE_UA},
            timeout=10,
        )
        return int(resp.json().get("data", {}).get("stat", {}).get("view", 0))
    except Exception:
        return None


def random_did():
    """移动端设备号 did：8 位小写字母+数字（实测形如 elrglb04）。"""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def req_headers(ep, bvid, cookies):
    return {
        "User-Agent": ep["ua"],
        "Referer": ep["referer"].format(bvid=bvid),
        "Origin": ep["origin"],
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookies,
    }


def click_mobile_detail(proxy, info, bvid, ep, timeout=8, use_now=True):
    """发一次 click，返回 (http_status, code, message)。proxy=None 表示直连。

    ep 为 ENDPOINTS 中的一项（决定 URL / UA / Referer / Origin / spmid）。
    """
    aid = info["aid"]
    cid = info["cid"]
    cookies = booster.build_random_cookies()   # 每次全新 buvid3/buvid4
    did = random_did()
    proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"} if proxy else None

    now = int(time.time())
    if use_now:
        try:
            r = requests.get(_NOW_URL, headers=req_headers(ep, bvid, cookies),
                             proxies=proxies, timeout=timeout)
            now = int(r.json().get("data", {}).get("now", now))
        except Exception:
            pass

    data = {
        "aid": aid,
        "cid": cid,
        "bvid": bvid,
        "part": "1",
        "did": did,
        "mid": "",
        "lv": "0",
        "ftime": now,
        "stime": now,
        "sub_type": "0",
        "spmid": ep["spmid"],
        "from_spmid": "undefined",
        "session": "".join(random.choices(string.hexdigits.lower()[:16], k=8)),
        "csrf": "",
    }
    try:
        resp = requests.post(ep["url"], headers=req_headers(ep, bvid, cookies),
                             data=data, proxies=proxies, timeout=timeout)
    except Exception as e:
        return None, None, str(e)[:60]
    code, msg = None, ""
    try:
        j = resp.json()
        code = j.get("code")
        msg = str(j.get("message", ""))
    except Exception:
        pass
    return resp.status_code, code, msg


def filter_alive(proxies, bvid, workers=200):
    """用 booster.probe_once 探测代理存活，返回存活列表（booster 的筛选阶段）。"""
    alive = []
    lock = threading.Lock()
    cursor = [0]
    total = len(proxies)

    def worker():
        while True:
            with lock:
                i = cursor[0]
                cursor[0] += 1
            if i >= total:
                return
            if booster.probe_once(proxies[i], bvid):
                with lock:
                    alive.append(proxies[i])

    threads = [threading.Thread(target=worker) for _ in range(min(workers, max(total, 1)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return alive


def run_clicks(proxies, info, bvid, ep, clicks, workers, direct):
    """并发打 clicks 次 click，返回 [(http, code, msg), ...]。

    代理模式下 clicks == len(proxies)，即「每个存活 IP 只打一次」。
    """
    results = []
    lock = threading.Lock()
    cursor = [0]
    proxy_list = proxies or []

    def worker():
        while True:
            with lock:
                i = cursor[0]
                cursor[0] += 1
            if i >= clicks:
                return
            proxy = None if direct else (proxy_list[i % len(proxy_list)] if proxy_list else None)
            r = click_mobile_detail(proxy, info, bvid, ep)
            with lock:
                results.append(r)
            time.sleep(random.uniform(0.005, 0.02))

    threads = [threading.Thread(target=worker) for _ in range(min(workers, max(clicks, 1)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def fetch_checker_offset(log, offset):
    """拉取 checkerproxy「offset 天前」那一天的代理列表（offset=0 即今天）。

    用于 fresh-IP 对照实验：booster 默认只取「最近一个 >=100 条的日子」（也就是今天），
    所以每轮拉池拿到的都是同一批 IP。改为取更早的归档日，可得到一批全新出口 IP。
    """
    from datetime import date, timedelta
    day = date.today() - timedelta(days=offset)
    url = f'https://checkerproxy.net/v1/landing/archive/{day.strftime("%Y-%m-%d")}'
    print(f'getting proxies from {url} ...')
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        pl = (r.json().get("data") or {}).get("proxyList")
        if isinstance(pl, list):
            lst = pl
        elif isinstance(pl, dict):
            lst = [v for v in pl.values() if v]
        else:
            lst = []
        print(f'successfully get {len(lst)} proxies from checkerproxy[{day}]')
        return lst
    except Exception as e:
        print(f'checkerproxy[{day}] failed: {e}')
        return []


def load_proxies(fresh_days=0, fresh_offset=0):
    """拉代理池。

    fresh_offset>0 : 只用 checkerproxy「前 fresh_offset 天」那一天（单日，用于给不同端分配互不重叠的 IP）。
    fresh_days>0   : 聚合前 1..fresh_days 天。
    否则           : booster 默认池（今天）。
    """
    if fresh_offset > 0:
        print(f"拉取 fresh 代理池（checkerproxy 前 {fresh_offset} 天，单日归档）...")
        proxies = sorted(set(fetch_checker_offset(print, fresh_offset)))
        print(f"共 {len(proxies)} 条 fresh 代理")
        return proxies
    if fresh_days > 0:
        print(f"拉取 fresh 代理池（checkerproxy 前 {fresh_days} 天归档）...")
        seen = set()
        for off in range(1, fresh_days + 1):
            for p in fetch_checker_offset(print, off):
                seen.add(p)
        proxies = list(seen)
        print(f"共 {len(proxies)} 条 fresh 代理")
        return proxies
    print("拉取公共代理池 ...")
    try:
        proxies = booster.get_total_proxies(print)
    except Exception as e:
        print(f"[!] 代理池拉取失败（{e}）")
        return []
    print(f"共 {len(proxies)} 条代理")
    return proxies


def main():
    args = parse_args()
    bvid = extract_bvid(args.bvid)
    if not bvid.startswith("BV"):
        print(f"[X] 无法识别 BV 号: {args.bvid!r}")
        return

    ep = dict(ENDPOINTS[args.endpoint])
    if args.ua:
        ep["ua"] = args.ua

    section(f"端探测 · {args.endpoint}  |  {bvid}")
    try:
        info = booster.fetch_video_info(bvid)
    except Exception as e:
        print(f"[X] 拉取视频信息失败: {e}")
        return
    print(f"标题     : {info.get('title')}")
    print(f"aid/cid  : {info.get('aid')} / {info.get('cid')}")
    print(f"端点     : {ep['url']}")
    print(f"端说明   : {ep['note']}")
    mode = "直连" if args.direct else (
        f"fresh 代理池（checkerproxy 前 {args.fresh_offset} 天，单日）"
        if args.fresh_offset > 0 else
        (f"fresh 代理池（checkerproxy 前 {args.fresh_days} 天归档，全新 IP）"
         if args.fresh_days > 0 else "代理池（每轮重新拉取 + 探测存活）"))
    print(f"出口     : {mode}")
    print(f"轮次     : {args.rounds}"
          + (f"    每轮直连点击: {args.clicks}" if args.direct else
             f"    每轮候选代理上限: {args.batch or '不限'}"))

    before = get_view_count(bvid)
    print(f"初始播放量: {before if before is not None else '读取失败'}")
    prev = before
    rounds_log = []

    for r in range(1, args.rounds + 1):
        section(f"第 {r}/{args.rounds} 轮")

        if args.direct:
            proxies = []
            clicks = args.clicks
        else:
            proxies = load_proxies(args.fresh_days, args.fresh_offset)
            if not proxies:
                print("[!] 本轮无代理可用，跳过")
                continue
            random.shuffle(proxies)
            if args.batch > 0:
                proxies = proxies[:args.batch]
            t0 = time.time()
            alive = filter_alive(proxies, bvid, workers=args.filter_workers)
            print(f"筛选: {len(proxies)} 条候选 -> {len(alive)} 条存活（耗时 {time.time() - t0:.0f}s）")
            proxies = alive
            clicks = len(alive)

        if clicks == 0:
            print("[!] 本轮可用出口为 0，跳过")
            continue

        t0 = time.time()
        results = run_clicks(proxies, info, bvid, ep, clicks, args.workers, args.direct)
        http_dist = Counter(x[0] for x in results)
        code_dist = Counter(x[1] for x in results)
        ok = code_dist.get(0, 0)
        print(f"打点: {clicks} 次，用时 {time.time() - t0:.0f}s")
        print(f"HTTP 分布 : {dict(http_dist)}")
        print(f"code 分布 : {dict(code_dist)}")
        samples = [x[2] for x in results if x[2]][:3]
        if samples:
            print(f"message 样例: {samples}")

        if args.settle > 0:
            print(f"等待统计落地 {args.settle}s ...")
            time.sleep(args.settle)
        cur = get_view_count(bvid)
        if cur is None:
            print(f"第 {r} 轮: 播放量读取失败（code=0 命中 {ok} 次）")
            continue
        delta = cur - prev
        print(f"第 {r} 轮: {prev} -> {cur}  (Δ={delta:+d})  code=0 命中 {ok}/{clicks}")
        rounds_log.append((r, clicks, ok, delta))
        prev = cur

    section("总结")
    after = prev
    if before is None or after is None:
        print("[!] 播放量读取失败，无法判定。")
        return
    print(f"播放量总变化: {before} -> {after}  (Δ = {after - before:+d})")
    print(f"{'轮次':<6}{'出口数':<8}{'code=0':<8}{'本轮Δ':<8}")
    for r, clicks, ok, delta in rounds_log:
        print(f"{r:<6}{clicks:<8}{ok:<8}{delta:+d}")
    total_ok = sum(x[2] for x in rounds_log)
    total_delta = after - before
    if total_delta > 0:
        print(f"[√] 移动端入口累计新增 {total_delta} 播放（code=0 共 {total_ok} 次）。")
        print("    注意：能加多少 ≈ 有多少个「全新且存活」的出口 IP；同一 IP 一般只计 1 次。")
    else:
        print("[×] 播放量没涨。看 code 分布：-352=风控 / -412=IP 拦截 / 0=接受但不计数（IP 被判重或额度满）。")


def parse_args():
    ap = argparse.ArgumentParser(description="试探 C：纯 HTTP 复刻移动端 click/h5")
    ap.add_argument("bvid", help="BV 号或含 BV 号的视频链接")
    ap.add_argument("--rounds", type=int, default=1, help="轮数（booster 式多轮），默认 1")
    ap.add_argument("--clicks", type=int, default=30, help="直连模式下每轮点击次数，默认 30")
    ap.add_argument("--workers", type=int, default=40, help="点击并发线程数，默认 40")
    ap.add_argument("--filter-workers", type=int, default=200, help="代理存活探测并发线程数，默认 200")
    ap.add_argument("--batch", type=int, default=0, help="每轮最多参与筛选的候选代理数，0=不限")
    ap.add_argument("--settle", type=int, default=20, help="每轮打完后等统计落地的秒数，默认 20")
    ap.add_argument("--direct", action="store_true", help="不走代理池，用本机 IP 直连")
    ap.add_argument("--endpoint", choices=sorted(ENDPOINTS), default="mobile",
                    help="要打的「端」（URL+UA+Referer+spmid 预设），默认 mobile")
    ap.add_argument("--ua", default="", help="覆盖端预设的 User-Agent")
    ap.add_argument("--fresh-days", type=int, default=0,
                    help="改用 checkerproxy 前 N 天归档作为代理源（全新 IP 对照，绕开重复池）")
    ap.add_argument("--fresh-offset", type=int, default=0,
                    help="只用 checkerproxy 前 N 天那一天作代理源（单日，给不同端分配互不重叠的 IP）")
    return ap.parse_args()


if __name__ == "__main__":
    main()
