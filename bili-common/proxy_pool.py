"""
公共代理池 — 免费代理源抓取与聚合

从 bili-booster/booster.py 抽出，供 booster 与其他模块（如 player）共用。
仅负责「获取一批候选代理」，不含探测与使用逻辑。
"""
# 兼容 Python 3.8 (Win7)：注解语法延迟求值
from __future__ import annotations

import re
from time import sleep
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.exceptions import RequestException

# 单个代理源的请求超时（秒）
timeout = 5

# 归一化：允许 "ip:port" 与 "http://ip:port" 两种写法，丢掉列表站的垃圾行
_PROXY_RE = re.compile(r'^(?:[a-zA-Z]+://)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})$')
_PRIVATE_PREFIXES = (0, 10, 127)     # 0.x 占位、10.x / 127.x 本机或内网


def normalize_proxy(raw):
    """把列表行规整成 ip:port；非法/内网/占位地址返回 None。"""
    m = _PROXY_RE.match((raw or '').strip())
    if not m:
        return None
    ip, port = m.group(1), int(m.group(2))
    octets = [int(x) for x in ip.split('.')]
    if any(x > 255 for x in octets) or octets[0] in _PRIVATE_PREFIXES:
        return None
    if octets[0] == 172 and 16 <= octets[1] <= 31:
        return None
    if octets[0] == 192 and octets[1] == 168:
        return None
    if not 1 <= port <= 65535:
        return None
    return f'{ip}:{port}'


# =========================================================================
#  一、代理源
# =========================================================================

def fetch_from_checkerproxy(log, min_count=100, max_lookback_days=7, union_days=3):
    """取「最近若干天」归档的并集（默认前 3 天）。

    单日列表已能过 min_count，但相邻日之间仍有不重叠的 IP；并入多日可抬高候选总量
    （扩池用），同时通过 union_days 限制回看天数，避免为凑数拉太多天。
    """
    collected = set()
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
            day_proxies = [p for p in proxies_obj if p]
        elif isinstance(proxies_obj, dict):
            day_proxies = [p for p in proxies_obj.values() if p]
        else:
            log(f'unexpected checkerproxy proxyList type: {type(proxies_obj)}')
            continue

        if len(day_proxies) < min_count:
            log(f'only have {len(day_proxies)} proxies from checkerproxy[{day}]')
            continue

        before = len(collected)
        collected.update(day_proxies)
        log(f'checkerproxy[{day}]: +{len(collected) - before} new (day total {len(day_proxies)})')
        if offset + 1 >= union_days:
            break

    if collected:
        log(f'successfully get {len(collected)} proxies from checkerproxy')
        return list(collected)
    return []


def fetch_from_geonode(log, limit=500, pages=3):
    """按 lastChecked 倒序翻页取前 pages 页（每页 limit 条），凑更多「较新」候选。"""
    proxy_url = 'https://proxylist.geonode.com/api/proxy-list'
    all_proxies = []
    for page in range(1, pages + 1):
        params = {
            'limit': limit,
            'page': page,
            'sort_by': 'lastChecked',
            'sort_type': 'desc',
            'protocols': 'http',
        }
        try:
            response = requests.get(proxy_url, params=params, timeout=timeout + 3)
            response.raise_for_status()
            data = response.json().get('data', [])
        except Exception as err:
            log(f'  geonode page {page} failed: {err}')
            break
        page_proxies = [f"{item['ip']}:{item['port']}"
                        for item in data if item.get('ip') and item.get('port')]
        if not page_proxies:
            break
        all_proxies.extend(page_proxies)
        log(f'  geonode page {page}: got {len(page_proxies)} proxies')
    log(f'successfully get {len(all_proxies)} proxies from geonode')
    return all_proxies


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


GITHUB_MIRRORS = (
    'https://ghproxy.net/https://raw.githubusercontent.com/{repo_path}',
    'https://cdn.jsdelivr.net/gh/{repo_path}',
)


def fetch_github_list(log, repo_path, label, req_timeout=12):
    """拉 GitHub 上的纯文本代理列表：先 ghproxy 镜像，再退 jsdelivr。

    raw.githubusercontent.com 在本机常被重置、jsdelivr 近期也频繁 10054，
    两个入口各试一次可明显提高 GitHub 类列表的可用率。
    """
    for tpl in GITHUB_MIRRORS:
        proxies = fetch_plaintext_proxy_list(
            log, tpl.format(repo_path=repo_path), label, req_timeout)
        if proxies:
            return proxies
    return []


def fetch_from_monosans(log):
    return fetch_github_list(log, 'monosans/proxy-list/main/proxies/http.txt', 'monosans mirror')


def fetch_from_proxifly(log):
    return fetch_github_list(
        log, 'proxifly/free-proxy-list/main/proxies/protocols/http/data.txt', 'proxifly mirror')


def fetch_from_proxyscrape(log, req_timeout=15):
    url = ('https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http'
           '&timeout=10000&country=all&ssl=all&anonymity=all')
    return fetch_plaintext_proxy_list(log, url, 'proxyscrape', req_timeout)


def fetch_from_proxy_list_download(log, req_timeout=15):
    return fetch_plaintext_proxy_list(
        log, 'https://www.proxy-list.download/api/v1/get?type=http',
        'proxy-list.download', req_timeout)


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


def fetch_from_openproxylist(log):
    """OpenProxyList：自有域名、分钟级校验，最接近 checkerproxy 的平替。"""
    all_proxies = []
    for kind in ('http', 'https'):
        all_proxies.extend(fetch_plaintext_proxy_list(
            log, f'https://api.openproxylist.xyz/{kind}.txt',
            f'openproxylist {kind}', timeout + 3))
    return all_proxies


def fetch_from_proxyspace(log):
    return fetch_plaintext_proxy_list(
        log, 'https://proxyspace.pro/http.txt', 'proxyspace', timeout + 3)


# =========================================================================
#  二、聚合
# =========================================================================

def get_total_proxies(log, stop_event=None):
    """并发从各代理源抓取并去重，任一源失败不影响其它源。

    各源同时抓（原来是串行），总耗时从「各源之和」压缩到「最慢一个源」。
    入库前统一 normalize_proxy，过滤占位/内网/格式不符的垃圾行。
    所有源都拿不到时抛 RuntimeError（保持与原 booster 行为一致）。
    """
    fetchers = [
        ('checkerproxy', fetch_from_checkerproxy),
        ('89ip', fetch_from_89ip),
        ('zdopen', fetch_from_zdopen),
        ('openproxylist', fetch_from_openproxylist),
        ('proxyspace', fetch_from_proxyspace),
        ('proxifly', fetch_from_proxifly),
        ('scdnio', fetch_from_scdnio),
        ('monosans', fetch_from_monosans),
        ('geonode', fetch_from_geonode),
        ('proxyscrape', fetch_from_proxyscrape),
        ('proxylistdownload', fetch_from_proxy_list_download),
        ('TheSpeedX', lambda log: fetch_github_list(
            log, 'TheSpeedX/PROXY-List/master/http.txt', 'TheSpeedX mirror')),
        ('ShiftyTR', lambda log: fetch_github_list(
            log, 'ShiftyTR/Proxy-List/master/http.txt', 'ShiftyTR mirror')),
        ('clarketm', lambda log: fetch_github_list(
            log, 'clarketm/proxy-list/master/proxy-list-raw.txt', 'clarketm mirror')),
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
                cleaned = normalize_proxy(proxy)
                if cleaned:
                    all_proxies.add(cleaned)
    if stop_event and stop_event.is_set():
        return []
    if all_proxies:
        log(f'collected {len(all_proxies)} proxies from available sources')
        return list(all_proxies)
    raise RuntimeError('failed to fetch proxies from all sources')
