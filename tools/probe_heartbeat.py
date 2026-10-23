# -*- coding: utf-8 -*-
"""试探 D：播放心跳通道（click-interface/web/heartbeat）是否是一份独立播放额度。

背景
----
booster 现有两条通道（click/web/h5 网页端、click/h5 移动端）已实测互相独立。
BAC 文档把「观看数据上报」拆成互不相同的接口，官方播放器两条都发：
  - 开始观看：click/web/h5、click/h5        <- booster 现用
  - 播放进度：x/click-interface/web/heartbeat <- 本脚本要验的第三条
两条走的是不同计数路径（「开始观看」有约 200 次/稿件的上限，心跳是「播放进度」上报）。
若心跳单独计数，就是白拿的第三份额度。

做法
----
对同一稿件，按顺序依次打三组请求（每组一次性用整批存活代理），每组打完等
--settle 秒（播放量更新非实时）再读一次 API 播放量，打印本组增量：

  组1 web      click/web/h5      （对照，复用 booster.click_once）
  组2 mobile   click/h5          （对照，复用 booster.click_mobile_once）
  组3 heartbeat web/heartbeat    （目标，本脚本实现）

先跑对照组再跑心跳组：如果对照组的额度已被吃光（增量≈0），而心跳组仍有正增量，
即可判定心跳是独立的一份额度。心跳组额外打印接口 body 的真实 code 分布
（booster 的 click_once 只看 HTTP 200，不看 code，这里补上；code=-352/-412 表示风控）。

判定
----
- 心跳组 code=0 占比高 且 播放量上涨（对照组已不涨） -> 独立份额，可接入 booster。
- 心跳组 code=0 但播放量不涨                        -> 接受但不计数（与 web 共享同一池）。
- 心跳组 code=-352/-412 占比高                       -> 风控拦截或匿名会话不被接受。

心跳探针之外还挂了 TV/OTT 变体组（VARIANTS），同样按「打一批 -> 等 --settle 秒 -> 读增量」验证。
TV 端免登录，理论上可能是又一份独立额度；最好挑一条 web/mobile 都已打满（增量恒为 0）的稿件做
测试床：此时任一变体组出现正增量，即可确定它不共用 web/mobile 的池。

用法
----
    python tools/probe_heartbeat.py BV1xxxxxxx --settle 20                 # 走代理池，三组全跑
    python tools/probe_heartbeat.py BV1xxxxxxx --clicks 100 --groups web,heartbeat
    python tools/probe_heartbeat.py BV1xxxxxxx --groups tv,ott,android_tv_yst,yst_tv
    python tools/probe_heartbeat.py BV1xxxxxxx --direct --clicks 5         # 直连本机 IP 冒烟（只打心跳）
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

import booster  # noqa: E402  （复用 fetch_video_info / get_total_proxies / probe_cached / click_once / click_mobile_once）

HEARTBEAT_URL = 'https://api.bilibili.com/x/click-interface/web/heartbeat'
_DESKTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
# 云视听小电视（TV 端）UA，沿用上次 probe_endpoints 的定义
_TV_UA = ('Mozilla/5.0 (Linux; Android 9; SHIELD Android TV Build/PPR1.180610.011) '
          'Mobile Safari/537.36 BiliApp/1.0.0 os/tv')
_WORKERS = 60

# 变体通道：kind -> (url, ua, referer(支持 {bv} 占位), origin, spmid)
VARIANTS = {
    'tv': ('http://api.bilibili.com/x/click-interface/click/tv',
           _TV_UA, 'https://www.bilibili.com/', 'https://www.bilibili.com', '333.401'),
    'ott': ('http://api.bilibili.com/x/click-interface/click/ott',
            _TV_UA, 'https://www.bilibili.com/', 'https://www.bilibili.com', '333.401'),
    'android_tv_yst': ('http://api.bilibili.com/x/click-interface/click/android_tv_yst',
                       _TV_UA, 'https://www.bilibili.com/', 'https://www.bilibili.com', '333.401'),
    'yst_tv': ('https://api.snm0516.aisee.tv/x/click-interface/click/tv',
               _TV_UA, 'https://www.bilibili.com/', 'https://www.bilibili.com', '333.401'),
    # spmid 分池假设：同一 click/h5，换入口 spmid 是否独立计数（在饱和稿件上测最干净）
    'm_788': ('http://api.bilibili.com/x/click-interface/click/h5',
              booster._mobile_ua, 'https://m.bilibili.com/video/{bv}', 'https://m.bilibili.com', '333.788'),
    'm_337': ('http://api.bilibili.com/x/click-interface/click/h5',
              booster._mobile_ua, 'https://m.bilibili.com/video/{bv}', 'https://m.bilibili.com', '333.337'),
    # 移动端心跳候选：移动端真实播放会话里除 click/h5 外还有一条 report 上报
    'm_report': ('http://api.bilibili.com/x/report/click/h5',
                 booster._mobile_ua, 'https://m.bilibili.com/video/{bv}', 'https://m.bilibili.com', '333.401'),
}

# UA 分池假设：同一 click/h5、同一 spmid，仅换移动端 UA（iOS/Android 版本）是否独立计数。
# 注意：这些组复刻 booster 的移动端载荷（不走 click_payload），保证唯一变量是 User-Agent。
UA_VARIANTS = {
    'ua_ios16': booster._mobile_ua,  # 对照：booster 现用 UA（iOS 16.6 Safari）
    'ua_ios17': ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) '
                 'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 '
                 'Mobile/15E148 Safari/604.1'),
    'ua_ios15': ('Mozilla/5.0 (iPhone; CPU iPhone OS 15_8 like Mac OS X) '
                 'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 '
                 'Mobile/15E148 Safari/604.1'),
    'ua_android': ('Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'),
    'ua_ipad': ('Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 '
                'Mobile/15E148 Safari/604.1'),
    # Mac 桌面 UA（未细化机型，按 macOS 10.15.7 + Safari 16.6 配置）
    'ua_mac': ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
               'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 '
               'Safari/605.1.15'),
}


def line(ch='-', n=78):
    print(ch * n)


def extract_bvid(text):
    m = re.search(r'(BV[0-9A-Za-z]{10})', text or '')
    return m.group(1) if m else (text or '').strip()


def heartbeat_data(info, bvid):
    """复刻网页端播放器的心跳载荷（参数含义见 BAC「上报视频播放心跳（web端）」）。"""
    duration = int(info.get('duration') or 60)
    now = int(time.time())
    return {
        'aid': info['aid'],
        'cid': info['cid'],
        'bvid': bvid,
        'mid': '',
        'played_time': duration - 1,
        'realtime': duration,
        'real_played_time': duration,
        'start_ts': now - duration,
        'type': '3',
        'sub_type': '0',
        'dt': '2',
        'outer': '0',
        'play_type': '0',
        'quality': '80',
        'video_duration': duration,
        'last_play_progress_time': duration - 1,
        'max_play_progress_time': duration - 1,
        'session': ''.join(random.choices('0123456789abcdef', k=8)),
        'spmid': '333.788.0.0',
        'from_spmid': '333.788.0.0',
        'refer_url': f'https://www.bilibili.com/video/{bvid}/',
        'csrf': '',
    }


def heartbeat_once(proxy, info, bvid):
    """打一次播放心跳，返回 (http_status, body_code)。"""
    try:
        cookies = booster.build_random_cookies()
        headers = booster.build_click_headers(_DESKTOP_UA, bvid, cookies)
        proxies = {'http': f'http://{proxy}', 'https': f'http://{proxy}'} if proxy else None
        resp = requests.post(HEARTBEAT_URL,
                             proxies=proxies,
                             headers=headers,
                             timeout=booster.timeout + 3,
                             data=heartbeat_data(info, bvid))
        code = None
        try:
            code = resp.json().get('code')
        except Exception:
            pass
        return resp.status_code, code
    except Exception:
        return None, None


def click_payload(info, bvid, spmid='333.401'):
    """click 变体通用载荷：web 的 type/sub_type/jsonp 与 mobile 的 did/lv/ftime/stime 取并集，
    先解决「接不接受」，不纠结参数最简。"""
    now = int(time.time())
    return {
        'aid': info['aid'],
        'cid': info['cid'],
        'bvid': bvid,
        'part': '1',
        'mid': '',
        'jsonp': 'jsonp',
        'did': booster.generate_did(),
        'lv': '0',
        'ftime': now,
        'stime': now,
        'type': info['desc_v2'][0]['type'] if info.get('desc_v2') else '1',
        'sub_type': '0',
        'spmid': spmid,
        'from_spmid': 'undefined',
        'session': ''.join(random.choices('0123456789abcdef', k=8)),
        'csrf': '',
    }


def variant_once(kind, proxy, info, bvid):
    """打一次 TV/OTT 变体通道，返回 (http_status, body_code)。"""
    url, ua, referer, origin, spmid = VARIANTS[kind]
    try:
        cookies = booster.build_random_cookies()
        headers = {
            'User-Agent': ua,
            'Referer': referer.format(bv=bvid),
            'Origin': origin,
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Cookie': cookies,
        }
        proxies = {'http': f'http://{proxy}', 'https': f'http://{proxy}'} if proxy else None
        resp = requests.post(url,
                             proxies=proxies,
                             headers=headers,
                             timeout=booster.timeout + 3,
                             data=click_payload(info, bvid, spmid))
        code = None
        try:
            code = resp.json().get('code')
        except Exception:
            pass
        return resp.status_code, code
    except Exception:
        return None, None


def mobile_ua_once(proxy, info, bvid, ua):
    """打一次 click/h5，载荷与 booster.click_mobile_once 完全一致，仅 User-Agent 不同。
    返回 (http_status, body_code)。"""
    try:
        cookies = booster.build_random_cookies()
        headers = {
            'User-Agent': ua,
            'Referer': f'https://m.bilibili.com/video/{bvid}',
            'Origin': 'https://m.bilibili.com',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Cookie': cookies,
        }
        now = int(time.time())
        proxies = {'http': f'http://{proxy}', 'https': f'http://{proxy}'} if proxy else None
        resp = requests.post(booster._mobile_click_url,
                             proxies=proxies,
                             headers=headers,
                             timeout=booster.timeout + 3,
                             data={
                                 'aid': info['aid'],
                                 'cid': info['cid'],
                                 'bvid': bvid,
                                 'part': '1',
                                 'did': booster.generate_did(),
                                 'mid': '',
                                 'lv': '0',
                                 'ftime': now,
                                 'stime': now,
                                 'sub_type': '0',
                                 'spmid': '333.401',
                                 'from_spmid': 'undefined',
                                 'session': ''.join(random.choices('0123456789abcdef', k=8)),
                                 'csrf': '',
                             })
        code = None
        try:
            code = resp.json().get('code')
        except Exception:
            pass
        return resp.status_code, code
    except Exception:
        return None, None


def filter_alive(proxies, bvid, log):
    """并发探测存活代理，返回存活列表（复用 booster 的探测与缓存逻辑）。"""
    cache = {}
    alive = []
    lock = threading.Lock()
    total = len(proxies)

    def worker(proxy):
        ok, _ = booster.probe_cached(proxy, bvid, cache)
        if ok:
            with lock:
                alive.append(proxy)

    workers = max(1, min(booster.thread_num, total or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, proxies))
    log(f'  存活代理 {len(alive)}/{total}')
    return alive


def fire(kind, proxies, info, bvid, stop_event=None):
    """对一个代理批次并发打指定通道；心跳组额外统计 body code 分布。"""
    codes = Counter()
    lock = threading.Lock()
    workers = max(1, min(_WORKERS, len(proxies)))

    def worker(proxy):
        if stop_event and stop_event.is_set():
            return
        if kind == 'web':
            ok = booster.click_once(proxy, info, bvid)
            with lock:
                codes['http200' if ok else 'http_fail'] += 1
        elif kind == 'mobile':
            ok = booster.click_mobile_once(proxy, info, bvid)
            with lock:
                codes['http200' if ok else 'http_fail'] += 1
        elif kind in UA_VARIANTS:
            http, code = mobile_ua_once(proxy, info, bvid, UA_VARIANTS[kind])
            with lock:
                if code is None:
                    codes[f'http{http}' if http else 'exc'] += 1
                else:
                    codes[f'code={code}'] += 1
        else:
            http, code = (heartbeat_once(proxy, info, bvid) if kind == 'heartbeat'
                          else variant_once(kind, proxy, info, bvid))
            with lock:
                if code is None:
                    codes[f'http{http}' if http else 'exc'] += 1
                else:
                    codes[f'code={code}'] += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, proxies))
    return codes


def read_views(bvid):
    return booster.fetch_video_info(bvid)['stat']['view']


def main():
    ap = argparse.ArgumentParser(description='播放心跳通道计数探测')
    ap.add_argument('bvid', help='BV 号或含 BV 号的链接')
    ap.add_argument('--groups', default='web,mobile,heartbeat', help='按序执行的组，逗号分隔')
    ap.add_argument('--clicks', type=int, default=0, help='每组最多使用的代理数，0=全部存活代理')
    ap.add_argument('--settle', type=int, default=20, help='每组打完等多少秒再读播放量（非实时更新）')
    ap.add_argument('--final-wait', type=int, default=0,
                    help='全部组跑完后再等多少秒做一次总增量复核（捕捉播放量延迟）')
    ap.add_argument('--direct', action='store_true', help='不走代理池，用本机 IP 直连（仅冒烟，容易判定不计数）')
    ap.add_argument('--direct-clicks', type=int, default=5, help='--direct 时的请求次数')
    args = ap.parse_args()

    bvid = extract_bvid(args.bvid)
    if not bvid.startswith('BV'):
        print(f'[X] 无法识别 BV 号: {args.bvid!r}')
        return

    info = booster.fetch_video_info(bvid)
    print(f'稿件: {info.get("title")}   aid={info["aid"]} cid={info["cid"]} duration={info.get("duration")}s')
    start_view = info['stat']['view']
    print(f'起始播放量: {start_view}')

    if args.direct:
        line('=')
        print(f'直连冒烟：本机 IP 打 {args.direct_clicks} 次心跳')
        line('=')
        before = info['stat']['view']
        codes = Counter()
        for _ in range(args.direct_clicks):
            http, code = heartbeat_once(None, info, bvid)
            codes[f'code={code}' if code is not None else f'http{http}'] += 1
            time.sleep(0.3)
        print(f'  返回: {dict(codes)}')
        print(f'  等待 {args.settle}s 后读播放量...')
        time.sleep(args.settle)
        after = read_views(bvid)
        print(f'  播放量 {before} -> {after}  (增量 {after - before:+d})')
        print('  提示：直连单 IP 会被去重，增量为 0 不能说明通道无效，需走代理池再测。')
        return

    line('=')
    print('拉取代理池...')
    line('=')
    proxies = booster.get_total_proxies(print)
    if not proxies:
        print('[X] 代理池为空')
        return
    print(f'共 {len(proxies)} 个代理，开始探测存活...')
    alive = filter_alive(proxies, bvid, print)
    if not alive:
        print('[X] 无存活代理')
        return

    random.shuffle(alive)
    if args.clicks > 0:
        alive = alive[:args.clicks]
    print(f'本组可用代理: {len(alive)}')

    results = []
    for kind in [g.strip() for g in args.groups.split(',') if g.strip()]:
        line('=')
        print(f'组 [{kind}] 发送 {len(alive)} 次（{len(alive)} 个代理，每个一次）')
        line('=')
        before = read_views(bvid)
        start = time.time()
        codes = fire(kind, alive, info, bvid)
        cost = int(time.time() - start)
        print(f'  返回统计: {dict(codes)}  （耗时 {cost}s）')
        print(f'  等待 {args.settle}s 后读播放量...')
        time.sleep(args.settle)
        after = read_views(bvid)
        delta = after - before
        print(f'  播放量 {before} -> {after}  (增量 {delta:+d})')
        results.append((kind, len(alive), dict(codes), delta))

    line('=')
    print('汇总')
    line('=')
    for kind, n, codes, delta in results:
        print(f'  {kind:<10} 请求 {n:<5} 增量 {delta:+d}   返回 {codes}')
    print('  判定：对照组（web/mobile）增量≈0 而 heartbeat 增量为正 => 心跳是独立额度。')
    print('        同稿同批代理下，先跑 ua_ios16 打满，再跑其他 UA：仍有正增量 => UA 分池。')

    if args.final_wait > 0:
        print(f'等待 {args.final_wait}s 后做总增量复核（播放量有延迟）...')
        time.sleep(args.final_wait)
        final = read_views(bvid)
        total = final - start_view
        summed = sum(r[3] for r in results)
        print(f'  播放量 {start_view} -> {final}  (总增量 {total:+d})')
        print(f'  各组增量之和 {summed:+d}，差额 {total - summed:+d} 为延迟补涨')


if __name__ == '__main__':
    main()
