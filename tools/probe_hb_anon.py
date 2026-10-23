# -*- coding: utf-8 -*-
"""匿名心跳计数实验：验证「同 buvid 会话内 click + heartbeat」是否产生独立播放。

设计（三组用互不重叠的代理切片，规模相同，可横向比较）：
  click     每个代理 1 次 click/web/h5                  （基准：1 次上报）
  hb_session每个代理 同 buvid 先 click 再 3 次 heartbeat （若心跳计数，增量应接近 4 倍）
  hb_only   每个代理 仅 3 次 heartbeat（不前置 click）    （若 >0，说明心跳脱离 click 独立计数）

判定：
  hb_session 增量 ≈ click 增量            -> 心跳不计数
  hb_session 增量显著大于 click（趋近 4x） -> 心跳独立计数
  hb_only 出现正增量                        -> 心跳可脱离 click 单独计数

用法：
  python tools/probe_hb_anon.py BV1xxxxxxx --per-group 120 --poll-interval 60 --stable-rounds 4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import threading
import time
import uuid
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

HEARTBEAT_URL = 'https://api.bilibili.com/x/click-interface/web/heartbeat'
REPORT_WEB_URL = 'https://api.bilibili.com/x/report/web/heartbeat'
_CLICK_URL = 'http://api.bilibili.com/x/click-interface/click/web/h5'
_DESKTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
              5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
              24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
              6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
WORKERS = 50


def line(ch='-', n=78):
    print(ch * n)


def wbi_key():
    d = requests.get('https://api.bilibili.com/x/web-interface/nav',
                     headers={'User-Agent': _DESKTOP_UA}, timeout=10).json()
    img = d['data']['wbi_img']['img_url'].rsplit('/', 1)[-1].split('.')[0]
    sub = d['data']['wbi_img']['sub_url'].rsplit('/', 1)[-1].split('.')[0]
    raw = img + sub
    return ''.join(raw[i] for i in _MIXIN_TAB)[:32]


def sign_url_params(base, key):
    params = dict(base)
    params['wts'] = str(int(time.time()))
    query = '&'.join(f'{k}={params[k]}' for k in sorted(params))
    params['w_rid'] = hashlib.md5((query + key).encode()).hexdigest()
    return params


def make_cookie(buvid):
    return (f'buvid3={buvid}; buvid4={int(time.time() * 1000)}-{uuid.uuid4().hex}; '
            f'b_nut={int(time.time())}; i-wanna-go-back=-1; header_theme_version=BMD25032713')


def new_buvid():
    return booster.generate_buvid3()


def hb_body(info, bv, session, played, play_type):
    dur = int(info.get('duration') or 60)
    now = int(time.time())
    return {
        'aid': info['aid'], 'bvid': bv, 'cid': info['cid'], 'mid': '',
        'played_time': str(min(played, dur - 1)),
        'realtime': str(min(played, dur)),
        'real_played_time': str(min(played, dur)),
        'start_ts': str(now - played), 'type': '3', 'sub_type': '0', 'dt': '2',
        'outer': '0', 'play_type': str(play_type), 'quality': '80',
        'video_duration': str(dur),
        'last_play_progress_time': str(min(played, dur - 1)),
        'max_play_progress_time': str(min(played, dur - 1)),
        'session': session, 'spmid': '333.788.0.0', 'from_spmid': '333.999.0.0',
        'refer_url': f'https://www.bilibili.com/video/{bv}/',
        'extra': json.dumps({'player_version': '4.8.36'}),
        'csrf': '',
    }


def hb_url_params(info, bv, played):
    dur = int(info.get('duration') or 60)
    return {
        'w_start_ts': str(int(time.time()) - played), 'w_mid': '', 'w_aid': str(info['aid']),
        'w_dt': '2', 'w_realtime': str(min(played, dur)),
        'w_playedtime': str(min(played, dur - 1)),
        'w_real_played_time': str(min(played, dur)),
        'w_video_duration': str(dur),
        'w_last_play_progress_time': str(min(played, dur - 1)),
        'web_location': '1315873',
    }


def click(proxy, info, bv, buvid):
    try:
        r = requests.post(_CLICK_URL,
                          proxies={'http': f'http://{proxy}'},
                          headers=booster.build_click_headers(_DESKTOP_UA, bv, make_cookie(buvid)),
                          timeout=booster.timeout,
                          data={'aid': info['aid'], 'cid': info['cid'], 'bvid': bv,
                                'part': '1', 'mid': '', 'jsonp': 'jsonp',
                                'type': '3', 'sub_type': '0'})
        return r.json().get('code')
    except Exception:
        return None


def heartbeat(proxy, info, bv, buvid, session, played, play_type, key):
    try:
        params = sign_url_params(hb_url_params(info, bv, played), key)
        r = requests.post(HEARTBEAT_URL, params=params,
                          proxies={'http': f'http://{proxy}', 'https': f'http://{proxy}'},
                          headers=booster.build_click_headers(_DESKTOP_UA, bv, make_cookie(buvid)),
                          timeout=booster.timeout + 2,
                          data=hb_body(info, bv, session, played, play_type))
        return r.json().get('code')
    except Exception:
        return None


def report_web(proxy, info, bv, buvid, session, played, key):
    try:
        params = sign_url_params(hb_url_params(info, bv, played), key)
        r = requests.post(REPORT_WEB_URL, params=params,
                          proxies={'http': f'http://{proxy}', 'https': f'http://{proxy}'},
                          headers=booster.build_click_headers(_DESKTOP_UA, bv, make_cookie(buvid)),
                          timeout=booster.timeout + 2,
                          data=hb_body(info, bv, session, played, 0))
        return r.json().get('code')
    except Exception:
        return None


def run_group(kind, proxies, info, bv, key):
    codes = Counter()
    lock = threading.Lock()

    def note(v):
        with lock:
            codes[f'code={v}' if v is not None else 'exc'] += 1

    def worker(proxy):
        buvid = new_buvid()
        session = uuid.uuid4().hex
        if kind == 'click':
            note(click(proxy, info, bv, buvid))
        elif kind == 'hb_only':
            for played, pt in ((15, 1), (30, 0), (60, 0)):
                note(heartbeat(proxy, info, bv, buvid, session, played, pt, key))
        elif kind == 'hb_session':
            note(click(proxy, info, bv, buvid))
            for played, pt in ((15, 1), (30, 0), (60, 0)):
                note(heartbeat(proxy, info, bv, buvid, session, played, pt, key))
        elif kind == 'report_web':
            for played in (15, 30, 60):
                note(report_web(proxy, info, bv, buvid, session, played, key))

    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(proxies)))) as pool:
        list(pool.map(worker, proxies))
    return dict(codes)


def views(bv):
    return booster.fetch_video_info(bv)['stat']['view']


def main():
    ap = argparse.ArgumentParser(description='匿名心跳计数实验')
    ap.add_argument('bvid')
    ap.add_argument('--groups', default='click,hb_session,hb_only,report_web')
    ap.add_argument('--per-group', type=int, default=120)
    ap.add_argument('--drift', type=int, default=120, help='开场背景漂移观察秒数')
    ap.add_argument('--poll-interval', type=int, default=60)
    ap.add_argument('--stable-rounds', type=int, default=4)
    ap.add_argument('--max-wait', type=int, default=1800)
    args = ap.parse_args()

    bv = args.bvid.strip()
    info = booster.fetch_video_info(bv)
    print(f'稿件 {info.get("title")}  aid={info["aid"]} cid={info["cid"]} '
          f'dur={info.get("duration")}s  views={info["stat"]["view"]}')

    base = views(bv)
    print(f'背景漂移观察 {args.drift}s ...')
    time.sleep(args.drift)
    drift = views(bv) - base
    print(f'背景漂移 {drift:+d} (base={base})')

    key = wbi_key()
    print(f'wbi key {key[:8]}...')

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

    groups = [g.strip() for g in args.groups.split(',') if g.strip()]
    need = args.per_group * len(groups)
    if len(alive) < need:
        args.per_group = max(1, len(alive) // len(groups))
        print(f'存活不足，per-group 降为 {args.per_group}')
    slices = [alive[i * args.per_group:(i + 1) * args.per_group] for i in range(len(groups))]

    results = []
    for kind, ps in zip(groups, slices):
        line('=')
        print(f'[{kind}] {len(ps)} 个代理')
        before = views(bv)
        t0 = time.time()
        codes = run_group(kind, ps, info, bv, key)
        print(f'  返回 {codes}  耗时 {int(time.time() - t0)}s')
        results.append([kind, len(ps), codes, before])

    line('=')
    print(f'发送完毕，开始轮询播放量（间隔 {args.poll_interval}s，连续 '
          f'{args.stable_rounds} 次无变化即停，上限 {args.max_wait}s）')
    start = time.time()
    last, stable, rounds = None, 0, []
    while time.time() - start < args.max_wait:
        cur = views(bv)
        rounds.append(cur)
        delta = cur - base
        print(f'  t+{int(time.time() - start):>4}s  views={cur}  delta={delta:+d}')
        if last is not None and cur == last:
            stable += 1
            if stable >= args.stable_rounds:
                break
        else:
            stable = 0
        last = cur
        time.sleep(args.poll_interval)
    final = rounds[-1] if rounds else base

    line('=')
    print('汇总（各组发送前基线 -> 最终播放量）')
    for kind, n, codes, before in results:
        print(f'  {kind:<12} 代理 {n:<4} 发送前 {before}  返回 {codes}')
    print(f'  总增量 {final - base:+d}（含背景漂移 {drift:+d}），净增量 {final - base - drift:+d}')
    print('  判定：hb_session 净增量 ≈ click 净增量 => 心跳不计数；'
          'hb_only 净增量 > 0 => 心跳可脱离 click 独立计数。')


if __name__ == '__main__':
    main()
