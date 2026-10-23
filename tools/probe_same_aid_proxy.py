# -*- coding: utf-8 -*-
"""同一稿件 + 多出口 IP 并发计数实验。

问题：播放计数的去重键是「60s / aid+cid」，还是「60s / aid+cid + 出口 IP」？

设计（同一靶子稿件，两组同刻并发，组间留 >60s 清窗）：
  multi   N 个 worker 各走一个不同代理 IP，同一毫秒发出 click + 心跳
  single  N 个 worker 全部走本机直连（同一出口 IP），同一毫秒发出

判定：
  multi +1、single +1  -> 去重按 aid+cid，换 IP 无效，单稿件天花板 ≈1/min
  multi +N、single +1  -> 去重含 IP，代理池即吞吐上限
  multi 介于 1..N      -> 存在部分归并（IP 段 / ASN / 设备指纹）

用法：
  py -3 tools/probe_same_aid_proxy.py smoke BV1SKH96WEnh
  py -3 tools/probe_same_aid_proxy.py run BV1SKH96WEnh --n 5 --play-s 20 --observe 180
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except ImportError:  # 打包运行时只带 pycryptodomex（模块名 Cryptodome）
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import pad

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
_BOOSTER_DIR = str(ROOT / 'bili-booster')
if _BOOSTER_DIR not in sys.path:
    sys.path.insert(0, _BOOSTER_DIR)

import booster  # noqa: E402

requests.packages.urllib3.disable_warnings()

CAR_CLICK = 'https://api.bilibili.com/x/report/click/android2'
HB_MOBILE = 'https://api.bilibili.com/x/report/heartbeat/mobile'
AES_KEY = b'fd6b639dbcff0c2a1b03b389ec763c4b'
AES_IV = b'77b07a672d57d64c'
SIGN_SALT = '9cafa6466a028bfb'
APPKEY = '1d8b6e7d45233436'
APPSEC = '560c52ccd288fed045859ed18bffd973'
CAR_UA = 'Mozilla/5.0 BiliTV/1.8.9'
TIMEOUT = 12


def line(ch='-', n=72):
    print(ch * n)


def aes_encrypt(plain: bytes) -> bytes:
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(plain, 16))


def car_cookies(buvid, buvid4):
    return f'buvid3={buvid}; buvid4={buvid4}; b_nut={int(time.time())}'


def car_headers(buvid, buvid4):
    return {
        'User-Agent': CAR_UA,
        'Content-Type': 'application/octet-stream',
        'Accept': '*/*',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Buvid': buvid,
        'Cookie': car_cookies(buvid, buvid4),
    }


def proxies_of(proxy):
    if not proxy:
        return None
    return {'http': f'http://{proxy}', 'https': f'http://{proxy}'}


def car_click(info, buvid, buvid4, did, proxy=None):
    """车机 click：AES-256-CBC 加密体，返回 (code, rpdid)。"""
    now = int(time.time())
    p = {
        'aid': str(info['aid']), 'cid': str(info['cid']), 'part': '1',
        'mid': '', 'lv': '0', 'ftime': str(now), 'stime': str(now),
        'did': did, 'type': '1', 'sub_type': '0', 'sid': '0', 'epid': '0',
        'auto_play': '0', 'build': '2100218',
        'mobi_app': 'android_bilithings', 'spmid': 'player.car-player.0.0',
        'device': 'car', 'from_spmid': '0.0.0.0.0',
    }
    query = '&'.join(f'{k}={p[k]}' for k in sorted(p))
    sign = hashlib.sha256((query + SIGN_SALT).encode()).hexdigest()
    body = aes_encrypt((query + '&sign=' + sign).encode())
    try:
        r = requests.post(CAR_CLICK, data=body, headers=car_headers(buvid, buvid4),
                          proxies=proxies_of(proxy), timeout=TIMEOUT, verify=False)
        d = r.json()
        data = d.get('data') or {}
        return d.get('code'), data.get('rpdid')
    except Exception as e:
        return f'exc:{type(e).__name__}', None


def hb_mobile(info, bv, session, buvid, buvid4, played, dur, play_type, proxy=None):
    """APP 心跳：FormUrlEncoded + appkey/ts/sign（md5(排序参数+appsec)）。"""
    now = int(time.time())
    played = max(1, min(int(played), dur - 1)) if dur > 1 else max(1, int(played))
    body = {
        'appkey': APPKEY, 'ts': str(now), 'mobi_app': 'android',
        'platform': 'android', 'build': '7660300',
        'start_ts': str(now - played), 'session': session, 'mid': '',
        'aid': str(info['aid']), 'cid': str(info['cid']), 'sid': '0', 'epid': '0',
        'type': '1', 'sub_type': '0', 'quality': '80',
        'total_time': str(played), 'paused_time': '0', 'played_time': str(played),
        'video_duration': str(dur), 'play_type': play_type, 'network_type': '1',
        'last_play_progress_time': str(played), 'max_play_progress_time': str(played),
        'from': '0', 'from_spmid': '0.0.0.0.0', 'spmid': 'player.car-player.0.0',
        'epid_status': '0', 'play_status': '1', 'user_status': '0',
        'actual_played_time': str(played), 'auto_play': '0',
        'list_play_time': '0', 'miniplayer_play_time': '0',
        'device': 'car',
        'playlist_id': '0', 'playlist_type': '0',
    }
    query = '&'.join(f'{k}={body[k]}' for k in sorted(body))
    body['sign'] = hashlib.md5((query + APPSEC).encode()).hexdigest()
    try:
        r = requests.post(HB_MOBILE, data=body,
                          headers={'User-Agent': 'Mozilla/5.0 BiliDroid/7.66.0 '
                                                '(bbcallen@gmail.com) os/android',
                                   'Cookie': car_cookies(buvid, buvid4),
                                   'Content-Type': 'application/x-www-form-urlencoded'},
                          proxies=proxies_of(proxy), timeout=TIMEOUT, verify=False)
        return r.json().get('code')
    except Exception as e:
        return f'exc:{type(e).__name__}'


def play(info, bv, proxy, play_s, barrier, out, tag):
    """一个独立设备会话：click -> 复点拿 rpdid -> start/mid/end 三次心跳。

    proxy 可以是单个代理、None（直连），或代理列表（失败自动换下一个）。
    """
    cands = proxy if isinstance(proxy, list) else [proxy]
    if not cands:
        cands = [None]
    buvid = booster.generate_buvid3()
    buvid4 = booster.generate_buvid4()
    did = booster.generate_did()
    session = uuid.uuid4().hex
    dur = int(info.get('duration') or 60)
    codes = []

    barrier.wait()  # 同刻起跑
    t0 = time.time()
    code, rpdid, used = None, None, cands[0]
    for p in cands:
        code, rpdid = car_click(info, buvid, buvid4, did, p)
        used = p
        if rpdid:
            break
    codes.append(f'click1={code}')
    if rpdid:
        code2 = None
        for p in [used] + [x for x in cands if x is not used]:
            for _ in range(2):
                code2, _ = car_click(info, buvid, buvid4, rpdid, p)
                if code2 == 0:
                    break
                time.sleep(0.3)
            if code2 == 0:
                used = p
                break
        codes.append(f'click2={code2}')
    hb_proxy = used
    for frac, pt in ((0.0, '1'), (0.5, '0'), (1.0, '0')):
        wait = t0 + play_s * frac - time.time()
        if wait > 0:
            time.sleep(wait)
        hb = hb_mobile(info, bv, session, buvid, buvid4, max(1, play_s * frac), dur, pt, hb_proxy)
        if isinstance(hb, str):
            for p in [x for x in cands if x is not hb_proxy]:
                hb = hb_mobile(info, bv, session, buvid, buvid4, max(1, play_s * frac), dur, pt, p)
                if not isinstance(hb, str):
                    hb_proxy = p
                    break
        codes.append(f'hb{int(frac * 100)}={hb}')
    with threading.Lock():
        out[tag] = codes


def views(bv):
    return booster.fetch_video_info(bv)['stat']['view']


def sample(bv_name, bv, base, seconds, interval=15):
    start = time.time()
    last = None
    while time.time() - start < seconds:
        cur = views(bv)
        print(f'  t+{int(time.time() - start):>4}s  views={cur}  delta={cur - base:+d}')
        last = cur
        time.sleep(interval)
    cur = views(bv)
    print(f'  t+{int(time.time() - start):>4}s  views={cur}  delta={cur - base:+d}')
    return cur


def run_group(name, info, bv, proxies, n, play_s, observe):
    line('=')
    kind = '本机直连' if all(p is None for p in proxies) else '各自代理（失败自动换）'
    print(f'[{name}] {n} 个 worker，出口 = {kind}')
    before = views(bv)
    print(f'  发送前 views={before}')
    barrier = threading.Barrier(n)
    out = {}
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = [pool.submit(play, info, bv, p, play_s, barrier, out, f'w{i}')
                for i, p in enumerate(proxies)]
        for f in futs:
            f.result()
    for k in sorted(out):
        print(f'  {k}: {" ".join(out[k])}')
    after = sample(name, bv, before, observe)
    return after - before


def probe_post(proxy):
    """判定代理能否真正转发 HTTPS POST（空体也回 JSON 即可）。"""
    try:
        r = requests.post(CAR_CLICK, data=b'', headers={'User-Agent': CAR_UA,
                                                        'Content-Type': 'application/octet-stream'},
                          proxies=proxies_of(proxy), timeout=8, verify=False)
        return r.status_code in (200, 400, 405, 412) and r.text.lstrip().startswith('{')
    except Exception:
        return False


def load_alive(n, bv):
    print('拉取代理池 ...')
    pool = booster.get_total_proxies(print)
    if not pool:
        return []
    print(f'共 {len(pool)} 个，HTTP 粗筛 ...')
    cache, alive, lock = {}, [], threading.Lock()

    def one(p):
        ok, _ = booster.probe_cached(p, bv, cache)
        if ok:
            with lock:
                alive.append(p)

    with ThreadPoolExecutor(max_workers=max(1, min(booster.thread_num, len(pool)))) as ex:
        list(ex.map(one, pool))
    print(f'HTTP 存活 {len(alive)}')
    random.shuffle(alive)
    print('HTTPS POST 复筛 ...')
    good = []
    from concurrent.futures import as_completed
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(probe_post, p): p for p in alive[:100]}
        for f in as_completed(futs):
            if f.result():
                good.append(futs[f])
                if len(good) >= n * 2:
                    break
        for f in futs:
            f.cancel()
    print(f'POST 可用 {len(good)}')
    return good


def main():
    ap = argparse.ArgumentParser(description='同一稿件 + 多出口 IP 并发计数实验')
    ap.add_argument('mode', choices=['smoke', 'run', 'burst'])
    ap.add_argument('bvid')
    ap.add_argument('--n', type=int, default=5)
    ap.add_argument('--bursts', type=int, default=4)
    ap.add_argument('--n-list', default='', help='burst 模式多档并发数，逗号分隔，如 10,20')
    ap.add_argument('--play-s', type=int, default=20)
    ap.add_argument('--drift', type=int, default=60, help='基线观察秒数')
    ap.add_argument('--observe', type=int, default=180, help='每组发送后观察秒数')
    ap.add_argument('--gap', type=int, default=70, help='两组之间的清窗秒数')
    ap.add_argument('--mode-run', default='both', choices=['both', 'multi', 'single'],
                    help='run 模式下跑哪些组')
    args = ap.parse_args()

    bv = args.bvid.strip()
    info = booster.fetch_video_info(bv)
    base = views(bv)
    print(f'稿件 {info.get("title")}')
    print(f'aid={info["aid"]} cid={info["cid"]} dur={info.get("duration")}s views={base}')
    print(f'基线观察 {args.drift}s ...')
    drift = sample('drift', bv, base, args.drift)
    print(f'基线漂移 {drift - base:+d}')

    if args.mode == 'smoke':
        line('=')
        print('[smoke] 本机直连跑 1 个会话')
        barrier = threading.Barrier(1)
        out = {}
        play(info, bv, None, args.play_s, barrier, out, 'smoke')
        for k, v in out.items():
            print(f'  {k}: {" ".join(v)}')
        print(f'观察 {args.observe}s ...')
        final = sample('smoke', bv, base, args.observe)
        print(f'净增量 {final - base - (drift - base):+d}')
        return

    if args.mode == 'burst':
        ns = ([int(x) for x in args.n_list.split(',') if x.strip()]
              if args.n_list else [args.n] * args.bursts)
        line('=')
        print(f'[burst] 同 IP 直连，批次 {ns}，每批间隔 {args.gap}s')
        results = []
        for i, n in enumerate(ns):
            before = views(bv)
            barrier = threading.Barrier(n)
            out = {}
            t_send = time.time()
            with ThreadPoolExecutor(max_workers=n) as pool:
                futs = [pool.submit(play, info, bv, None, args.play_s, barrier, out,
                                    f'b{i}w{j}') for j in range(n)]
                for f in futs:
                    f.result()
            time.sleep(args.observe)
            after = views(bv)
            results.append(after - before)
            ok = sum(1 for v in out.values() if 'click2=0' in v)
            print(f'  批次 {i + 1} (N={n}): 成功复点 {ok}/{n}  发送前 {before} -> {after}  '
                  f'增量 {after - before:+d}  耗时 {int(time.time() - t_send)}s')
            if i < len(ns) - 1:
                time.sleep(max(0, args.gap - (time.time() - t_send)))
        print(f'各档 N 增量 {list(zip(ns, results))}  合计 +{sum(results)}')
        return

    alive = load_alive(args.n, bv)
    if args.mode_run in ('both', 'multi') and len(alive) < args.n:
        print(f'[X] 可用代理不足（{len(alive)}/{args.n}），只跑 single 组')
        args.mode_run = 'single'

    results = {}
    if args.mode_run in ('both', 'multi'):
        groups = [alive[i::args.n] or [None] for i in range(args.n)]
        print(f'multi 代理分组：{[len(g) for g in groups]}')
        results['multi'] = run_group('multi', info, bv, groups, args.n,
                                     args.play_s, args.observe)
        if args.mode_run == 'both':
            print(f'清窗 {args.gap}s ...')
            time.sleep(args.gap)
    if args.mode_run in ('both', 'single'):
        results['single'] = run_group('single', info, bv, [None] * args.n, args.n,
                                      args.play_s, args.observe)

    line('=')
    print('汇总（发送后观察窗内增量）')
    for k, v in results.items():
        print(f'  {k:<8} +{v}')
    if 'multi' in results and 'single' in results:
        print('判定：multi/single 接近 1 => 去重按 aid+cid（换 IP 无效）；'
              '接近 N => 去重含 IP（代理即吞吐）。')


if __name__ == '__main__':
    main()
