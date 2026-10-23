# -*- coding: utf-8 -*-
"""播放点击参数矩阵实验：验证 part / type / outer 是否能作为额外计数维度。

每组使用互不重叠的代理切片，每个代理只发 1 次 click/web/h5，组间规模相同：
  base     part=1, type=稿件默认, sub_type=0        （基准，已知可计数）
  part2    part=2（单P稿件，测试服务端是否校验 part）
  type4    type=4
  type10   type=10
  outer1   追加 outer=1（站外播放）
  base2    再次 base（校验额度是否已被前面组耗尽 / 自然流量漂移）

判定：某组增量与 base 同量级 => 该参数组合仍可计数（可作为额外维度）；
      增量为 0 且大量 code!=0 => 服务端拒绝该组合。

用法：
  python tools/probe_click_params.py BV1xxxxxxx --per-group 80 --drift 120
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

VARIANTS = [
    ('base', {}),
    ('part2', {'part': '2'}),
    ('type4', {'type': '4'}),
    ('type10', {'type': '10'}),
    ('outer1', {'outer': '1'}),
    ('base2', {}),
]


def line(ch='-', n=78):
    print(ch * n)


def views(bv):
    return booster.fetch_video_info(bv)['stat']['view']


def click_once(proxy, info, bv, extra):
    try:
        ua = booster._ua_instance.random
        cookies = booster.build_random_cookies()
        data = {
            'aid': info['aid'],
            'cid': info['cid'],
            'bvid': bv,
            'part': '1',
            'mid': info['owner']['mid'],
            'jsonp': 'jsonp',
            'type': info['desc_v2'][0]['type'] if info['desc_v2'] else '1',
            'sub_type': '0',
        }
        data.update(extra)
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


def run_group(proxies, info, bv, extra):
    codes = Counter()
    lock = threading.Lock()

    def worker(proxy):
        res = click_once(proxy, info, bv, extra)
        with lock:
            codes[res] += 1

    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(proxies)))) as pool:
        list(pool.map(worker, proxies))
    return dict(codes)


def main():
    ap = argparse.ArgumentParser(description='播放点击参数矩阵实验')
    ap.add_argument('bvid')
    ap.add_argument('--per-group', type=int, default=80)
    ap.add_argument('--drift', type=int, default=120, help='开场背景漂移观察秒数')
    ap.add_argument('--settle', type=int, default=30, help='每组发送后等待结算秒数')
    ap.add_argument('--poll-interval', type=int, default=60, help='结算轮询间隔秒数')
    ap.add_argument('--stable-rounds', type=int, default=4, help='连续多少次无变化视为结算完成')
    ap.add_argument('--max-wait', type=int, default=600, help='每组结算轮询上限秒数')
    args = ap.parse_args()

    bv = args.bvid.strip()
    info = booster.fetch_video_info(bv)
    default_type = info['desc_v2'][0]['type'] if info['desc_v2'] else '1'
    print(f'稿件 {info.get("title")}  aid={info["aid"]} cid={info["cid"]} '
          f'dur={info.get("duration")}s  views={info["stat"]["view"]}  '
          f'默认type={default_type}  P数={len(info.get("pages") or [])}')

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

    need = args.per_group * len(VARIANTS)
    if len(alive) < need:
        args.per_group = max(1, len(alive) // len(VARIANTS))
        print(f'存活不足，per-group 降为 {args.per_group}')
    slices = [alive[i * args.per_group:(i + 1) * args.per_group]
              for i in range(len(VARIANTS))]

    results = []
    cursor = base
    for (name, extra), ps in zip(VARIANTS, slices):
        line('=')
        print(f'[{name}] {len(ps)} 个代理  extra={extra or "{}"}')
        before = cursor
        t0 = time.time()
        codes = run_group(ps, info, bv, extra)
        print(f'  返回 {codes}  耗时 {int(time.time() - t0)}s')
        time.sleep(args.settle)

        # 轮询到结算平台期，避免延迟结算把上一组的增量算到下一组头上
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
        after = cur
        print(f'  播放量 {before} -> {after}  (增量 {after - before:+d})')
        results.append([name, len(ps), codes, before, after])
        cursor = after

    line('=')
    print('汇总（各组发送前 -> 发送后）')
    for name, n, codes, before, after in results:
        print(f'  {name:<8} 代理 {n:<4} {before} -> {after}  增量 {after - before:+d}  返回 {codes}')
    print(f'  全程增量 {cursor - base:+d}（含背景漂移 {drift:+d}），净增量 {cursor - base - drift:+d}')
    print('  判定：与 base 同量级 => 该参数组合仍可计数；0 且 code!=0 居多 => 被服务端拒绝。')


if __name__ == '__main__':
    main()
