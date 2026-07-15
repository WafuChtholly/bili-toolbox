import sys
import logging
import threading
import queue
import random
import hashlib
import time as time_module
from time import sleep
from typing import Optional
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.exceptions import RequestException
from fake_useragent import UserAgent

# 全局 UserAgent 实例，避免重复初始化（初始化非常慢）
_ua_instance = UserAgent()

logger = logging.getLogger("bili_booster")

class _StdoutHandler(logging.StreamHandler):
    """Always writes to the current sys.stdout (supports runtime redirection)."""
    def __init__(self):
        super().__init__()
    @property
    def stream(self):
        return sys.stdout
    @stream.setter
    def stream(self, value):
        pass


class _CallbackHandler(logging.Handler):
    """Routes log records to a callback function (for WebUI direct buffer write)."""
    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def emit(self, record):
        try:
            self._callback(self.format(record))
        except Exception:
            pass

if not logger.handlers:
    _handler = _StdoutHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [BOOSTER] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

timeout = 3
thread_num = 30
batch_size = 30
request_delay = 0.01
per_video_boost = 80
_max_concurrent_requests = 30  # 全局并发请求上限（进程级单例，所有视频共享）

# 全局信号量，挂在 threading 模块上实现进程级单例
# 因为 app.py 每次启动任务都会 importlib 重新加载本模块，模块级变量会被重置
# 用 threading 模块属性可以跨模块重载共享同一个信号量
if not hasattr(threading, '_bili_booster_semaphore'):
    threading._bili_booster_semaphore = threading.Semaphore(_max_concurrent_requests)
_net_semaphore = threading._bili_booster_semaphore


def fetch_from_checkerproxy(min_count: int = 100, max_lookback_days: int = 7) -> list[str]:
    day = date.today()
    for _ in range(max_lookback_days):
        day = day - timedelta(days=1)
        proxy_url = f'https://api.checkerproxy.net/v1/landing/archive/{day.strftime("%Y-%m-%d")}'
        logger.info(f'getting proxies from {proxy_url} ...')
        try:
            response = requests.get(proxy_url, timeout=timeout)
            response.raise_for_status()
        except RequestException as err:
            logger.info(f'checkerproxy unavailable: {err}')
            continue

        data = response.json()
        data_obj = data.get('data')
        if not data_obj:
            logger.info(f'checkerproxy has no data for {day.strftime("%Y-%m-%d")}')
            continue

        proxies_obj = data_obj.get('proxyList')
        if isinstance(proxies_obj, list):
            total_proxies = proxies_obj
        elif isinstance(proxies_obj, dict):
            total_proxies = [proxy for proxy in proxies_obj.values() if proxy]
        else:
            logger.info(f'unexpected checkerproxy proxyList type: {type(proxies_obj)}')
            continue

        if len(total_proxies) >= min_count:
            logger.info(f'successfully get {len(total_proxies)} proxies from checkerproxy')
            return total_proxies
        logger.info(f'only have {len(total_proxies)} proxies from checkerproxy')
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
    logger.info(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, params=params, timeout=timeout + 2)
    response.raise_for_status()
    data = response.json().get('data', [])
    proxies = [f"{item['ip']}:{item['port']}" for item in data if item.get('ip') and item.get('port')]
    logger.info(f'successfully get {len(proxies)} proxies from geonode')
    return proxies


def fetch_plaintext_proxy_list(url: str, label: str, req_timeout: int = 15) -> list[str]:
    logger.info(f'getting proxies from {url} ...')
    try:
        response = requests.get(url, timeout=req_timeout)
        response.raise_for_status()
        proxies = [line.strip() for line in response.text.splitlines() if line.strip() and ':' in line]
        logger.info(f'successfully get {len(proxies)} proxies from {label}')
        return proxies
    except Exception as err:
        logger.info(f'{label} failed: {err}')
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
            logger.info(f'  scdnio call {i+1}/{calls_needed}: got {len(proxies)} proxies')
        except Exception as err:
            logger.info(f'  scdnio call {i+1} failed: {err}')
            break
        if i < calls_needed - 1:
            sleep(0.5)
    logger.info(f'successfully get {len(all_proxies)} proxies from scdnio')
    return all_proxies


def fetch_from_89ip() -> list[str]:
    proxy_url = 'http://api.89ip.cn/tqdl.html?api=1&num=9999'
    logger.info(f'getting proxies from {proxy_url} ...')
    response = requests.get(proxy_url, timeout=timeout + 5)
    response.raise_for_status()
    text = response.text
    parts = text.split('<br>')
    proxies = []
    for part in parts:
        line = part.strip()
        if line and ':' in line and not line.startswith('<'):
            proxies.append(line)
    logger.info(f'successfully get {len(proxies)} proxies from 89ip')
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
            logger.info(f'  zdopen dalu={dalu}: got {len(proxy_list)} proxies')
        except Exception as err:
            logger.info(f'  zdopen dalu={dalu} failed: {err}')
        if dalu == 0:
            sleep(3)
    logger.info(f'successfully get {len(all_proxies)} proxies from zdopen')
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


def get_total_proxies(stop_event=None) -> list[str]:
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
        if stop_event and stop_event.is_set():
            return []
        try:
            proxies = fetcher()
        except RequestException as err:
            logger.info(f'{name} source failed: {err}')
            continue
        except Exception as err:
            logger.info(f'{name} source error: {err}')
            continue
        for proxy in proxies:
            all_proxies.add(proxy)
    if all_proxies:
        logger.info(f'collected {len(all_proxies)} proxies from available sources')
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


def probe_once(proxy: str, bv: str) -> bool:
    """轻量探测：通过代理请求视频信息接口，验证代理可用且不触发播放量。"""
    _net_semaphore.acquire()
    try:
        resp = requests.get(
            'https://api.bilibili.com/x/web-interface/view',
            params={'bvid': bv},
            proxies={'http': f'http://{proxy}'},
            headers={'User-Agent': _ua_instance.random},
            timeout=timeout)
        return resp.status_code == 200
    except:
        return False
    finally:
        _net_semaphore.release()


def click_once(proxy: str, info: dict, bv: str) -> bool:
    _net_semaphore.acquire()
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
    finally:
        _net_semaphore.release()


def click_batch(proxies: 'list[str]', info: dict, bv: str, max_workers: int = None) -> int:
    """批量点击，使用线程池限制并发数"""
    success = [0]
    lock = threading.Lock()
    worker_count = max_workers if max_workers else batch_size
    # 点击线程数取筛选的1/2，与筛选共享全局信号量
    worker_count = max(3, worker_count // 2)

    def worker(proxy: str) -> None:
        if click_once(proxy, info, bv):
            with lock:
                success[0] += 1

    with ThreadPoolExecutor(max_workers=min(worker_count, len(proxies))) as executor:
        executor.map(worker, proxies)

    return success[0]


def boost_video_once(video: dict, total_proxies: 'list[str]', batch_num: int, stop_event=None, max_threads: int = None) -> int:
    bv = video['bvid']
    info = video['info']

    random.shuffle(total_proxies)

    active_proxies = []
    count = [0]
    active_proxies_len_ref = [0]  # 用引用类型跟踪有效代理数，避免频繁 append 列表
    last_milestone = [0]
    lock = threading.Lock()
    total = len(total_proxies)
    effective_thread_num = max_threads if max_threads else thread_num
    
    # 使用队列实现边筛选边点击
    proxy_queue = queue.Queue()
    filter_done = threading.Event()

    def filter_worker(proxies: 'list[str]') -> None:
        local_count = 0
        local_active = 0
        for proxy in proxies:
            if stop_event and stop_event.is_set():
                # 退出前同步本地计数
                with lock:
                    count[0] += local_count
                    active_proxies_len_ref[0] += local_active
                return
            if probe_once(proxy, bv):
                proxy_queue.put(proxy)
                local_active += 1
            local_count += 1
            # 每检查 20 个代理才同步一次，大幅减少锁竞争
            if local_count % 20 == 0:
                with lock:
                    count[0] += local_count
                    active_proxies_len_ref[0] += local_active
                    local_count = 0
                    local_active = 0
                    # 打印进度
                    pct = int(count[0] * 100 / total)
                    milestone = (pct // 10) * 10
                    if milestone > last_milestone[0] and milestone <= 100:
                        last_milestone[0] = milestone
                        bar_len = milestone // 10
                        bar = '█' * bar_len + '░' * (10 - bar_len)
                        logger.info(f'    ⏳ [{bar}] {pct}% | 已检测: {count[0]}/{total} | 有效: {active_proxies_len_ref[0]}')
        # 处理剩余的本地计数
        if local_count > 0:
            with lock:
                count[0] += local_count
                active_proxies_len_ref[0] += local_active

    logger.info(f'  🔍 正在筛选代理 ({total} 个，使用 {effective_thread_num} 线程，超时 {timeout}秒)...')
    start_filter = datetime.now()
    
    # 启动筛选线程
    thread_proxy_num = max(1, total // effective_thread_num)
    filter_threads = []
    for i in range(effective_thread_num):
        if stop_event and stop_event.is_set():
            return 0
        start = i * thread_proxy_num
        end = start + thread_proxy_num if i < (effective_thread_num - 1) else None
        t = threading.Thread(target=filter_worker, args=(total_proxies[start:end],))
        t.start()
        filter_threads.append(t)
    
    # 同时开始点击，边筛选边点击
    logger.info(f'  👆 开始边筛选边点击...')
    start_click = datetime.now()
    clicks = 0
    batch_count = 0
    current_batch = []
    
    # 等待筛选完成
    while not filter_done.is_set():
        try:
            proxy = proxy_queue.get(timeout=0.5)
            current_batch.append(proxy)
            
            if len(current_batch) >= batch_size:
                batch_count += 1
                c = click_batch(current_batch, info, bv, max_workers=effective_thread_num)
                clicks += c
                
                if batch_count % 5 == 0:
                    logger.info(f'    📊 已完成 {batch_count} 批 | 累计成功: {clicks}')
                current_batch = []
                
                if request_delay > 0:
                    sleep(request_delay)
        except queue.Empty:
            pass
        
        if all(not t.is_alive() for t in filter_threads):
            filter_done.set()
    
    # 处理剩余的代理
    while not proxy_queue.empty():
        current_batch.append(proxy_queue.get())
    
    if current_batch:
        batch_count += 1
        c = click_batch(current_batch, info, bv, max_workers=effective_thread_num)
        clicks += c
    
    for t in filter_threads:
        t.join()
    
    filter_cost = int((datetime.now() - start_filter).total_seconds())
    click_cost = int((datetime.now() - start_click).total_seconds())
    logger.info(f'  ✅ 完成: {clicks} 次成功 | 有效代理: {len(active_proxies)}/{total} | 筛选耗时: {time_fmt(filter_cost)} | 点击耗时: {time_fmt(click_cost)}')

    return clicks


def main(bv_input, target_input, stop_event=None, log_fn=None):
    """启动播放量提升任务。"""
    # 如果提供了 log_fn（WebUI 场景），将 logger handler 切换为回调模式
    # 这样所有 logger.info() 调用都会直接写入对应的 task buffer，避免线程串台
    if log_fn is not None:
        logger.handlers.clear()
        _cb_handler = _CallbackHandler(log_fn)
        _cb_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(_cb_handler)

    bv_list = [bv.strip() for bv in bv_input.split(',') if bv.strip()]
    if not bv_list:
        logger.info('no valid video ids provided')
        return

    target = int(target_input)

    videos = []
    logger.info('')
    for raw_bv in bv_list:
        if stop_event and stop_event.is_set():
            logger.info('stopped by user')
            return
        logger.info(f'fetching info for {raw_bv}...')
        try:
            info = fetch_video_info(raw_bv)
            videos.append({
                'bvid': info['bvid'],
                'info': info,
                'initial': info['stat']['view'],
                'current': info['stat']['view'],
                'total_hits': 0,
            })
            logger.info(f'  {info["bvid"]}: initial views = {info["stat"]["view"]}')
        except Exception as e:
            logger.info(f'  failed: {e}')

    if not videos:
        logger.info('no videos available, exiting')
        return

    # ── 任务概览 ──
    logger.info('')
    logger.info('━' * 40)
    logger.info(f'🚀 播放量提升任务启动')
    logger.info(f'   模式: 速刷（不限速）')
    logger.info(f'   视频数: {len(videos)} | 目标: {target} | 最大轮次: 5')
    for i, v in enumerate(videos, 1):
        gap = target - v['initial']
        logger.info(f'   [{i}/{len(videos)}] {v["bvid"]} | 当前: {v["initial"]} | 差距: +{gap}')
    logger.info('━' * 40)
    logger.info('')

    round_num = 0
    max_rounds = 5
    no_growth_streak = 0

    while True:
        if stop_event and stop_event.is_set():
            logger.info('stopped by user')
            break

        # 找出还未达标的视频
        pending = [v for v in videos if v['current'] < target]
        if not pending:
            break

        round_num += 1
        if round_num > max_rounds:
            logger.info(f'max rounds ({max_rounds}) reached, stopping')
            break

        # snapshot views before this round
        views_before = {v['bvid']: v['current'] for v in videos}

        logger.info('')
        logger.info(f'━━━ 🔄 第 {round_num}/{max_rounds} 轮 | 待提升: {len(pending)}/{len(videos)} 个视频 ━━━')

        try:
            total_proxies = get_total_proxies(stop_event=stop_event)
        except Exception as e:
            logger.info(f'failed to fetch proxies: {e}')
            sleep(5)
            continue

        # 使用线程池并发处理多个视频
        # 计算每个视频可用的线程数，避免总线程数过多导致系统卡死
        num_pending = len(pending)
        # 外层最多同时处理 3 个视频，避免线程数叠加
        max_concurrent_videos = min(num_pending, 3)
        threads_per_video = max(10, thread_num // max(num_pending, 1))  # 每个视频至少 10 线程

        def boost_single_video(v, idx):
            """并发提升单个视频"""
            if stop_event and stop_event.is_set():
                return None

            gap = target - v['current']
            logger.info(f'')
            logger.info(f'  📌 [{idx}/{num_pending}] {v["bvid"]} | 当前: {v["current"]} | 目标: {target} | 差距: +{gap}')
            hits = boost_video_once(v, total_proxies, round_num, stop_event=stop_event, max_threads=threads_per_video)
            v['total_hits'] += hits

            try:
                fresh = fetch_video_info(v['bvid'])
                v['current'] = fresh['stat']['view']
                growth = v['current'] - v['initial']
                new_gap = target - v['current']
                status = '✅' if v['current'] >= target else '📈'
                logger.info(f'  {status} 结果: {v["current"]} (+{growth}) | 点击: {hits} | 剩余差距: +{max(0, new_gap)}')
            except Exception as e:
                logger.info(f'  ⚠️ 查询播放量失败: {e}')

            # 每个视频处理完后输出 [RESULT] 标记供前端解析
            logger.info(f'  [RESULT] video|{v["bvid"]}|{v["current"]}|{v["current"] - v["initial"]}|{v["total_hits"]}|{max(0, target - v["current"])}')

            return v

        # 使用线程池并发执行，最多同时处理 max_concurrent_videos 个视频
        with ThreadPoolExecutor(max_workers=max_concurrent_videos) as executor:
            futures = {
                executor.submit(boost_single_video, v, idx): v
                for idx, v in enumerate(pending, 1)
            }
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    logger.info('stopped by user')
                    break
                try:
                    future.result()
                except Exception as e:
                    logger.info(f'  ⚠️ 视频处理异常: {e}')

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
            if no_growth_streak >= 2:
                logger.info(f'  ⚠️ 连续 {no_growth_streak} 轮无增长，停止')
                break

        # 输出 [RESULT] 轮次汇总标记供前端渲染
        total_hits = sum(v['total_hits'] for v in videos)
        total_growth = sum(v['current'] - v['initial'] for v in videos)
        reached = sum(1 for v in videos if v['current'] >= target)
        logger.info(f'  [RESULT] round|{round_num}|{len(videos)}|{reached}|{total_hits}|{total_growth}')

    # ── 结果汇总 ──
    total_clicks_all = sum(v['total_hits'] for v in videos)
    total_growth_all = sum(v['current'] - v['initial'] for v in videos)
    reached_target = sum(1 for v in videos if v['current'] >= target)

    logger.info('')
    logger.info('━' * 40)
    logger.info('📊 执行结果汇总')
    logger.info('━' * 40)
    logger.info(f'🎯 目标播放量: {target}')
    logger.info(f'🔄 完成轮次: {round_num}')
    logger.info(f'📹 视频数量: {len(videos)}')
    logger.info(f'👆 总点击数: {total_clicks_all}')
    logger.info(f'📈 总播放增长: +{total_growth_all}')
    logger.info(f'✅ 达标视频: {reached_target}/{len(videos)}')
    logger.info('┄' * 40)
    logger.info('📋 各视频详情:')
    for v in videos:
        growth = v['current'] - v['initial']
        status = '✅' if v['current'] >= target else '❌'
        logger.info(f'  {status} {v["bvid"]}: {v["initial"]} → {v["current"]} (+{growth}) | 点击: {v["total_hits"]}')
    logger.info(f'🕐 结束时间: {datetime.now().strftime("%H:%M:%S")}')
    logger.info('━' * 40)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
