# -*- coding: utf-8 -*-
"""验证 buvid 来源是否决定 click/h5 是否真正计数。

假设
----
booster 的 buvid3/buvid4 是本地 md5 伪造的（booster.generate_buvid3/4），
而真实 buvid 由服务端 /x/frontend/finger/spi 签发，并且播放计数可能要求
「服务端签发的 buvid + 一次 playurl 播放会话」。

这就解释了反复出现的现象：click/h5 大量返回 code=0，播放量却零增长。

三组对照（代理互不重叠，排除 IP 额度串扰）
------------------------------------------
  forged      本地伪造 buvid -> click/h5                 （= booster 现状）
  spi         spi 真实 buvid -> click/h5
  spi_playurl spi 真实 buvid -> playurl -> click/h5

判定
----
- forged 组 0，spi/spi_playurl 组出现明显增量 -> buvid/播放会话是缺失环节，可改 booster。
- 三组都 0                                -> buvid 不是原因，匿名通道确实只有两条。
- playurl 返回大量非 0                     -> 播放会话没建立，spi_playurl 组结论无效。

用法
----
    python tools/probe_buvid.py BV1xxxxxxxxx
    python tools/probe_buvid.py BV1xxxxxxxxx --per-group 120 --poll-interval 30 --stable-rounds 3
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
_BOOSTER_DIR = str(ROOT / 'bili-booster')
if _BOOSTER_DIR not in sys.path:
    sys.path.insert(0, _BOOSTER_DIR)

import booster  # noqa: E402

SPI_URL = 'https://api.bilibili.com/x/frontend/finger/spi'
PLAYURL_URL = 'https://api.bilibili.com/x/player/playurl'
CLICK_URL = booster._mobile_click_url
WORKERS = 40


def line(ch='-', n=78):
    print(ch * n)


def extract_bvid(text):
    m = re.search(r'(BV[0-9A-Za-z]{10})', text or '')
    return m.group(1) if m else (text or '').strip()


def proxy_map(proxy):
    return {'http': f'http://{proxy}', 'https': f'http://{proxy}'}


def mobile_headers(bv, cookies):
    return {
        'User-Agent': booster._mobile_ua,
        'Referer': f'https://m.bilibili.com/video/{bv}',
        'Origin': 'https://m.bilibili.com',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Cookie': cookies,
    }


def click_payload(info, bv):
    now = int(time.time())
    return {
        'aid': info['aid'], 'cid': info['cid'], 'bvid': bv, 'part': '1',
        'did': booster.generate_did(), 'mid': '', 'lv': '0',
        'ftime': now, 'stime': now, 'sub_type': '0',
        'spmid': '333.401', 'from_spmid': 'undefined',
        'session': ''.join(random.choices('0123456789abcdef', k=8)),
        'csrf': '',
    }


def fetch_spi(proxy, bv):
    """用该代理取一组服务端签发的 buvid，返回 cookie 字符串或 None。"""
    try:
        r = requests.get(SPI_URL, proxies=proxy_map(proxy),
                         headers={'User-Agent': booster._mobile_ua,
                                  'Referer': f'https://m.bilibili.com/video/{bv}'},
                         timeout=booster.timeout + 3)
        data = (r.json() or {}).get('data') or {}
        b3, b4 = data.get('b_3'), data.get('b_4')
        if not b3 or not b4:
            return None
        return (f'buvid3={b3}; buvid4={b4}; b_nut={int(time.time())}; '
                'i-wanna-go-back=-1; header_theme_version=BMD25032713')
    except Exception:
        return None


def warm_playurl(proxy, info, bv, cookies):
    """建立一次匿名播放会话，返回 body code（None 表示请求异常）。"""
    try:
        r = requests.get(PLAYURL_URL, proxies=proxy_map(proxy),
                         headers=mobile_headers(bv, cookies),
                         params={'bvid': bv, 'cid': info['cid'], 'qn': 64,
                                 'fnval': 0, 'fourk': 0},
                         timeout=booster.timeout + 3)
        try:
            return r.json().get('code')
        except Exception:
            return None
    except Exception:
        return None


def click_once(proxy, info, bv, cookies):
    """发一次 click/h5，返回 (http_status, body_code)。"""
    try:
        r = requests.post(CLICK_URL, proxies=proxy_map(proxy),
                          headers=mobile_headers(bv, cookies),
                          timeout=booster.timeout + 3,
                          data=click_payload(info, bv))
        try:
            return r.status_code, r.json().get('code')
        except Exception:
            return r.status_code, None
    except Exception:
        return None, None


def probe_alive(proxies, bv):
    cache = {}
    alive = []
    lock = threading.Lock()

    def worker(p):
        ok, _ = booster.probe_cached(p, bv, cache)
        if ok:
            with lock:
                alive.append(p)

    with ThreadPoolExecutor(max_workers=max(1, min(booster.thread_num, len(proxies) or 1))) as pool:
        list(pool.map(worker, proxies))
    return alive


def run_group(kind, proxies, info, bv):
    """跑一组，返回 (click code 统计, spi 失败数, playurl code 统计)。"""
    codes = Counter()
    play_codes = Counter()
    spi_fail = [0]
    lock = threading.Lock()

    def worker(proxy):
        if kind == 'forged':
            cookies = booster.build_random_cookies()
        else:
            cookies = fetch_spi(proxy, bv)
            if not cookies:
                with lock:
                    spi_fail[0] += 1
                return
            if kind == 'spi_playurl':
                pc = warm_playurl(proxy, info, bv, cookies)
                with lock:
                    play_codes[f'code={pc}' if pc is not None else 'exc'] += 1

        http, code = click_once(proxy, info, bv, cookies)
        with lock:
            if code is None:
                codes[f'http{http}' if http else 'exc'] += 1
            else:
                codes[f'code={code}'] += 1

    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(proxies)))) as pool:
        list(pool.map(worker, proxies))
    return codes, spi_fail[0], play_codes


def read_views(bv):
    return booster.fetch_video_info(bv)['stat']['view']


def read_until_stable(bv, poll_interval, stable_rounds, max_wait):
    timeline = []
    last = read_views(bv)
    start = time.time()
    unchanged = 0
    while True:
        time.sleep(poll_interval)
        cur = read_views(bv)
        elapsed = int(time.time() - start)
        mark = '' if cur == last else f'  (+{cur - last})'
        timeline.append(f'{elapsed}s={cur}{mark}')
        if cur == last:
            unchanged += 1
            if unchanged >= stable_rounds:
                break
        else:
            unchanged = 0
        last = cur
        if time.time() - start > max_wait:
            timeline.append(f'[达到 {max_wait}s 上限]')
            break
    return last, timeline


def main():
    ap = argparse.ArgumentParser(description='buvid 来源 / 播放会话对 click/h5 计数的影响')
    ap.add_argument('bvid')
    ap.add_argument('--groups', default='forged,spi,spi_playurl')
    ap.add_argument('--per-group', type=int, default=100)
    ap.add_argument('--poll-interval', type=int, default=30)
    ap.add_argument('--stable-rounds', type=int, default=3)
    ap.add_argument('--max-wait', type=int, default=420)
    args = ap.parse_args()

    bv = extract_bvid(args.bvid)
    if not bv.startswith('BV'):
        print(f'[X] 无法识别 BV 号: {args.bvid!r}')
        return

    groups = [g.strip() for g in args.groups.split(',') if g.strip()]
    known = {'forged', 'spi', 'spi_playurl'}
    if any(g not in known for g in groups):
        print(f'[X] 未知组，可选: {sorted(known)}')
        return

    info = booster.fetch_video_info(bv)
    print(f'稿件: {info.get("title")}')
    print(f'  aid={info["aid"]} cid={info["cid"]} duration={info.get("duration")}s')
    print(f'  起始播放量: {info["stat"]["view"]}')

    line('=')
    print('[0] 背景漂移（不发请求，只轮询）')
    line('=')
    d0 = read_views(bv)
    d_stable, d_timeline = read_until_stable(bv, args.poll_interval, args.stable_rounds, args.max_wait)
    drift = d_stable - d0
    print(f'  时间线: {" ".join(d_timeline)}')
    print(f'  自然增量: {drift:+d}')

    line('=')
    print('拉取代理池并探活...')
    line('=')
    proxies = booster.get_total_proxies(print)
    if not proxies:
        print('[X] 代理池为空')
        return
    alive = probe_alive(proxies, bv)
    print(f'  存活代理 {len(alive)}/{len(proxies)}')

    need = args.per_group * len(groups)
    if len(alive) < need:
        per = max(1, len(alive) // len(groups))
        print(f'[!] 存活不足 {need}，每组下调为 {per}')
        args.per_group = per
    if args.per_group < 20:
        print('[X] 每组可用代理 < 20，样本不足')
        return

    random.shuffle(alive)
    slices = [(g, alive[i * args.per_group:(i + 1) * args.per_group])
              for i, g in enumerate(groups)]
    print(f'  切成 {len(groups)} 份互不重叠，每组 {args.per_group} 个')

    results = []
    for name, group in slices:
        line('=')
        print(f'组 [{name}]  {len(group)} 个代理')
        line('=')
        before = read_views(bv)
        start = time.time()
        codes, spi_fail, play_codes = run_group(name, group, info, bv)
        print(f'  耗时 {int(time.time() - start)}s')
        if name != 'forged':
            print(f'  spi 取 buvid 失败: {spi_fail}')
        if name == 'spi_playurl':
            print(f'  playurl 返回: {dict(play_codes)}')
        print(f'  click 返回: {dict(codes)}')
        after, timeline = read_until_stable(bv, args.poll_interval, args.stable_rounds, args.max_wait)
        print(f'  时间线: {" ".join(timeline)}')
        print(f'  播放量 {before} -> {after}  (本组增量 {after - before:+d})')
        results.append((name, len(group), dict(codes), after - before))

    line('=')
    print(f'汇总（背景漂移 {drift:+d}）')
    line('=')
    for name, n, codes, delta in results:
        print(f'  {name:<12} 请求 {n:<4} 增量 {delta:+d}  净增量 {delta - drift:+d}  {codes}')


if __name__ == '__main__':
    main()
