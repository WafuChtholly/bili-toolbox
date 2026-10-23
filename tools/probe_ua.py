# -*- coding: utf-8 -*-
"""只验证「换 UA」这一单个变量对 click/h5（移动 H5 开始观看接口）的影响。

控制变量
--------
只改 User-Agent，其余全部保持与 booster.click_mobile_once 一致：
同一个 URL（click/h5）、同一套 Referer/Origin（m.bilibili.com）、同一份 payload
（did/lv/ftime/stime/spmid=333.401/session/csrf）、同样随机 buvid cookie。
因此组间差异只能归因到 UA（在当前这个请求形态下）。

为什么每组用不同的代理
----------------------
bilibili 的播放量按「IP + 稿件」去重/限额。如果五组共用同一批代理，第一组就把额度
吃光，后面几组必然增量≈0，那不是 UA 的锅。所以这里把存活代理切成互不重叠的 5 份，
每组各自独享一份，组间对比才成立。

观测口径
--------
1) 每组额外记录响应 body 的真实 code（booster 只看 HTTP 200，看不到业务失败）。
   code=0 成功；-352/-412 一般为风控；非 0 说明这组请求形态没被接受。
2) 播放量是延迟批量结算的（实测滞后 1~3 分钟），固定 settle 会把上一组的增量错记到
   下一组。因此每组打完都轮询到「连续 N 次不再变化」才进入下一组，增量才归属正确。
3) 先测一段「自然增长漂移」：不发请求只轮询，看稿件自己涨不涨，用来给各组减去背景噪声。

结论怎么读
----------
- 各组增量都在漂移量级内                    -> UA 对这个接口的计数没有可见影响。
- 某组增量明显高于 iPhone 基线且 code 正常  -> 该 UA 形态可能多走一份计数，需复测确认。
- 各组 code 大量 -352/-412                  -> 不是额度问题而是被风控，换 UA 无意义。
- iPhone 基线增量已经是 0 而 code 正常      -> 该稿件的移动端额度很可能已被打满，
  无法区分 UA 效果，请换一条全新稿件复测。

用法
----
    python tools/probe_ua.py BV1xxxxxxxxx
    python tools/probe_ua.py BV1xxxxxxxxx --variants iphone,ipad,tv --per-group 200
    python tools/probe_ua.py BV1xxxxxxxxx --poll-interval 30 --stable-rounds 2 --max-wait 480
    python tools/probe_ua.py BV1xxxxxxxxx --order tv,android,iphone     # 自定顺序
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

import booster  # noqa: E402  （复用 click_mobile_once / fetch_video_info / probe_cached 等）

_WORKERS = 60
REFERER = 'https://m.bilibili.com/video/{bv}'
ORIGIN = 'https://m.bilibili.com'
SPMID = '333.401'

# 各变体只差 User-Agent。iphone 是 booster 线上基线，作为对照组。
UAS = {
    'iphone': booster._mobile_ua,
    'android': ('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'),
    'ipad': ('Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 '
             '(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1'),
    'mac': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
            '(KHTML, like Gecko) Version/16.6 Safari/605.1.15'),
    'tv': ('Mozilla/5.0 (Linux; Android 9; SHIELD Android TV Build/PPR1.180610.011) '
           'Mobile Safari/537.36 BiliApp/1.0.0 os/tv'),
}


def line(ch='-', n=78):
    print(ch * n)


def extract_bvid(text):
    m = re.search(r'(BV[0-9A-Za-z]{10})', text or '')
    return m.group(1) if m else (text or '').strip()


def mobile_payload(info, bv):
    """与 booster.click_mobile_once 完全相同的 payload。"""
    now = int(time.time())
    return {
        'aid': info['aid'],
        'cid': info['cid'],
        'bvid': bv,
        'part': '1',
        'did': booster.generate_did(),
        'mid': '',
        'lv': '0',
        'ftime': now,
        'stime': now,
        'sub_type': '0',
        'spmid': SPMID,
        'from_spmid': 'undefined',
        'session': ''.join(random.choices('0123456789abcdef', k=8)),
        'csrf': '',
    }


def click_with_ua(proxy, ua, info, bv):
    """只把 UA 换成 ua，打一次 click/h5；返回 (http_status, body_code)。"""
    try:
        headers = {
            'User-Agent': ua,
            'Referer': REFERER.format(bv=bv),
            'Origin': ORIGIN,
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Cookie': booster.build_random_cookies(),
        }
        resp = requests.post(booster._mobile_click_url,
                             proxies={'http': f'http://{proxy}'},
                             headers=headers,
                             timeout=booster.timeout + 3,
                             data=mobile_payload(info, bv))
        code = None
        try:
            code = resp.json().get('code')
        except Exception:
            pass
        return resp.status_code, code
    except Exception:
        return None, None


def probe_alive(proxies, bv, log):
    cache = {}
    alive = []
    lock = threading.Lock()

    def worker(proxy):
        ok, _ = booster.probe_cached(proxy, bv, cache)
        if ok:
            with lock:
                alive.append(proxy)

    workers = max(1, min(booster.thread_num, len(proxies) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, proxies))
    log(f'  存活代理 {len(alive)}/{len(proxies)}')
    return alive


def fire(ua, proxies, info, bv):
    codes = Counter()
    lock = threading.Lock()
    workers = max(1, min(_WORKERS, len(proxies)))

    def worker(proxy):
        http, code = click_with_ua(proxy, ua, info, bv)
        with lock:
            if code is None:
                codes[f'http{http}' if http else 'exc'] += 1
            else:
                codes[f'code={code}'] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, proxies))
    return codes


def read_views(bv):
    return booster.fetch_video_info(bv)['stat']['view']


def read_until_stable(bv, poll_interval, stable_rounds, max_wait, label=''):
    """轮询播放量直到连续 stable_rounds 次不再变化，返回 (最终值, 时间线)。

    播放量是延迟批量结算的（实测滞后 1~3 分钟），固定 settle 会把某一组的增量
    错记到下一组头上。按「不改动即认为已结算」轮询，才能把每组增量归到正确的组。
    """
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
            timeline.append(f'[达到 {max_wait}s 上限，可能仍在结算]')
            break
    return last, timeline


def main():
    ap = argparse.ArgumentParser(description='换 UA 对 click/h5 计数影响的单变量对照')
    ap.add_argument('bvid', help='BV 号或含 BV 号的链接')
    ap.add_argument('--variants', default='iphone,android,ipad,mac,tv', help='要测的变体，逗号分隔')
    ap.add_argument('--order', default='', help='自定义执行顺序（默认 iphone 先跑做基线）')
    ap.add_argument('--per-group', type=int, default=150, help='每组使用的代理数（每个代理只打一次）')
    ap.add_argument('--poll-interval', type=int, default=30, help='轮询播放量的间隔秒数')
    ap.add_argument('--stable-rounds', type=int, default=2, help='连续多少次不变即认为结算完成')
    ap.add_argument('--max-wait', type=int, default=480, help='单组最多轮询等待秒数')
    args = ap.parse_args()

    bv = extract_bvid(args.bvid)
    if not bv.startswith('BV'):
        print(f'[X] 无法识别 BV 号: {args.bvid!r}')
        return

    variants = [g.strip() for g in (args.order or args.variants).split(',') if g.strip()]
    unknown = [v for v in variants if v not in UAS]
    if unknown:
        print(f'[X] 未知变体: {unknown}，可选: {list(UAS)}')
        return

    info = booster.fetch_video_info(bv)
    print(f'稿件: {info.get("title")}')
    print(f'  aid={info["aid"]} cid={info["cid"]} duration={info.get("duration")}s')
    print(f'  起始播放量: {info["stat"]["view"]}')

    # 背景漂移：不发请求，先看稿件自身涨不涨
    line('=')
    print('[0] 背景漂移测量：不发任何请求，只轮询到播放量稳定')
    line('=')
    d0 = read_views(bv)
    d_stable, d_timeline = read_until_stable(bv, args.poll_interval, args.stable_rounds, args.max_wait)
    drift = d_stable - d0
    print(f'  时间线: {" ".join(d_timeline)}')
    print(f'  自然增量: {drift:+d}（后面各组的增量都要减去这个量级）')

    line('=')
    print('拉取代理池...')
    line('=')
    proxies = booster.get_total_proxies(print)
    if not proxies:
        print('[X] 代理池为空')
        return
    print(f'共 {len(proxies)} 个候选代理，探测存活...')
    alive = probe_alive(proxies, bv, print)

    need = args.per_group * len(variants)
    if len(alive) < need:
        per = max(1, len(alive) // len(variants))
        print(f'[!] 存活代理仅 {len(alive)} 个，不足 {need}，每组下调为 {per} 个')
        args.per_group = per
    if args.per_group < 20:
        print('[X] 可用代理太少（每组 < 20），样本不足，建议稍后重试')
        return

    random.shuffle(alive)
    # 用列表而非 dict：允许同一变体重复出现（如末尾加一组 iphone 复核）
    slices = []
    for i, v in enumerate(variants):
        slices.append((v, alive[i * args.per_group:(i + 1) * args.per_group]))
    print(f'  已切成 {len(variants)} 份互不重叠，每组 {args.per_group} 个代理')

    results = []
    for name, group in slices:
        line('=')
        print(f'组 [{name}] UA={UAS[name][:70]}...')
        print(f'  {len(group)} 个代理，每个打一次 click/h5')
        line('=')
        before = read_views(bv)
        start = time.time()
        codes = fire(UAS[name], group, info, bv)
        cost = int(time.time() - start)
        print(f'  返回统计: {dict(codes)}  （耗时 {cost}s）')
        print(f'  轮询播放量直到稳定（间隔 {args.poll_interval}s，连续 {args.stable_rounds} 次不变）...')
        after, timeline = read_until_stable(bv, args.poll_interval, args.stable_rounds, args.max_wait)
        delta = after - before
        print(f'  时间线: {" ".join(timeline)}')
        print(f'  播放量 {before} -> {after}  (本组增量 {delta:+d})')
        results.append((name, len(group), dict(codes), delta))

    line('=')
    print('汇总（增量已含背景漂移，注意减去漂移量级）')
    line('=')
    print(f'  背景漂移: {drift:+d}')
    for name, n, codes, delta in results:
        net = delta - drift
        print(f'  {name:<8} 请求 {n:<4} 增量 {delta:+d}  净增量 {net:+d}  返回 {codes}')
    print('  判定：')
    print('   - 首组 iphone 增量≈0 而 code=0 正常   -> 该稿件额度已打满，本稿测不出 UA 差异，需换新稿。')
    print('   - ipad/tv 净增量为 0 且末组 iphone 也为 0 -> 额度耗尽，不能说明 UA 更差。')
    print('   - ipad/tv 净增量显著为正且首组 iphone 未打满 -> 该 UA 可能另计一份，需复测确认。')


if __name__ == '__main__':
    main()
