"""
公共代理池 — 免费代理源抓取与聚合

从 bili-booster/booster.py 抽出，供 booster 与其他模块（如 player）共用。
仅负责「获取一批候选代理」，不含探测与使用逻辑。
"""
# 兼容 Python 3.8 (Win7)：注解语法延迟求值
from __future__ import annotations

from time import sleep
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.exceptions import RequestException

# 单个代理源的请求超时（秒）
timeout = 5


# =========================================================================
#  一、代理源
# =========================================================================

def fetch_from_checkerproxy(log, min_count=100, max_lookback_days=7):
    for offset in range(max_lookback_days):
        day = date.today() - timedelta(days=offset)
        proxy_url = f'https://checkerproxy.net/v1/landing/archive/{day.strftime("%Y-%m-%d")}'
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
#  二、聚合
# =========================================================================

def get_total_proxies(log, stop_event=None):
    """并发从各代理源抓取并去重，任一源失败不影响其它源。

    6 个源同时抓（原来是串行），总耗时从「各源之和」压缩到「最慢一个源」。
    所有源都拿不到时抛 RuntimeError（保持与原 booster 行为一致）。
    """
    fetchers = [
        ('checkerproxy', fetch_from_checkerproxy),
        ('89ip', fetch_from_89ip),
        ('zdopen', fetch_from_zdopen),
        ('scdnio', fetch_from_scdnio),
        ('monosans', fetch_from_monosans),
        ('geonode', fetch_from_geonode),
    ]
    if stop_event and stop_event.is_set():
        return []

    def _run(item):
        name, fetcher = item
        try:
            return name, fetcher(log)
        except RequestException as err:
            return name, RuntimeError(f'{name} source failed: {err}')
        except Exception as err:
            return name, RuntimeError(f'{name} source error: {err}')

    all_proxies = set()
    with ThreadPoolExecutor(max_workers=len(fetchers)) as pool:
        futures = {pool.submit(_run, item): item[0] for item in fetchers}
        for fut in as_completed(futures):
            name, result = fut.result()
            # 等待期间收到停止：结果只丢弃不并入（已发出的源请求无法中断，跑完即释放）
            if stop_event and stop_event.is_set():
                continue
            if isinstance(result, Exception):
                log(str(result))
                continue
            for proxy in result:
                all_proxies.add(proxy)
    if stop_event and stop_event.is_set():
        return []
    if all_proxies:
        log(f'collected {len(all_proxies)} proxies from available sources')
        return list(all_proxies)
    raise RuntimeError('failed to fetch proxies from all sources')
