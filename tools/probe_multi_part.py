# -*- coding: utf-8 -*-
"""多P稿件 part/cid 维度实验：验证不同分P是否各自拥有独立计数额度。

在同一多P稿件上依次发送：
  p1  part=1, cid=第1P
  p2  part=2, cid=第2P
  p3  part=3, cid=第3P
  p1b part=1, cid=第1P（复测，判断额度是否按视频共享）

判定：若 p2/p3 在 p1 之后仍能产生增量 => 每个分P有独立额度（第三条通道成立）；
     若 p2/p3 增量为 0 而 p1b 同量级 => 额度按视频共享，part 维度无效。

用法：
  python tools/probe_multi_part.py BV1xxxxxxxxx --per-group 40
"""
from __future__ import annotations

import argparse
import random
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

CLICK_URL = 'http://api.bilibili.com/x/click-interface/click/web/h5'
WORKERS = 50


def line(ch='-', n=78):
    print(ch * n)


def views(bv):
    return booster.fetch_video_info(bv)['stat']['view']


def click_once(proxy, info, bv, part, cid):
    try:
        ua = booster._ua_instance.random
        cookies = booster.build_random_cookies()
        data = {
            'aid': info['aid'],
            'cid': cid,
            'bvid': bv,
            'part': str(part),
            'mid': info['owner']['mid'],
            'jsonp': 'jsonp',
            'type': info['desc_v2'][0]['type'] if info['desc_v2'] else '1',
            'sub_type': '0',
        }
        r = requests.post(CLICK_URL,
                          proxies={'http': f'http://{proxy}'},
                          headers=booster.build_click_headers(ua, bv, cookies),
                          timeout=booster.timeout,
                          data=data)
        try:
            return f'code={r.json().get("code")}'
        except Exception:
            return f'http{r.status_code}'
    except Exception:
        return 'exc'


def run_group(proxies, info, bv, part, cid):
    codes = Counter()
    lock = threading.Lock()

    def worker(proxy):
        res = click_once(proxy, info, bv, part, cid)
        with lock:
            codes[res] += 1

    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(proxies)))) as pool:
        list(pool.map(worker, proxies))
    return dict(codes)


def main():
    ap = argparse.ArgumentParser(description='多P稿件 part/cid 维度实验')
    ap.add_argument('bvid')
    ap.add_argument('--per-group', type=int, default=40)
    ap.add_argument('--drift', type=int, default=120)
    ap.add_argument('--settle', type=int, default=30)
    ap.add_argument('--poll-interval', type=int, default=60)
    ap.add_argument('--stable-rounds', type=int, default=4)
    ap.add_argument('--max-wait', type=int, default=600)
    args = ap.parse_args()

    bv = args.bvid.strip()
    info = booster.fetch_video_info(bv)
    pages = info.get('pages') or []
    if len(pages) < 2:
        print(f'[X] 稿件只有 {len(pages)} 个分P，无法做多P实验')
        return
    print(f'稿件 {info.get("title")}  aid={info["aid"]}  P数={len(pages)}  '
          f'views={info["stat"]["view"]}')
    for p in pages[:5]:
        print(f'  P{p["page"]} cid={p["cid"]} dur={p.get("duration")}s {p.get("part","")[:40]}')

    variants = [(f'p{p["page"]}', p['page'], p['cid']) for p in pages[:3]]
    variants.append(('p1b', 1, pages[0]['cid']))

    base = views(bv)
    print(f'背景漂移观察 {args.drift}s ...')
    time.sleep(args.drift)
    drift = views(bv) - base
    print(f'背景漂移 {drift:+d} (base={base})')

    line('=')
    print('拉取代理池 ...')
    proxies = booster.get_total_proxies(print)
    if not proxies:
        print('[X] 代理池为空')
        return
    print(f'共 {len(proxies)} 个，开始探活 ...')
    cache, alive, lock = {}, [], threading.Lock()

    def probe(p):
        ok, _ = booster.probe_cached(p, bv, cache)
        if ok:
            with lock:
                alive.append(p)

    with ThreadPoolExecutor(max_workers=max(1, min(booster.thread_num, len(proxies)))) as pool:
        list(pool.map(probe, proxies))
    print(f'存活 {len(alive)}/{len(proxies)}')
    random.shuffle(alive)
    if len(alive) < args.per_group * len(variants):
        args.per_group = max(1, len(alive) // len(variants))
        print(f'存活不足，per-group 降为 {args.per_group}')
    slices = [alive[i * args.per_group:(i + 1) * args.per_group]
              for i in range(len(variants))]

    results = []
    cursor = base
    for (name, part, cid), ps in zip(variants, slices):
        line('=')
        print(f'[{name}] part={part} cid={cid}  {len(ps)} 个代理')
        before = cursor
        t0 = time.time()
        codes = run_group(ps, info, bv, part, cid)
        print(f'  返回 {codes}  耗时 {int(time.time() - t0)}s')
        time.sleep(args.settle)

        start = time.time()
        last, stable = None, 0
        cur = before
        while time.time() - start < args.max_wait:
            cur = views(bv)
            print(f'    t+{int(time.time() - start):>4}s  views={cur}  (本组增量 {cur - before:+d})')
            if last is not None and cur == last:
                stable += 1
                if stable >= args.stable_rounds:
                    break
            else:
                stable = 0
            last = cur
            time.sleep(args.poll_interval)
        print(f'  播放量 {before} -> {cur}  (增量 {cur - before:+d})')
        results.append([name, len(ps), codes, before, cur])
        cursor = cur

    line('=')
    print('汇总')
    for name, n, codes, before, after in results:
        print(f'  {name:<5} 代理 {n:<4} {before} -> {after}  增量 {after - before:+d}  返回 {codes}')
    print(f'  全程增量 {cursor - base:+d}（含背景漂移 {drift:+d}），净增量 {cursor - base - drift:+d}')


if __name__ == '__main__':
    main()
