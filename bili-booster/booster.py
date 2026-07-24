"""
B站播放量提升 — 核心模块
完全基于 bili-booster-webui 逻辑，仅增加 stop_event 支持任务取消。
输出使用 print()，由 app.py 端 sys.stdout 重定向捕获。"""
import sys
import threading
import random
import hashlib
import time as time_module
from time import sleep
from datetime import date, datetime, timedelta

import requests
from requests.exceptions import RequestException
from fake_useragent import UserAgent

# ── 全局配置（与 webui 完全一致） ──
timeout = 5
thread_num = 100
batch_size = 200
request_delay = 0.01
per_video_boost = 80

# ── 全局 UserAgent 实例 ──
_ua_instance = UserAgent()


# =========================================================================
#  一、代理源（与 webui 完全一致）
# =========================================================================

def fetch_from_checkerproxy(log, min_count=100, max_lookback_days=7):
    day = date.today()
    for _ in range(max_lookback_days):
        day = day - timedelta(days=1)
        proxy_url = f'https://api.checkerproxy.net/v1/landing/archive/{day.strftime("%Y-%m-%d")}'
        log(f'getting proxies from {proxy_url} ...')
        try:
            response = requests.get(proxy_url, timeout=timeout)
            response.raise_for_status()
        except RequestException as err:
            log(f'checkerproxy unavailable: {err}')
            continue

        data = response.json()
        data_obj = data.get('data')
        if not data_obj:
            log(f'checkerproxy has no data for {day.strftime("%Y-%m-%d")}')
            continue

        proxies_obj = data_obj.get('proxyList')
        if isinstance(proxies_obj, list):
            total_proxies = proxies_obj
        elif isinstance(proxies_obj, dict):
            total_proxies = [proxy for proxy in proxies_obj.values() if proxy]
        else:
            log(f'unexpected checkerproxy proxyList type: {type(proxies_obj)}')
            continue

        if len(total_proxies) >= min_count:
            log(f'successfully get {len(total_proxies)} proxies from checkerproxy')
            return total_proxies
        log(f'only have {len(total_proxies)} proxies from checkerproxy')
    return []


def fetch_from_geonode(log, limit=500):
    proxy_url = 'https://proxylist.geonode.com/api/proxy-list'
    params = {
        'limit': limit,
        'page': 1,
        'sort_by': 'lastChecked',
        'sort_type': 'desc',
        'protocols': 'http',
    }
    log(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, params=params, timeout=timeout + 2)
    response.raise_for_status()
    data = response.json().get('data', [])
    proxies = [f"{item['ip']}:{item['port']}" for item in data if item.get('ip') and item.get('port')]
    log(f'successfully get {len(proxies)} proxies from geonode')
    return proxies


def fetch_plaintext_proxy_list(log, url, label, req_timeout=15):
    log(f'getting proxies from {url} ...')
    try:
        response = requests.get(url, timeout=req_timeout)
        response.raise_for_status()
        proxies = [line.strip() for line in response.text.splitlines() if line.strip() and ':' in line]
        log(f'successfully get {len(proxies)} proxies from {label}')
        return proxies
    except Exception as err:
        log(f'{label} failed: {err}')
        return []


def fetch_from_monosans(log):
    return fetch_plaintext_proxy_list(log,
        'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt',
        'monosans GitHub list', 8)


def fetch_from_scdnio(log, max_count=100):
    proxy_url = 'https://proxy.scdn.io/api/get_proxy.php'
    all_proxies = []
    calls_needed = (max_count + 19) // 20
    for i in range(calls_needed):
        params = {'protocol': 'http', 'count': 20}
        try:
            response = requests.get(proxy_url, params=params, timeout=timeout + 2)
            response.raise_for_status()
            data = response.json()
            proxies = data.get('data', {}).get('proxies', [])
            for p in proxies:
                if p and ':' in p:
                    all_proxies.append(p)
            log(f'  scdnio call {i+1}/{calls_needed}: got {len(proxies)} proxies')
        except Exception as err:
            log(f'  scdnio call {i+1} failed: {err}')
            break
        if i < calls_needed - 1:
            sleep(0.5)
    log(f'successfully get {len(all_proxies)} proxies from scdnio')
    return all_proxies


def fetch_from_89ip(log):
    proxy_url = 'http://api.89ip.cn/tqdl.html?api=1&num=9999'
    log(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, timeout=timeout + 5)
    response.raise_for_status()
    text = response.text
    parts = text.split('<br>')
    proxies = []
    for part in parts:
        line = part.strip()
        if line and ':' in line and not line.startswith('<'):
            proxies.append(line)
    log(f'successfully get {len(proxies)} proxies from 89ip')
    return proxies


def fetch_from_zdopen(log):
    base_url = 'http://www.zdopen.com/FreeProxy/Get/'
    params = {
        'app_id': '202605261442027753',
        'akey': '41ef99a09ee6b0ca',
        'return_type': 3,
    }
    all_proxies = []
    for dalu in (0, 1):
        try:
            response = requests.get(base_url, params={**params, 'dalu': dalu}, timeout=timeout + 5)
            response.raise_for_status()
            data = response.json()
            proxy_list = data.get('data', {}).get('proxy_list', [])
            for item in proxy_list:
                ip = item.get('ip')
                port = item.get('port')
                if ip and port:
                    all_proxies.append(f'{ip}:{port}')
            log(f'  zdopen dalu={dalu}: got {len(proxy_list)} proxies')
        except Exception as err:
            log(f'  zdopen dalu={dalu} failed: {err}')
        if dalu == 0:
            sleep(3)
    log(f'successfully get {len(all_proxies)} proxies from zdopen')
    return all_proxies


# =========================================================================
#  二、工具函数（与 webui 完全一致）
# =========================================================================

def build_view_params(video_id):
    normalized = video_id.strip()
    if not normalized:
        raise ValueError('video id is empty')
    lowered = normalized.lower()
    if lowered.startswith('av'):
        aid = normalized[2:]
        if not aid.isdigit():
            raise ValueError(f'invalid av id: {video_id}')
        return {'aid': aid}
    if normalized.isdigit():
        return {'aid': normalized}
    return {'bvid': normalized}


def generate_buvid3():
    rand_str = f'{random.random()}{time_module.time()}'
    digest = hashlib.md5(rand_str.encode()).hexdigest()
    return f'{digest[:8]}-{digest[8:12]}infoc'


def generate_buvid4():
    ts = int(time_module.time() * 1000)
    rand_str = f'{random.randint(0, 99999)}{ts}{random.random()}'
    digest = hashlib.md5(rand_str.encode()).hexdigest()
    return f'{ts}-{digest[:32]}'


def build_click_headers(ua, bv, cookies):
    return {
        'User-Agent': ua,
        'Referer': f'https://www.bilibili.com/video/{bv}/',
        'Origin': 'https://www.bilibili.com',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Cookie': cookies,
    }


def build_random_cookies():
    buvid3 = generate_buvid3()
    buvid4 = generate_buvid4()
    b_nut = str(int(time_module.time()))
    return f'buvid3={buvid3}; buvid4={buvid4}; b_nut={b_nut}; i-wanna-go-back=-1; header_theme_version=BMD25032713'


def fetch_video_info(video_id):
    params = build_view_params(video_id)
    response = requests.get(
        'https://api.bilibili.com/x/web-interface/view',
        params=params,
        headers={'User-Agent': _ua_instance.random},
        timeout=timeout + 2
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != 0 or 'data' not in payload:
        msg = payload.get('message', 'unknown error')
        raise RuntimeError(f'bilibili API error: code={payload.get("code")} message={msg}')
    data = payload['data']
    if not data.get('aid') or not data.get('bvid'):
        raise RuntimeError('video info missing key identifiers')
    return data


def get_total_proxies(log, stop_event=None):
    fetchers = [
        ('checkerproxy', fetch_from_checkerproxy),
        ('89ip', fetch_from_89ip),
        ('zdopen', fetch_from_zdopen),
        ('scdnio', fetch_from_scdnio),
        ('monosans', fetch_from_monosans),
        ('geonode', fetch_from_geonode),
    ]
    all_proxies = set()
    for name, fetcher in fetchers:
        if stop_event and stop_event.is_set():
            return []
        try:
            proxies = fetcher(log)
        except RequestException as err:
            log(f'{name} source failed: {err}')
            continue
        except Exception as err:
            log(f'{name} source error: {err}')
            continue
        for proxy in proxies:
            all_proxies.add(proxy)
    if all_proxies:
        log(f'collected {len(all_proxies)} proxies from available sources')
        return list(all_proxies)
    raise RuntimeError('failed to fetch proxies from all sources')


def time_fmt(seconds):
    if seconds < 60:
        return f'{seconds}s'
    else:
        return f'{int(seconds / 60)}min {seconds % 60}s'


def pbar(n, total):
    progress = '\u2501' * int(n / total * 50)
    blank = ' ' * (50 - len(progress))
    return f'{n}/{total} {progress}{blank}'


# =========================================================================
#  三、核心：筛选 + 点击（与 webui 完全一致）
# =========================================================================

def probe_once(proxy, bv):
    """轻量探测：GET 请求验证代理可用，不触发播放量。使用 HTTP 避免 HTTPS 隧道的高 CPU 开销。"""
    try:
        resp = requests.get(
            'http://api.bilibili.com/x/web-interface/view',
            params={'bvid': bv},
            proxies={'http': f'http://{proxy}'},
            headers={'User-Agent': _ua_instance.random},
            timeout=timeout,
            allow_redirects=False)
        return resp.status_code in (200, 301, 302, 307, 308)
    except:
        return False


def click_once(proxy, info, bv):
    """发送一次播放点击请求。"""
    try:
        ua = _ua_instance.random
        cookies = build_random_cookies()
        resp = requests.post('http://api.bilibili.com/x/click-interface/click/web/h5',
                             proxies={'http': f'http://{proxy}'},
                             headers=build_click_headers(ua, bv, cookies),
                             timeout=timeout,
                             data={
                                 'aid': info['aid'],
                                 'cid': info['cid'],
                                 'bvid': bv,
                                 'part': '1',
                                 'mid': info['owner']['mid'],
                                 'jsonp': 'jsonp',
                                 'type': info['desc_v2'][0]['type'] if info['desc_v2'] else '1',
                                 'sub_type': '0'
                             })
        return resp.status_code == 200
    except:
        return False


def click_batch(proxies, info, bv, stop_event=None):
    """批量并发点击（webui 原始逻辑）。"""
    success = [0]
    lock = threading.Lock()

    def worker(proxy):
        if stop_event and stop_event.is_set():
            return
        if click_once(proxy, info, bv):
            with lock:
                success[0] += 1

    threads = []
    for proxy in proxies:
        if stop_event and stop_event.is_set():
            break
        t = threading.Thread(target=worker, args=(proxy,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    return success[0]


def boost_video_once(video, total_proxies, batch_num, log, stop_event=None):
    """对单个视频执行一轮筛选+点击（webui 原始逻辑）。"""
    bv = video['bvid']
    info = video['info']

    proxies = list(total_proxies)
    random.shuffle(proxies)

    active_proxies = []
    count = [0]

    def filter_worker(proxies_slice):
        total = len(proxies)
        for proxy in proxies_slice:
            if stop_event and stop_event.is_set():
                return
            if probe_once(proxy, bv):
                active_proxies.append(proxy)
            count[0] += 1
            n = count[0]
            if n % 50 == 0 or n == total:
                print(f'\r[PROGRESS] {pbar(n, total)} {100*n/total:.1f}% [valid: {len(active_proxies)}]   ', end='')

    log(f'  filtering {len(proxies)} proxies for {bv}...')
    start_filter = datetime.now()
    thread_proxy_num = len(proxies) // thread_num
    threads = []
    for i in range(thread_num):
        start = i * thread_proxy_num
        end = start + thread_proxy_num if i < (thread_num - 1) else None
        t = threading.Thread(target=filter_worker, args=(proxies[start:end],))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    filter_cost = int((datetime.now() - start_filter).total_seconds())
    log(f'\n  filtered {len(active_proxies)} valid proxies using {time_fmt(filter_cost)}')

    if not active_proxies:
        log('  no valid proxies, skipping')
        return 0

    log(f'  sending clicks for {bv}...')
    start_click = datetime.now()
    clicks = 0
    for i in range(0, len(active_proxies), batch_size):
        if stop_event and stop_event.is_set():
            break
        chunk = active_proxies[i:i + batch_size]
        c = click_batch(chunk, info, bv, stop_event=stop_event)
        clicks += c
        log(f'    chunk {i//batch_size + 1}/{(len(active_proxies)-1)//batch_size + 1}: {c}/{len(chunk)} success')
        if request_delay > 0:
            sleep(request_delay)
    click_cost = int((datetime.now() - start_click).total_seconds())
    log(f'  done: {clicks} clicks in {time_fmt(click_cost)}')

    return clicks


# =========================================================================
#  四、主入口（webui 原始循环 + log_fn / stop_event / logger_name）
# =========================================================================

def _boost_single_video(v, total_proxies, round_num, target, log, stop_event, video_lock):
    """单个视频的 boost 任务，供多视频并发调用。"""
    if stop_event and stop_event.is_set():
        return
    if v['current'] >= target:
        log(f'  {v["bvid"]} already at target ({v["current"]}), skipping')
        return

    log(f'--- boosting {v["bvid"]} (current: {v["current"]}, target: {target}) ---')
    hits = boost_video_once(v, total_proxies, round_num, log, stop_event=stop_event)
    with video_lock:
        v['total_hits'] += hits

    try:
        fresh = fetch_video_info(v['bvid'])
        with video_lock:
            v['current'] = fresh['stat']['view']
        gap = max(0, target - v['current'])
        log(f'  {v["bvid"]} views: {v["current"]} (+{v["current"] - v["initial"]})')
        log(f'[RESULT] video|{v["bvid"]}|{v["current"]}|{v["current"] - v["initial"]}|{v["total_hits"]}|{gap}')
    except Exception as e:
        log(f'  failed to check views: {e}')


def main(bv_input, target_input, stop_event=None):
    """启动播放量提升任务。完全基于 bili-booster-webui 的 main() 循环逻辑。"""
    log = print

    bv_list = [bv.strip() for bv in bv_input.split(',') if bv.strip()]
    if not bv_list:
        log('no valid video ids provided')
        return

    target = int(target_input)

    videos = []
    for raw_bv in bv_list:
        if stop_event and stop_event.is_set():
            log('stopped by user')
            return
        log(f'fetching info for {raw_bv}...')
        try:
            info = fetch_video_info(raw_bv)
            videos.append({
                'bvid': info['bvid'],
                'info': info,
                'initial': info['stat']['view'],
                'current': info['stat']['view'],
                'total_hits': 0,
            })
            gap = max(0, target - info['stat']['view'])
            log(f'  {info["bvid"]}: initial views = {info["stat"]["view"]}')
            log(f'[RESULT] video|{info["bvid"]}|{info["stat"]["view"]}|0|0|{gap}')
        except Exception as e:
            log(f'  failed: {e}')

    if not videos:
        log('no videos available, exiting')
        return

    round_num = 0
    max_rounds = 5
    no_growth_streak = 0

    while True:
        if stop_event and stop_event.is_set():
            log('stopped by user')
            break

        all_done = True
        for v in videos:
            if v['current'] < target:
                all_done = False
                break
        if all_done:
            break

        round_num += 1
        if round_num > max_rounds:
            log(f'\nmax rounds ({max_rounds}) reached, stopping')
            break

        # snapshot views before this round
        views_before = {v['bvid']: v['current'] for v in videos}

        log(f'\n========== ROUND {round_num}/{max_rounds} ==========')

        try:
            total_proxies = get_total_proxies(log, stop_event=stop_event)
        except Exception as e:
            log(f'failed to fetch proxies: {e}')
            sleep(5)
            continue

        if not total_proxies:
            log('代理列表为空，跳过本轮')
            continue

        # 多视频完全并发执行
        video_lock = threading.Lock()
        pending = [v for v in videos if v['current'] < target]
        threads = []
        for v in pending:
            if stop_event and stop_event.is_set():
                break
            t = threading.Thread(target=_boost_single_video, args=(v, total_proxies, round_num, target, log, stop_event, video_lock))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # check if any video grew this round
        any_growth = False
        for v in videos:
            if v['current'] > views_before[v['bvid']]:
                any_growth = True
                break

        if any_growth:
            no_growth_streak = 0
        else:
            no_growth_streak += 1
            log(f'\n  warning: no views growth in this round (streak: {no_growth_streak})')
            if no_growth_streak >= 2:
                log(f'  no growth for {no_growth_streak} consecutive rounds, stopping')
                break

        reached = sum(1 for v in videos if v['current'] >= target)
        total_clicks = sum(v['total_hits'] for v in videos)
        total_growth = sum(v['current'] - v['initial'] for v in videos)
        log(f'\n========== ROUND {round_num} SUMMARY ==========')
        for v in videos:
            log(f'  {v["bvid"]}: {v["current"]} (+{v["current"] - v["initial"]}) | hits: {v["total_hits"]}')
        log(f'[RESULT] round|{round_num}|{len(videos)}|{reached}|{total_clicks}|{total_growth}')

    log(f'\nFinish at {datetime.now().strftime("%H:%M:%S")}')
    log(f'Final Statistics:')
    for v in videos:
        gap = max(0, target - v['current'])
        log(f'  {v["bvid"]}: {v["initial"]} -> {v["current"]} (+{v["current"] - v["initial"]}) | total hits: {v["total_hits"]}')
        log(f'[RESULT] video|{v["bvid"]}|{v["current"]}|{v["current"] - v["initial"]}|{v["total_hits"]}|{gap}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
