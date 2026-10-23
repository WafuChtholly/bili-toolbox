"""
B站播放量提升 — 核心模块
完全基于 bili-booster-webui 逻辑，仅增加 stop_event 支持任务取消。
输出通过 log_fn 注入：app.py 传入按任务绑定的 logger，避免多任务并发时日志串台。"""
import os
import sys
import threading
import random
import hashlib
import time as time_module
from time import sleep
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests
from fake_useragent import UserAgent

# 公共代理池位于 bili-common/；该目录无 app.py，加入 sys.path 不会与主程序冲突
_COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bili-common")
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)
from proxy_pool import get_total_proxies  # noqa: E402

# ── 全局配置（与 webui 完全一致） ──
timeout = 5
probe_connect_timeout = 2  # 探测 TCP 连接超时：死代理多在连接阶段失败，设短以快速淘汰
probe_timeout = 3   # 代理存活探测读取超时（只等响应头、不读 body，慢代理也能快速判定）
thread_num = 300    # 单任务筛选线程数上限（HTTP 探针无 TLS 握手开销，可安全提高；实际取 min(thread_num, 候选数)）
click_thread_num = 60   # 单任务点击并发上限：替代「每个代理一个线程」，压低多任务并发时的线程峰值
batch_size = 200
request_delay = 0.01
per_video_boost = 80
# 探测结果缓存 TTL：存活结果短 TTL（同轮多稿件共享 / 后续轮次免探）；死亡结果长 TTL（死代理短期内不会复活）
probe_cache_alive_ttl = 300
probe_cache_dead_ttl = 1800
probe_cache_max = 20000  # 缓存条目上限，超出先清过期项、仍超则整体丢弃，防止长跑无限增长

# ── 全局 UserAgent 实例 ──
_ua_instance = UserAgent()

# ── 移动端通道（click/h5）：与网页端 click/web/h5 是两份独立额度 ──
# 每个存活代理会「先打 web 再打 mobile」，两份额度共用同一次存活探测，避免重复验证。
_mobile_ua = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 '
              'Mobile/15E148 Safari/604.1')
_mobile_click_url = 'http://api.bilibili.com/x/click-interface/click/h5'

# ── PC 客户端通道（x/report/click/pc/client）：自签名，web / mobile / pc 三份独立额度 ──
# 实测口径：一个 IP 每轮只打一次；不带 buvid4（共用 buvid4 会触发设备级去重，计数率 43% vs 100%）；
# did 留空、每次请求新 buvid3（同 buvid 复用 rpdid 会被去重）。明文 HTTP 便于走代理免 TLS 隧道。
_pc_click_url = 'http://api.bilibili.com/x/report/click/pc/client'
_pc_ua = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) bilibili_pc/1.19.0 Chrome/108.0.5359.215 '
          'Electron/22.3.27 Safari/537.36 build/1001019000')
_pc_appkey = 'aa1e74ee4874176e'
_pc_appsec = '54e6a9a31b911cd5fc0daa66ebf94bc4'
_pc_build = '1001019000'


# 代理池实现已抽到 bili-common/proxy_pool.py，本模块只保留探测与使用逻辑

# =========================================================================
#  一、工具函数（与 webui 完全一致）
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


_VIEW_URL = 'https://api.bilibili.com/x/web-interface/view'
_WBI_VIEW_URL = 'https://api.bilibili.com/x/web-interface/wbi/view'
# 机房 IP 只带 UA 直连 view 接口极易被 WAF 判 412，固定桌面 Chrome UA 并补全 Referer/Cookie，
# 模拟正常网页内 XHR 请求。
_VIEW_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
# WBI 签名用的重排表（img_key + sub_key 取前 32 位）
_WBI_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
                  33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
                  61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62,
                  11, 36, 20, 34, 44, 52]
_wbi_key_cache = {'key': '', 'ts': 0}


def _build_view_headers(cookie):
    cookies = build_random_cookies()
    if cookie:
        # 账号 Cookie（SESSDATA 等）能显著降低机房 IP 被 WAF 判 412 的概率
        cookies = f'{cookie}; {cookies}'
    return {
        'User-Agent': _VIEW_UA,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://www.bilibili.com/',
        'Origin': 'https://www.bilibili.com',
        'Cookie': cookies,
    }


def _fetch_view_payload(params, cookie=None):
    response = requests.get(_VIEW_URL, params=params, headers=_build_view_headers(cookie), timeout=timeout + 2)
    response.raise_for_status()
    return response.json()


def _wbi_mixin_key():
    """取 WBI 签名密钥（img_key + sub_key），进程内缓存 1 小时。"""
    now = time_module.time()
    if _wbi_key_cache['key'] and now - _wbi_key_cache['ts'] < 3600:
        return _wbi_key_cache['key']
    resp = requests.get('https://api.bilibili.com/x/web-interface/nav',
                        headers=_build_view_headers(None), timeout=timeout + 2)
    resp.raise_for_status()
    wbi_img = (resp.json().get('data') or {}).get('wbi_img') or {}
    img_key = wbi_img['img_url'].rsplit('/', 1)[-1].split('.')[0]
    sub_key = wbi_img['sub_url'].rsplit('/', 1)[-1].split('.')[0]
    raw = img_key + sub_key
    key = ''.join(raw[i] for i in _WBI_MIXIN_TAB)[:32]
    _wbi_key_cache.update(key=key, ts=now)
    return key


def _fetch_wbi_view_payload(params, cookie=None):
    """走 WBI 签名版 view 接口：空间等 WBI 接口在机房 IP 上可用，用作被 WAF 拦时的备用通道。"""
    signed = dict(params)
    signed['wts'] = str(int(time_module.time()))
    query = '&'.join(f'{k}={signed[k]}' for k in sorted(signed))
    signed['w_rid'] = hashlib.md5((query + _wbi_mixin_key()).encode()).hexdigest()
    response = requests.get(_WBI_VIEW_URL, params=signed, headers=_build_view_headers(cookie), timeout=timeout + 2)
    response.raise_for_status()
    return response.json()


def _is_risk_control(error):
    """被风控（HTTP 412/403/429 或风控 code）时才值得换通道；BV 不存在之类的错误换通道无用。"""
    resp = getattr(error, 'response', None)
    if resp is not None and resp.status_code in (403, 412, 429):
        return True
    return getattr(error, 'code', None) in (-352, -412, -509, -799)


def _parse_view_payload(payload):
    if payload.get('code') != 0 or 'data' not in payload:
        msg = payload.get('message', 'unknown error')
        err = RuntimeError(f'bilibili API error: code={payload.get("code")} message={msg}')
        err.code = payload.get('code')
        raise err
    data = payload['data']
    if not data.get('aid') or not data.get('bvid'):
        raise RuntimeError('video info missing key identifiers')
    return data


def fetch_video_info(video_id, cookie=None):
    """拉取稿件详情（含 stat.view / aid / cid）。

    只走直连，按通道依次降级：
    1) 普通 view 接口（带浏览器头 + 账号 Cookie）；
    2) 被 WAF 判 412 时改用 WBI 签名版 view 接口（空间等 WBI 接口在机房 IP 上可用）。
    免费代理池仅做过 HTTP 可达性探测、基本不具备 HTTPS CONNECT 能力，因此不再走代理取数。
    两条通道都失败时由调用方决定降级（如用扫描阶段已知的播放数）。
    """
    params = build_view_params(video_id)
    try:
        return _parse_view_payload(_fetch_view_payload(params, cookie))
    except Exception as direct_error:
        if not _is_risk_control(direct_error):
            raise
        try:
            return _parse_view_payload(_fetch_wbi_view_payload(params, cookie))
        except Exception as wbi_error:
            raise RuntimeError(f'view 接口不可用（{direct_error}），WBI 接口也失败（{wbi_error}）')


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
#  二、核心：筛选 + 点击（与 webui 完全一致）
# =========================================================================

def probe_once(proxy, bv):
    """轻量探测：GET 请求验证代理可用，不触发播放量。使用 HTTP 避免 HTTPS 隧道的高 CPU 开销。

    stream=True 只等响应头到达即判定，不下载 body（慢代理的读体时间也省掉）；
    (connect, read) 分别设超时，死代理在 TCP 连接阶段就被快速淘汰。
    """
    try:
        resp = requests.get(
            'http://api.bilibili.com/x/web-interface/view',
            params={'bvid': bv},
            proxies={'http': f'http://{proxy}'},
            headers={'User-Agent': _ua_instance.random},
            timeout=(probe_connect_timeout, probe_timeout),
            allow_redirects=False,
            stream=True)
        try:
            return resp.status_code in (200, 301, 302, 307, 308)
        finally:
            resp.close()
    except:
        return False


def prune_probe_cache(cache, now_ts=None):
    """探测缓存瘦身：先删过期项，仍超上限则整体清空（调用方共享缓存长跑时防止无限增长）。"""
    if now_ts is None:
        now_ts = time_module.time()
    if len(cache) < probe_cache_max:
        return
    for proxy, (alive, ts) in list(cache.items()):
        ttl = probe_cache_alive_ttl if alive else probe_cache_dead_ttl
        if now_ts - ts > ttl:
            cache.pop(proxy, None)
    if len(cache) >= probe_cache_max:
        cache.clear()


def probe_cached(proxy, bv, cache, now_ts=None):
    """带缓存的存活探测，返回 (alive, from_cache)。

    cache 结构 {proxy: (alive, timestamp)}，与稿件无关（只验证代理能否连通 bilibili），
    因此可跨稿件、跨轮次共享：存活 TTL 内直接采信（点击时若代理恰好已死，click 自身会失败，
    无副作用）；死亡 TTL 内直接跳过，避免对同一批死代理反复打满超时。
    """
    if now_ts is None:
        now_ts = time_module.time()
    entry = cache.get(proxy)
    if entry is not None:
        alive, ts = entry
        ttl = probe_cache_alive_ttl if alive else probe_cache_dead_ttl
        if now_ts - ts <= ttl:
            return alive, True
    alive = probe_once(proxy, bv)
    cache[proxy] = (alive, now_ts)
    return alive, False


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


def build_mobile_click_headers(bv, cookies):
    return {
        'User-Agent': _mobile_ua,
        'Referer': f'https://m.bilibili.com/video/{bv}',
        'Origin': 'https://m.bilibili.com',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Cookie': cookies,
    }


def generate_did():
    """移动端设备号 did：8 位小写字母+数字（实测形如 elrglb04）。"""
    return ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))


def click_mobile_once(proxy, info, bv):
    """发送一次移动端播放点击请求（click/h5）。

    与 click_once（网页端）共用同一条存活代理，构成「一个 IP 打两份独立额度」。
    payload 复刻实测可计数的移动端匿名会话：did/lv=0/ftime/stime/spmid=333.401。
    """
    try:
        cookies = build_random_cookies()
        headers = build_mobile_click_headers(bv, cookies)
        proxies = {'http': f'http://{proxy}'}

        # ftime/stime 用本地时间即可（省去 report/click/now 的一次额外请求）
        now = int(time_module.time())

        resp = requests.post(_mobile_click_url,
                             proxies=proxies,
                             headers=headers,
                             timeout=timeout,
                             data={
                                 'aid': info['aid'],
                                 'cid': info['cid'],
                                 'bvid': bv,
                                 'part': '1',
                                 'did': generate_did(),
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
        return resp.status_code == 200
    except:
        return False


def build_pc_payload(aid, cid, buvid):
    """PC 客户端通道的 payload（字段与官方 electron 客户端一致，did 留空）。"""
    now = int(time_module.time())
    return {
        'aid': str(aid), 'cid': str(cid), 'part': '1', 'lv': '0',
        'ftime': str(now), 'stime': str(now),
        'type': '3', 'sub_type': '0', 'refer_url': '', 'outer': '0',
        'statistics': '{"appId":112,"platform":4}',
        'mobi_app': 'pc_electron',
        'device': 'win', 'platform': 'web', 'cur_language': '', 'perfer_type': '',
        'play_mode': '1', 'spmid': 'main.play-detail.0.0.pv', 'from_spmid': '',
        'session': hashlib.md5(f'{random.random()}{time_module.time()}'.encode()).hexdigest(),
        'extra': '{"from_outer_spmid":""}',
        'build': _pc_build, 'buvid': buvid, 'did': '',
        'auto_play': '0', 'track_id': '', 'playlist_type': '', 'playlist_id': '',
    }


def build_pc_body(payload):
    """PC 通道签名：md5(sorted_query + appsec)，body 保持 payload 插入序 + appkey/build/ts + sign。"""
    params = dict(payload)
    params['appkey'] = _pc_appkey
    params['ts'] = str(int(time_module.time() * 1000))
    sign_str = '&'.join(f'{k}={quote(str(v), safe="")}' for k, v in sorted(params.items()))
    sign = hashlib.md5((sign_str + _pc_appsec).encode()).hexdigest()
    body = '&'.join(f'{k}={quote(str(v), safe="")}' for k, v in params.items())
    return f'{body}&sign={sign}'


def build_pc_headers(buvid):
    return {
        'User-Agent': _pc_ua,
        'Origin': 'https://www.bilibili.com',
        'Referer': 'https://www.bilibili.com/client',
        'Content-Type': 'application/x-www-form-urlencoded',
        'x-app-version': '1.19.0',
        'buvid': buvid,
        'Cookie': f'buvid3={buvid}',
    }


def click_pc_once(proxy, info, bv):
    """发送一次 PC 客户端播放点击（本地自签名，无需打开官方客户端）。

    与 web / mobile 共用同一条存活代理，构成「一个 IP 打三份独立额度」。
    """
    try:
        buvid = generate_buvid3()
        body = build_pc_body(build_pc_payload(info['aid'], info['cid'], buvid))
        resp = requests.post(_pc_click_url,
                             proxies={'http': f'http://{proxy}'},
                             headers=build_pc_headers(buvid),
                             data=body.encode(),
                             timeout=timeout)
        return resp.status_code == 200
    except:
        return False


def click_batch(proxies, info, bv, stop_event=None):
    """批量并发点击：每个存活代理依次打网页端、移动端、PC 端（共用这一批存活代理）。

    并发上限 click_thread_num：不再「每个代理一个线程」，避免批量大时线程数被顶到 batch_size。
    返回 (web_success, mobile_success, pc_success)。
    """
    web_ok = [0]
    mobile_ok = [0]
    pc_ok = [0]
    lock = threading.Lock()

    def worker(proxy):
        if stop_event and stop_event.is_set():
            return
        if click_once(proxy, info, bv):
            with lock:
                web_ok[0] += 1
        if stop_event and stop_event.is_set():
            return
        if click_mobile_once(proxy, info, bv):
            with lock:
                mobile_ok[0] += 1
        if stop_event and stop_event.is_set():
            return
        if click_pc_once(proxy, info, bv):
            with lock:
                pc_ok[0] += 1

    if not proxies:
        return 0, 0, 0
    workers = max(1, min(int(click_thread_num), len(proxies)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, proxies))

    return web_ok[0], mobile_ok[0], pc_ok[0]


def boost_video_once(video, total_proxies, batch_num, log, stop_event=None, probe_cache=None):
    """对单个视频执行一轮筛选+点击（webui 原始逻辑 + 探测缓存）。存活代理全部保留使用，不做早停。"""
    bv = video['bvid']
    info = video['info']

    proxies = list(total_proxies)
    random.shuffle(proxies)
    total = len(proxies)

    if probe_cache is None:
        probe_cache = {}
    prune_probe_cache(probe_cache)

    active_proxies = []
    state_lock = threading.Lock()
    processed = [0]
    cache_reused = [0]

    def filter_worker(proxy):
        if stop_event and stop_event.is_set():
            return
        alive, from_cache = probe_cached(proxy, bv, probe_cache)
        with state_lock:
            processed[0] += 1
            n = processed[0]
            if from_cache:
                cache_reused[0] += 1
            if alive:
                active_proxies.append(proxy)
            valid = len(active_proxies)
            if n % 50 == 0 or n == total:
                log(f'\r[PROGRESS] {pbar(n, total)} {100*n/total:.1f}% '
                    f'[valid: {valid}, cached: {cache_reused[0]}]   ')

    log(f'  filtering {total} proxies for {bv}...')
    start_filter = datetime.now()
    if not proxies:
        log('  no proxies available, skipping')
        return 0
    # ThreadPoolExecutor 复用线程；候选不足 thread_num 时按候选数收敛
    filter_threads = max(1, min(thread_num, total))
    with ThreadPoolExecutor(max_workers=filter_threads) as pool:
        list(pool.map(filter_worker, proxies))
    filter_cost = int((datetime.now() - start_filter).total_seconds())
    log(f'\n  filtered {len(active_proxies)} valid proxies using {time_fmt(filter_cost)} '
        f'(cache reused: {cache_reused[0]})')

    if not active_proxies:
        log('  no valid proxies, skipping')
        return 0

    log(f'  sending clicks (web + mobile + pc per proxy) for {bv}...')
    start_click = datetime.now()
    web_clicks = 0
    mobile_clicks = 0
    pc_clicks = 0
    for i in range(0, len(active_proxies), batch_size):
        if stop_event and stop_event.is_set():
            break
        chunk = active_proxies[i:i + batch_size]
        w, m, p = click_batch(chunk, info, bv, stop_event=stop_event)
        web_clicks += w
        mobile_clicks += m
        pc_clicks += p
        log(f'    chunk {i//batch_size + 1}/{(len(active_proxies)-1)//batch_size + 1}: '
            f'web {w}/{len(chunk)}, mobile {m}/{len(chunk)}, pc {p}/{len(chunk)}')
        if request_delay > 0:
            sleep(request_delay)
    click_cost = int((datetime.now() - start_click).total_seconds())
    log(f'  done: web {web_clicks} + mobile {mobile_clicks} + pc {pc_clicks} clicks in {time_fmt(click_cost)}')

    return web_clicks + mobile_clicks + pc_clicks


# =========================================================================
#  三、主入口（webui 原始循环 + log_fn / stop_event / logger_name）
# =========================================================================

def _tagged_logger(log, tag):
    """给单稿日志加 [BV] 前缀。

    同一任务内多个稿件是并发线程执行的，日志天然交错，靠前缀区分归属；
    进度条（以 \\r 开头、行内覆盖）不加前缀，避免破坏覆盖行为。
    """
    def _log(msg):
        text = str(msg)
        if text.startswith('\r'):
            log(text)
        elif text.startswith('\n'):
            log('\n' + f'[{tag}] ' + text[1:])
        else:
            log(f'[{tag}] {text}')
    return _log


def _boost_single_video(v, total_proxies, round_num, target, log, stop_event, video_lock, probe_cache=None, cookie=None):
    """单个视频的 boost 任务，供多视频并发调用。"""
    if stop_event and stop_event.is_set():
        return
    log = _tagged_logger(log, v['bvid'])
    if v['current'] >= target:
        log(f'  {v["bvid"]} already at target ({v["current"]}), skipping')
        return

    log(f'--- boosting {v["bvid"]} (current: {v["current"]}, target: {target}) ---')
    hits = boost_video_once(v, total_proxies, round_num, log, stop_event=stop_event, probe_cache=probe_cache)
    with video_lock:
        v['total_hits'] += hits

    try:
        fresh = fetch_video_info(v['bvid'], cookie=cookie)
        with video_lock:
            v['current'] = fresh['stat']['view']
            v['verified'] = True
        gap = max(0, target - v['current'])
        log(f'  {v["bvid"]} views: {v["current"]} (+{v["current"] - v["initial"]})')
        log(f'[RESULT] video|{v["bvid"]}|{v["current"]}|{v["current"] - v["initial"]}|{v["total_hits"]}|{gap}')
    except Exception as e:
        with video_lock:
            v['verified'] = False
        log(f'  无法刷新播放数（{e}），沿用 {v["current"]}')


def main(bv_input, target_input, stop_event=None, max_rounds=5, refetch_proxies=True, proxies=None, probe_cache=None, log_fn=None, cookie=None):
    """启动播放量提升任务。完全基于 bili-booster-webui 的 main() 循环逻辑。
    max_rounds 为最大轮数，传 0 或 None 表示无上限。
    refetch_proxies 为 True 时每轮重新获取代理，为 False 时只在启动时获取一次并复用。
    proxies 为外部注入的代理快照（定时任务一轮内多个稿件共用同一份）：提供时直接复用，
    不再自行拉取代理池（等价于 refetch_proxies=False）。
    probe_cache 为外部注入的探测结果缓存（跨稿件/跨轮共享）：不传则本任务内部新建，
    至少保证同一任务的多轮之间不重复探测。
    log_fn 为外部注入的日志函数：并发任务各自传入绑定到自己缓冲区的 log，
    避免靠切换全局 sys.stdout 捕获日志（多任务会互相抢占，导致日志串台）。
    cookie 为账号 Cookie 头（SESSDATA 等）：view 接口带登录态可降低机房 IP 被 WAF 判 412 的概率。"""
    log = log_fn or print

    if probe_cache is None:
        probe_cache = {}

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
            info = fetch_video_info(raw_bv, cookie=cookie)
        except Exception as e:
            log(f'  failed: {e}')
            continue
        videos.append({
            'bvid': info['bvid'],
            'info': info,
            'initial': info['stat']['view'],
            'current': info['stat']['view'],
            'total_hits': 0,
            'verified': True,
        })
        gap = max(0, target - info['stat']['view'])
        log(f'  {info["bvid"]}: initial views = {info["stat"]["view"]}')
        log(f'[RESULT] video|{info["bvid"]}|{info["stat"]["view"]}|0|0|{gap}')

    if not videos:
        log('no videos available, exiting')
        return

    round_num = 0
    no_growth_streak = 0
    total_proxies = []

    if proxies is not None:
        # 共享快照：由调用方（如定时任务）一次性拉取后注入，多个稿件共用，避免各自重复拉取 6 源代理池
        total_proxies = list(proxies)
        if not total_proxies:
            log('代理列表为空，无法启动')
            return
        refetch_proxies = False
        log(f'使用共享代理快照（共 {len(total_proxies)} 个），所有轮次复用')
    elif not refetch_proxies:
        try:
            total_proxies = get_total_proxies(log, stop_event=stop_event)
        except Exception as e:
            log(f'failed to fetch proxies: {e}')
        if not total_proxies:
            log('代理列表为空，无法启动')
            return
        log(f'代理仅获取一次（共 {len(total_proxies)} 个），所有轮次复用')

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
        if max_rounds and round_num > max_rounds:
            log(f'\nmax rounds ({max_rounds}) reached, stopping')
            break

        # snapshot views before this round
        views_before = {v['bvid']: v['current'] for v in videos}

        round_label = f'{round_num}/{max_rounds}' if max_rounds else f'{round_num} (unlimited)'
        log(f'\n========== ROUND {round_label} ==========')

        if refetch_proxies:
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
        for v in pending:
            v['verified'] = False
        threads = []
        for v in pending:
            if stop_event and stop_event.is_set():
                break
            t = threading.Thread(target=_boost_single_video, args=(v, total_proxies, round_num, target, log, stop_event, video_lock, probe_cache, cookie))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # check if any video grew this round
        # 只统计本轮成功刷新到播放数的稿件：view 接口长期 412 时无法判断增长，
        # 不能据此提前停止（否则刷量还在跑就被「no growth」判定结束）
        verified = [v for v in videos if v.get('verified')]
        any_growth = any(v['current'] > views_before[v['bvid']] for v in verified)
        if not verified:
            no_growth_streak = 0
            log('\n  warning: view 接口不可用，本轮无法确认播放增长，跳过停止判定')
        elif any_growth:
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
    cli_max_rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    cli_refetch = sys.argv[4] != '0' if len(sys.argv) > 4 else True
    main(sys.argv[1], sys.argv[2], max_rounds=cli_max_rounds, refetch_proxies=cli_refetch)
