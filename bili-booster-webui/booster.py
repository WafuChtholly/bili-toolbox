import sys
import threading
import random
import hashlib
import time as time_module
from time import sleep
from typing import Optional
from datetime import date, datetime, timedelta

import requests
from requests.exceptions import RequestException
from fake_useragent import UserAgent

timeout = 5
thread_num = 200
batch_size = 200
request_delay = 0.01
per_video_boost = 80


def fetch_from_checkerproxy(min_count: int = 100, max_lookback_days: int = 7) -> list[str]:
    day = date.today()
    for _ in range(max_lookback_days):
        day = day - timedelta(days=1)
        proxy_url = f'https://api.checkerproxy.net/v1/landing/archive/{day.strftime("%Y-%m-%d")}'
        print(f'getting proxies from {proxy_url} ...')
        try:
            response = requests.get(proxy_url, timeout=timeout)
            response.raise_for_status()
        except RequestException as err:
            print(f'checkerproxy unavailable: {err}')
            continue

        data = response.json()
        data_obj = data.get('data')
        if not data_obj:
            print(f'checkerproxy has no data for {day.strftime("%Y-%m-%d")}')
            continue

        proxies_obj = data_obj.get('proxyList')
        if isinstance(proxies_obj, list):
            total_proxies = proxies_obj
        elif isinstance(proxies_obj, dict):
            total_proxies = [proxy for proxy in proxies_obj.values() if proxy]
        else:
            print(f'unexpected checkerproxy proxyList type: {type(proxies_obj)}')
            continue

        if len(total_proxies) >= min_count:
            print(f'successfully get {len(total_proxies)} proxies from checkerproxy')
            return total_proxies
        print(f'only have {len(total_proxies)} proxies from checkerproxy')
    return []


def fetch_from_geonode(limit: int = 500) -> list[str]:
    proxy_url = 'https://proxylist.geonode.com/api/proxy-list'
    params = {
        'limit': limit,
        'page': 1,
        'sort_by': 'lastChecked',
        'sort_type': 'desc',
        'protocols': 'http',
    }
    print(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, params=params, timeout=timeout + 2)
    response.raise_for_status()
    data = response.json().get('data', [])
    proxies = [f"{item['ip']}:{item['port']}" for item in data if item.get('ip') and item.get('port')]
    print(f'successfully get {len(proxies)} proxies from geonode')
    return proxies


def fetch_plaintext_proxy_list(url: str, label: str, req_timeout: int = 15) -> list[str]:
    print(f'getting proxies from {url} ...')
    try:
        response = requests.get(url, timeout=req_timeout)
        response.raise_for_status()
        proxies = [line.strip() for line in response.text.splitlines() if line.strip() and ':' in line]
        print(f'successfully get {len(proxies)} proxies from {label}')
        return proxies
    except Exception as err:
        print(f'{label} failed: {err}')
        return []


def fetch_from_shiftytr() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt',
        'ShiftyTR GitHub list', 15)


def fetch_from_roosterkid() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt',
        'roosterkid GitHub list', 15)


def fetch_from_mmpx12() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt',
        'mmpx12 GitHub list', 15)


def fetch_from_monosans() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt',
        'monosans GitHub list', 8)


def fetch_from_clarketm() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt',
        'clarketm GitHub list', 8)


def fetch_from_proxy4parsing() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt',
        'proxy4parsing GitHub list', 8)


def fetch_from_zevtyardt() -> list[str]:
    return fetch_plaintext_proxy_list(
        'https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt',
        'zevtyardt GitHub list', 8)


def fetch_from_scdnio(max_count: int = 100) -> list[str]:
    proxy_url = 'https://proxy.scdn.io/api/get_proxy.php'
    all_proxies: list[str] = []
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
            print(f'  scdnio call {i+1}/{calls_needed}: got {len(proxies)} proxies')
        except Exception as err:
            print(f'  scdnio call {i+1} failed: {err}')
            break
        if i < calls_needed - 1:
            sleep(0.5)
    print(f'successfully get {len(all_proxies)} proxies from scdnio')
    return all_proxies


def fetch_from_89ip() -> list[str]:
    proxy_url = 'http://api.89ip.cn/tqdl.html?api=1&num=9999'
    print(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, timeout=timeout + 5)
    response.raise_for_status()
    text = response.text
    parts = text.split('<br>')
    proxies = []
    for part in parts:
        line = part.strip()
        if line and ':' in line and not line.startswith('<'):
            proxies.append(line)
    print(f'successfully get {len(proxies)} proxies from 89ip')
    return proxies


def fetch_from_zdopen() -> list[str]:
    base_url = 'http://www.zdopen.com/FreeProxy/Get/'
    params = {
        'app_id': '202605261442027753',
        'akey': '41ef99a09ee6b0ca',
        'return_type': 3,
    }
    all_proxies: list[str] = []
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
            print(f'  zdopen dalu={dalu}: got {len(proxy_list)} proxies')
        except Exception as err:
            print(f'  zdopen dalu={dalu} failed: {err}')
        if dalu == 0:
            sleep(3)
    print(f'successfully get {len(all_proxies)} proxies from zdopen')
    return all_proxies


def build_view_params(video_id: str) -> dict[str, str]:
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


def generate_buvid3() -> str:
    rand_str = f'{random.random()}{time_module.time()}'
    digest = hashlib.md5(rand_str.encode()).hexdigest()
    return f'{digest[:8]}-{digest[8:12]}infoc'


def generate_buvid4() -> str:
    ts = int(time_module.time() * 1000)
    rand_str = f'{random.randint(0, 99999)}{ts}{random.random()}'
    digest = hashlib.md5(rand_str.encode()).hexdigest()
    return f'{ts}-{digest[:32]}'


def build_click_headers(ua: str, bv: str, cookies: str) -> dict[str, str]:
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


def build_random_cookies() -> str:
    buvid3 = generate_buvid3()
    buvid4 = generate_buvid4()
    b_nut = str(int(time_module.time()))
    return f'buvid3={buvid3}; buvid4={buvid4}; b_nut={b_nut}; i-wanna-go-back=-1; header_theme_version=BMD25032713'


def fetch_video_info(video_id: str) -> dict:
    params = build_view_params(video_id)
    response = requests.get(
        'https://api.bilibili.com/x/web-interface/view',
        params=params,
        headers={'User-Agent': UserAgent().random},
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


def get_total_proxies() -> list[str]:
    fetchers = [
        ('checkerproxy', fetch_from_checkerproxy),
        ('89ip', fetch_from_89ip),
        ('zdopen', fetch_from_zdopen),
        ('scdnio', fetch_from_scdnio),
        ('mmpx12', fetch_from_mmpx12),
        ('shiftytr', fetch_from_shiftytr),
        ('roosterkid', fetch_from_roosterkid),
        ('proxy4parsing', fetch_from_proxy4parsing),
        ('zevtyardt', fetch_from_zevtyardt),
        ('monosans', fetch_from_monosans),
        ('clarketm', fetch_from_clarketm),
        ('geonode', fetch_from_geonode),
    ]
    all_proxies: set[str] = set()
    for name, fetcher in fetchers:
        try:
            proxies = fetcher()
        except RequestException as err:
            print(f'{name} source failed: {err}')
            continue
        except Exception as err:
            print(f'{name} source error: {err}')
            continue
        for proxy in proxies:
            all_proxies.add(proxy)
    if all_proxies:
        print(f'collected {len(all_proxies)} proxies from available sources')
        return list(all_proxies)
    raise RuntimeError('failed to fetch proxies from all sources')


def time_fmt(seconds: int) -> str:
    if seconds < 60:
        return f'{seconds}s'
    else:
        return f'{int(seconds / 60)}min {seconds % 60}s'


def pbar(n: int, total: int) -> str:
    progress = '━' * int(n / total * 50)
    blank = ' ' * (50 - len(progress))
    return f'\r{n}/{total} {progress}{blank}'


def click_once(proxy: str, info: dict, bv: str) -> bool:
    try:
        ua = UserAgent().random
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


def click_batch(proxies: 'list[str]', info: dict, bv: str) -> int:
    success = [0]
    lock = threading.Lock()

    def worker(proxy: str) -> None:
        if click_once(proxy, info, bv):
            with lock:
                success[0] += 1

    threads = []
    for proxy in proxies:
        t = threading.Thread(target=worker, args=(proxy,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    return success[0]


def boost_video_once(video: dict, total_proxies: 'list[str]', batch_num: int) -> int:
    bv = video['bvid']
    info = video['info']

    random.shuffle(total_proxies)

    active_proxies = []
    count = [0]

    def filter_worker(proxies: 'list[str]') -> None:
        for proxy in proxies:
            if click_once(proxy, info, bv):
                active_proxies.append(proxy)
            count[0] += 1
            n = count[0]
            print(f'{pbar(n, len(total_proxies))} {100*n/len(total_proxies):.1f}% [valid: {len(active_proxies)}]   ', end='')

    print(f'  filtering {len(total_proxies)} proxies for {bv}...')
    start_filter = datetime.now()
    thread_proxy_num = len(total_proxies) // thread_num
    threads = []
    for i in range(thread_num):
        start = i * thread_proxy_num
        end = start + thread_proxy_num if i < (thread_num - 1) else None
        t = threading.Thread(target=filter_worker, args=(total_proxies[start:end],))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    filter_cost = int((datetime.now() - start_filter).total_seconds())
    print(f'\n  filtered {len(active_proxies)} valid proxies using {time_fmt(filter_cost)}')

    if not active_proxies:
        print('  no valid proxies, skipping')
        return 0

    print(f'  sending clicks for {bv}...')
    start_click = datetime.now()
    clicks = 0
    for i in range(0, len(active_proxies), batch_size):
        chunk = active_proxies[i:i + batch_size]
        c = click_batch(chunk, info, bv)
        clicks += c
        print(f'    chunk {i//batch_size + 1}/{(len(active_proxies)-1)//batch_size + 1}: {c}/{len(chunk)} success')
        if request_delay > 0:
            sleep(request_delay)
    click_cost = int((datetime.now() - start_click).total_seconds())
    print(f'  done: {clicks} clicks in {time_fmt(click_cost)}')

    return clicks


def main(bv_input, target_input):
    bv_list = [bv.strip() for bv in bv_input.split(',') if bv.strip()]
    if not bv_list:
        print('no valid video ids provided')
        sys.exit(1)

    target = int(target_input)

    videos = []
    print()
    for raw_bv in bv_list:
        print(f'fetching info for {raw_bv}...')
        try:
            info = fetch_video_info(raw_bv)
            videos.append({
                'bvid': info['bvid'],
                'info': info,
                'initial': info['stat']['view'],
                'current': info['stat']['view'],
                'total_hits': 0,
            })
            print(f'  {info["bvid"]}: initial views = {info["stat"]["view"]}')
        except Exception as e:
            print(f'  failed: {e}')

    if not videos:
        print('no videos available, exiting')
        sys.exit(1)

    round_num = 0
    max_rounds = 5
    no_growth_streak = 0

    while True:
        all_done = True
        for v in videos:
            if v['current'] < target:
                all_done = False
                break
        if all_done:
            break

        round_num += 1
        if round_num > max_rounds:
            print(f'\nmax rounds ({max_rounds}) reached, stopping')
            break

        # snapshot views before this round
        views_before = {v['bvid']: v['current'] for v in videos}

        print(f'\n========== ROUND {round_num}/{max_rounds} ==========')

        try:
            total_proxies = get_total_proxies()
        except Exception as e:
            print(f'failed to fetch proxies: {e}')
            sleep(5)
            continue

        for v in videos:
            if v['current'] >= target:
                print(f'\n  {v["bvid"]} already at target ({v["current"]}), skipping')
                continue

            print(f'\n--- boosting {v["bvid"]} (current: {v["current"]}, target: {target}) ---')
            hits = boost_video_once(v, total_proxies, round_num)
            v['total_hits'] += hits

            try:
                fresh = fetch_video_info(v['bvid'])
                v['current'] = fresh['stat']['view']
                print(f'  {v["bvid"]} views: {v["current"]} (+{v["current"] - v["initial"]})')
            except Exception as e:
                print(f'  failed to check views: {e}')

            sleep(random.uniform(1, 3))

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
            print(f'\n  warning: no views growth in this round (streak: {no_growth_streak})')
            if no_growth_streak >= 2:
                print(f'  no growth for {no_growth_streak} consecutive rounds, stopping')
                break

        print(f'\n========== ROUND {round_num} SUMMARY ==========')
        for v in videos:
            print(f'  {v["bvid"]}: {v["current"]} (+{v["current"] - v["initial"]}) | hits: {v["total_hits"]}')

    print(f'\nFinish at {datetime.now().strftime("%H:%M:%S")}')
    print(f'Final Statistics:')
    for v in videos:
        print(f'  {v["bvid"]}: {v["initial"]} -> {v["current"]} (+{v["current"] - v["initial"]}) | total hits: {v["total_hits"]}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
