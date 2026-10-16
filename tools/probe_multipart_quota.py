# -*- coding: utf-8 -*-
"""试探 B：播放计数维度是「稿件(aid)」还是「分P(cid)」。

背景
----
booster 对单稿件打约 200 次匿名 click/web/h5 后播放量就停住。如果 B站 的配额
其实按 cid（分P）计，那么一个 N 分P 的视频理论上能拿到 N×200 —— 这是不花钱
放大额度的唯一路子。

做法
----
依次对每个分P的 cid 发起 --clicks 次 click/web/h5（每次换一套全新匿名设备 Cookie），
每打完一个分P就打印累计播放量变化，并统计接口返回的真实 code（booster 的 click_once
只看 HTTP 200，不看 body 的 code，这里补上）。

判定
----
- 第 2 个分P 仍能推动播放量上涨 → 按 cid 计数成立，多分P是额度放大器。
- 第 2 个分P 请求 code=0 但播放量不涨 → 接受但不计数，配额按 aid 计（各分P共享）。

用法
----
    python tools/probe_multipart_quota.py BV1xx411c7mD                 # 全部分P，每P 200 次
    python tools/probe_multipart_quota.py BV1xx411c7mD --clicks 100
    python tools/probe_multipart_quota.py BV1xx411c7mD --parts 1,2
    python tools/probe_multipart_quota.py BV1xx411c7mD --direct        # 不走代理池（用本机 IP）
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import httpx
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
_BOOSTER_DIR = str(ROOT / "bili-booster")
if _BOOSTER_DIR not in sys.path:
    sys.path.insert(0, _BOOSTER_DIR)

import booster  # noqa: E402  （复用 fetch_video_info / build_random_cookies / build_click_headers）

_CLICK_URL = "http://api.bilibili.com/x/click-interface/click/web/h5"


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
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10,
        )
        return int(resp.json().get("data", {}).get("stat", {}).get("view", 0))
    except Exception:
        return None


def click_part_detail(proxy, aid, cid, bvid, mid, part_index, vtype, timeout=8):
    """发一次 click/web/h5，返回 (http_status, code, message)。proxy=None 表示直连。

    与 booster.click_once 的差别：这里解析响应 body 的 code，用于区分
    「HTTP 200 但被风控/未计数」和「真正被接受」。"""
    ua = booster._ua_instance.random
    cookies = booster.build_random_cookies()
    kwargs = {
        "headers": booster.build_click_headers(ua, bvid, cookies),
        "timeout": timeout,
        "data": {
            "aid": aid,
            "cid": cid,
            "bvid": bvid,
            "part": str(part_index),
            "mid": mid,
            "jsonp": "jsonp",
            "type": vtype,
            "sub_type": "0",
        },
    }
    if proxy:
        kwargs["proxies"] = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    try:
        resp = requests.post(_CLICK_URL, **kwargs)
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


def run_part(proxies, info, bvid, page_index, clicks, workers, direct):
    """对指定分P并发打 clicks 次点击，返回结果列表 [(http, code, msg), ...]。"""
    aid = info["aid"]
    cid = info["pages"][page_index]["cid"]
    mid = info["owner"]["mid"]
    vtype = info["desc_v2"][0]["type"] if info.get("desc_v2") else "1"

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
            results.append(click_part_detail(proxy, aid, cid, bvid, mid, page_index + 1, vtype))
            time.sleep(random.uniform(0.005, 0.02))

    threads = [threading.Thread(target=worker) for _ in range(min(workers, max(clicks, 1)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def load_proxies(direct):
    if direct:
        return []
    section("拉取公共代理池")
    try:
        proxies = booster.get_total_proxies(print)
    except Exception as e:
        print(f"[!] 代理池拉取失败（{e}），退回直连")
        return []
    print(f"共 {len(proxies)} 条代理")
    return proxies


def main():
    args = parse_args()
    bvid = extract_bvid(args.bvid)
    if not bvid.startswith("BV"):
        print(f"[X] 无法识别 BV 号: {args.bvid!r}")
        return

    section(f"试探 B · 分P(cid) 配额  |  {bvid}")
    try:
        info = booster.fetch_video_info(bvid)
    except Exception as e:
        print(f"[X] 拉取视频信息失败: {e}")
        return

    pages = info.get("pages") or []
    print(f"标题     : {info.get('title')}")
    print(f"aid      : {info.get('aid')}")
    print(f"分P数量  : {len(pages)}")
    for i, pg in enumerate(pages):
        print(f"  P{i + 1}  cid={pg.get('cid')}  {pg.get('part')}")

    # 目标分P
    if args.parts:
        targets = []
        for seg in args.parts.split(","):
            seg = seg.strip()
            if not seg:
                continue
            idx = int(seg) - 1
            if 0 <= idx < len(pages):
                targets.append(idx)
            else:
                print(f"[!] 忽略越界分P: P{seg}")
    else:
        targets = list(range(len(pages)))
    if not targets:
        print("[X] 没有可用的目标分P")
        return
    if len(targets) < 2:
        print("[!] 只测一个分P无法判定「按 cid 计」，建议至少测 2 个分P。")

    proxies = load_proxies(args.direct)
    export = "直连" if args.direct else f"代理池（{len(proxies)} 条）"
    print(f"出口     : {export}")
    print(f"每P点击  : {args.clicks} 次    并发: {args.workers}")

    before = get_view_count(bvid)
    print(f"初始播放量: {before if before is not None else '读取失败'}")

    prev = before
    for idx in targets:
        part_name = pages[idx].get("part")
        section(f"打 P{idx + 1}（cid={pages[idx].get('cid')}  {part_name}）")
        t0 = time.time()
        results = run_part(proxies, info, bvid, idx, args.clicks, args.workers, args.direct)
        http_dist = Counter(r[0] for r in results)
        code_dist = Counter(r[1] for r in results)
        print(f"用时 {time.time() - t0:.1f}s")
        print(f"HTTP 分布 : {dict(http_dist)}")
        print(f"code 分布 : {dict(code_dist)}")
        samples = [r[2] for r in results if r[2]][:3]
        if samples:
            print(f"message 样例: {samples}")

        time.sleep(args.settle)  # 等上报落地
        cur = get_view_count(bvid)
        if cur is None:
            print("[!] 播放量读取失败，跳过本P统计")
            continue
        print(f"播放量    : {prev} -> {cur}  (本P Δ = {cur - prev:+d})")
        if code_dist.get(0, 0) > 0 and cur - prev <= 0:
            print("[!] 接口接受了请求（code=0）但播放量没涨 → 说明是「接受但不计数」，疑似 aid 级配额已满。")
        prev = cur

    section("结论")
    if before is None or prev is None:
        print("[!] 播放量读取失败，无法判定。")
        return
    total_delta = prev - before
    print(f"总变化: {before} -> {prev}  (Δ = {total_delta:+d})")
    if len(targets) >= 2:
        print("若只有 P1 上涨、后续分P 不再上涨 → 计数按 aid（各分P共享配额），多分P无增益。")
        print("若每个分P都能各自推动播放量上涨   → 计数按 cid，多分P是额度放大器。")
    print("注意：B站 播放量统计有延迟，若本轮回显不明显，可等几分钟后手动再读一次播放量确认。")


def parse_args():
    ap = argparse.ArgumentParser(description="试探 B：播放计数维度是 aid 还是 cid")
    ap.add_argument("bvid", help="BV 号或含 BV 号的视频链接")
    ap.add_argument("--clicks", type=int, default=200, help="每个分P的点击次数，默认 200")
    ap.add_argument("--workers", type=int, default=100, help="并发线程数，默认 100")
    ap.add_argument("--parts", default=None, help="要测的分P序号，逗号分隔（如 1,2）；默认测全部分P")
    ap.add_argument("--settle", type=int, default=20, help="每个分P打完后等待统计落地的秒数，默认 20")
    ap.add_argument("--direct", action="store_true", help="不走代理池，用本机 IP 直连")
    return ap.parse_args()


if __name__ == "__main__":
    main()
