# -*- coding: utf-8 -*-
"""车机（car player）匿名播放计数通道 —— 长线运行。

实测结论（2026-10-23 复测，记录见 docs/播放计数通道研究-PC增强包评估.md 第八轮）：
  - 匿名车机 click（AES-256-CBC 加密体）+ heartbeat/mobile 三次心跳，可稳定记 1 次播放；
  - 额度按 **出口 IP** 立：单 IP 每 ~85s 窗口只计 1 次，与同时开多少设备会话无关
    （同 IP 并发 5 / 10 个新设备会话均只 +1；同 IP 间隔 112s 串行两个会话 -> +2）；
  - 换 IP 才能再拿一份额度，故吞吐 = IP 数 × 42/h；本机单 IP 就是 42/h；
  - 旧结论「额度按 aid 立、窗口 65~85s、与 IP 无关」已被上述复测证伪
    （当年观测到的同 IP +2 是上一批计数的延迟回补污染）。

本模块按「一稿一线程、start-to-start 85s 节拍」长期运行，对单个 IP 已是上限；
想放量只能扩 IP。车机端点全为 https，只有 CONNECT 可用的代理才能用，免费池里这类
出口极少（约 1/500），所以由 ProxyVault 在后台低频探活补货（实验性功能）；仓里没货时
自动走直连，主循环不受影响。

输出统一走 log 回调（默认 print），由 app.py 写入任务日志缓冲。
"""
import hashlib
import os
import random
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests

try:
    requests.packages.urllib3.disable_warnings()
except Exception:
    pass

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
except ImportError:  # 打包运行时只带 pycryptodomex（模块名 Cryptodome）
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import pad

_COMMON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'bili-common')
if _COMMON_DIR not in sys.path:
    sys.path.insert(0, _COMMON_DIR)
try:
    from proxy_pool import get_total_proxies
except Exception:  # 打包缺失时退化为纯直连
    get_total_proxies = None

CAR_CLICK = 'https://api.bilibili.com/x/report/click/android2'
HB_MOBILE = 'https://api.bilibili.com/x/report/heartbeat/mobile'
AES_KEY = b'fd6b639dbcff0c2a1b03b389ec763c4b'
AES_IV = b'77b07a672d57d64c'
SIGN_SALT = '9cafa6466a028bfb'
APPKEY = '1d8b6e7d45233436'
APPSEC = '560c52ccd288fed045859ed18bffd973'
CAR_UA = 'Mozilla/5.0 BiliTV/1.8.9'
HB_UA = 'Mozilla/5.0 BiliDroid/7.66.0 (bbcallen@gmail.com) os/android'
TIMEOUT = 10

DEFAULT_PLAY_S = 20
DEFAULT_INTERVAL = 85      # 窗口 65~85s，留足余量
MIN_INTERVAL = 70

# ── 免费代理池 ────────────────────────────────────────────────────────
# 车机端点全 https，需要 CONNECT 能力的代理；免费池里命中率约 1/500，
# 因此只在后台异步探活，主循环取不到就直连，绝不阻塞 85s 节拍。
PROXY_ENABLED = True
PROXY_MIN_READY = 3          # 可用出口低于该水位即后台补货
PROXY_MAX_READY = 8          # 仓内最多囤几个
PROXY_PROBE_WORKERS = 80     # 并发探活线程数
PROXY_ALIVE_TTL = 600        # 探活成功后多久内可直接取用
PROXY_DEAD_TTL = 1800        # 探活/使用失败后的冷却期
PROXY_PER_IP_LIMIT = 180     # 同一出口跑满这么多次就轮换（180×85s ≈ 4.2h）
PROXY_NO_GAIN_LIMIT = 6      # 同一出口连续这么多轮不计播放就判死（机房 IP 常见，约 8.5 分钟）
PROXY_TIMEOUT = 5            # 走代理时连接超时（免费代理挂掉的多，别死等 TIMEOUT）
PROBE_CONNECT_TIMEOUT = 3
PROBE_READ_TIMEOUT = 5
PROXY_PROBE_URL = 'https://api.bilibili.com/x/web-interface/nav'


# ── 指纹 ──────────────────────────────────────────────────────────────
def generate_buvid3():
    digest = hashlib.md5(f'{random.random()}{time.time()}'.encode()).hexdigest()
    return f'{digest[:8]}-{digest[8:12]}infoc'


def generate_buvid4():
    ts = int(time.time() * 1000)
    digest = hashlib.md5(f'{random.randint(0, 99999)}{ts}{random.random()}'.encode()).hexdigest()
    return f'{ts}-{digest[:32]}'


def generate_did():
    return ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))


def _cookies(buvid, buvid4):
    return f'buvid3={buvid}; buvid4={buvid4}; b_nut={int(time.time())}'


# ── 代理池 ────────────────────────────────────────────────────────────
_vault_lock = threading.Lock()
_vault = None
_vault_refs = 0


def _acquire_vault(log):
    """同一进程内多个车机任务共享一个补货仓，避免各自重复拉源探活。"""
    global _vault, _vault_refs
    with _vault_lock:
        if _vault is None:
            _vault = ProxyVault(log)
            _vault.start()
        _vault_refs += 1
        return _vault


def _release_vault():
    global _vault, _vault_refs
    with _vault_lock:
        _vault_refs = max(0, _vault_refs - 1)
        if _vault_refs == 0 and _vault is not None:
            _vault.stop()
            _vault = None


def _proxy_map(proxy):
    url = f'http://{proxy}'
    return {'http': url, 'https': url}


def probe_https(proxy):
    """探活：走 CONNECT 拿 bilibili nav，能拿到 JSON 才算真可用（排除只回 200 的劫持页）。"""
    try:
        r = requests.get(PROXY_PROBE_URL, proxies=_proxy_map(proxy),
                         timeout=(PROBE_CONNECT_TIMEOUT, PROBE_READ_TIMEOUT),
                         headers={'User-Agent': CAR_UA}, verify=False,
                         stream=True, allow_redirects=False)
        try:
            if r.status_code != 200:
                return False
            data = r.json()
        finally:
            r.close()
        return isinstance(data, dict) and isinstance(data.get('code'), int)
    except Exception:
        return False


class ProxyVault:
    """免费代理池后台补货仓（只放 HTTPS CONNECT 可用的出口）。

    - 补货全在后台线程：拉源 -> 并发探活 -> 入库，主循环 get() 只是出队，O(1)；
    - 仓空时 get() 返回 None，调用方直连，不阻塞节拍；
    - 同一个代理可以被反复取用（粘性），跑满 PROXY_PER_IP_LIMIT 次后由调用方轮换。
    """

    def __init__(self, log=None):
        self._log = log or (lambda *_: None)
        self._lock = threading.Lock()
        self._ready = deque()
        self._alive_at = {}
        self._dead_at = {}
        self._tried = set()
        self._stop = threading.Event()
        self._thread = None
        self._source_fails = 0
        self._empty_warned = False

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._refill_loop,
                                        name='car-proxy-vault', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(2.0)

    def size(self):
        with self._lock:
            return len(self._ready)

    def get(self, wait_s=0.0):
        """取一个可用出口；没有就返回 None（调用方直连）。"""
        deadline = time.time() + max(0.0, wait_s)
        while True:
            now = time.time()
            with self._lock:
                while self._ready:
                    p = self._ready.popleft()
                    if now - self._alive_at.get(p, 0.0) <= PROXY_ALIVE_TTL:
                        self._empty_warned = False
                        return p
                warn = not self._empty_warned
                if warn:
                    self._empty_warned = True
            if self._stop.is_set() or time.time() >= deadline:
                if warn:
                    self._log('[CAR] 暂无可用代理出口，本轮走直连（后台继续补货）')
                return None
            self._stop.wait(1.0)

    def release(self, proxy, alive=True):
        if not proxy:
            return
        now = time.time()
        with self._lock:
            if not alive:
                self._dead_at[proxy] = now
                try:
                    self._ready.remove(proxy)   # 判死的出口不能还留在仓里被再次取到
                except ValueError:
                    pass
                return
            self._alive_at[proxy] = now
            if proxy not in self._ready and len(self._ready) < PROXY_MAX_READY:
                self._ready.append(proxy)

    def mark_dead(self, proxy):
        self.release(proxy, alive=False)

    # -- 后台补货 --
    def _refill_loop(self):
        while not self._stop.is_set():
            if self.size() >= PROXY_MIN_READY:
                self._stop.wait(5.0)
                continue
            try:
                added = self._refill_once()
            except Exception as e:
                self._log(f'[CAR] 代理补货异常：{type(e).__name__}: {e}')
                added = 0
            if self._stop.is_set():
                break
            self._stop.wait(2.0 if added else 60.0)

    def _refill_once(self):
        if get_total_proxies is None:
            return 0
        try:
            cands = get_total_proxies(lambda *_: None, self._stop)
        except Exception:
            self._source_fails += 1
            if self._source_fails in (1, 3) or self._source_fails % 10 == 0:
                self._log(f'[CAR] 免费代理源拉取失败（第 {self._source_fails} 次），继续直连')
            return 0
        self._source_fails = 0
        if not cands or self._stop.is_set():
            return 0

        now = time.time()
        with self._lock:
            fresh = [p for p in cands if p not in self._tried
                     and now - self._dead_at.get(p, 0.0) > PROXY_DEAD_TTL]
            if not fresh:  # 候选都试过了，过一轮再重来
                self._tried.clear()
                fresh = [p for p in cands
                         if now - self._dead_at.get(p, 0.0) > PROXY_DEAD_TTL]
        fresh = list(dict.fromkeys(fresh))
        if not fresh:
            return 0
        random.shuffle(fresh)

        added = scanned = 0
        chunk = PROXY_PROBE_WORKERS * 2
        with ThreadPoolExecutor(max_workers=PROXY_PROBE_WORKERS) as ex:
            for i in range(0, len(fresh), chunk):
                if self._stop.is_set() or self.size() >= PROXY_MAX_READY:
                    break
                batch = fresh[i:i + chunk]
                for p, ok in zip(batch, ex.map(probe_https, batch)):
                    scanned += 1
                    ts = time.time()
                    with self._lock:
                        self._tried.add(p)
                        if ok and len(self._ready) < PROXY_MAX_READY:
                            self._alive_at[p] = ts
                            self._ready.append(p)
                            added += 1
                        elif not ok:
                            self._dead_at[p] = ts
        if added:
            self._log(f'[CAR] 代理池补货 +{added}（当前可用 {self.size()}，'
                      f'本轮已探 {scanned}/{len(fresh)} 个候选）')
        return added


# ── 请求 ──────────────────────────────────────────────────────────────
def _aes_encrypt(plain: bytes) -> bytes:
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(plain, 16))


def car_click(info, buvid, buvid4, did, proxy=None, http=None):
    """车机 click：AES 加密体，返回 (code, rpdid)。首次随机 did 会回 14013 + rpdid。

    proxy 为 'ip:port' 时走该出口；http 为可选 requests.Session（会话内复用连接）。
    """
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
    body = _aes_encrypt((query + '&sign=' + sign).encode())
    try:
        r = (http or requests).post(
            CAR_CLICK, data=body,
            timeout=(PROXY_TIMEOUT, TIMEOUT) if proxy else TIMEOUT, verify=False,
            proxies=_proxy_map(proxy) if proxy else None,
            headers={
                'User-Agent': CAR_UA,
                'Content-Type': 'application/octet-stream',
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Buvid': buvid,
                'Cookie': _cookies(buvid, buvid4),
            })
        d = r.json()
        return d.get('code'), (d.get('data') or {}).get('rpdid')
    except Exception as e:
        return f'exc:{type(e).__name__}', None


def hb_mobile(info, session, buvid, buvid4, played, duration, play_type, proxy=None, http=None):
    """APP 心跳：FormUrlEncoded + appkey/ts/sign（md5(排序参数 + appsec)）。

    session 是心跳会话 id（不是 requests.Session），http 才是可选的连接复用对象。
    """
    now = int(time.time())
    played = max(1, min(int(played), duration - 1)) if duration > 1 else max(1, int(played))
    body = {
        'appkey': APPKEY, 'ts': str(now), 'mobi_app': 'android',
        'platform': 'android', 'build': '7660300',
        'start_ts': str(now - played), 'session': session, 'mid': '',
        'aid': str(info['aid']), 'cid': str(info['cid']), 'sid': '0', 'epid': '0',
        'type': '1', 'sub_type': '0', 'quality': '80',
        'total_time': str(played), 'paused_time': '0', 'played_time': str(played),
        'video_duration': str(duration), 'play_type': play_type, 'network_type': '1',
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
        r = (http or requests).post(
            HB_MOBILE, data=body, timeout=(PROXY_TIMEOUT, TIMEOUT) if proxy else TIMEOUT,
            verify=False, proxies=_proxy_map(proxy) if proxy else None,
            headers={
                'User-Agent': HB_UA,
                'Cookie': _cookies(buvid, buvid4),
                'Content-Type': 'application/x-www-form-urlencoded',
            })
        return r.json().get('code')
    except Exception as e:
        return f'exc:{type(e).__name__}'


def fetch_video_info(bv):
    # 机房 IP 只带 UA 直连 view 接口易被 WAF 判 412，补全 Referer/Cookie 模拟网页内 XHR
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'),
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://www.bilibili.com/',
        'Origin': 'https://www.bilibili.com',
        'Cookie': (f'buvid3={generate_buvid3()}; buvid4={generate_buvid4()}; '
                   f'b_nut={int(time.time())}'),
    }
    r = requests.get('https://api.bilibili.com/x/web-interface/view', params={'bvid': bv},
                     timeout=TIMEOUT + 2, headers=headers)
    r.raise_for_status()
    p = r.json()
    if p.get('code') != 0 or 'data' not in p:
        raise RuntimeError(f'bilibili API code={p.get("code")} message={p.get("message")}')
    return p['data']


def fetch_views(bv):
    return int(fetch_video_info(bv)['stat']['view'])


# ── 单次会话 ──────────────────────────────────────────────────────────
def run_session(info, play_s=DEFAULT_PLAY_S, stop_event=None, proxy=None):
    """一次独立设备会话：click -> 复点拿 rpdid -> start/mid/end 三次心跳。

    proxy 为 'ip:port' 时整段会话（5 个请求）粘在同一出口上。
    """
    stop_event = stop_event or threading.Event()
    buvid, buvid4, did = generate_buvid3(), generate_buvid4(), generate_did()
    session = uuid.uuid4().hex
    duration = int(info.get('duration') or 60)
    t0 = time.time()

    http = None
    if proxy:
        http = requests.Session()
        http.proxies.update(_proxy_map(proxy))
        http.verify = False
    try:
        code1, rpdid = car_click(info, buvid, buvid4, did, proxy=proxy, http=http)
        if isinstance(code1, str) and code1.startswith('exc:'):
            # 代理连接层就失败，心跳必然也打不通，别白等三轮
            return {'ok': False, 'click': (code1, None), 'hb': [],
                    'done': True, 'proxy': proxy}
        code2 = None
        if rpdid:
            for _ in range(2):
                code2, _ = car_click(info, buvid, buvid4, rpdid, proxy=proxy, http=http)
                if code2 == 0:
                    break
                time.sleep(0.3)
        ok = code1 == 0 or code2 == 0

        hbs = []
        for frac, play_type in ((0.0, '1'), (0.5, '0'), (1.0, '0')):
            remain = t0 + play_s * frac - time.time()
            if remain > 0 and stop_event.wait(remain):
                return {'ok': ok, 'click': (code1, code2), 'hb': hbs,
                        'done': False, 'proxy': proxy}
            hbs.append(hb_mobile(info, session, buvid, buvid4, max(1, int(play_s * frac)),
                                 duration, play_type, proxy=proxy, http=http))
        return {'ok': ok, 'click': (code1, code2), 'hb': hbs,
                'done': True, 'proxy': proxy}
    finally:
        if http is not None:
            try:
                http.close()
            except Exception:
                pass


# ── 长线循环 ──────────────────────────────────────────────────────────
def _bv_loop(bv, info, play_s, interval, stop_event, on_cycle, log, max_rounds=None,
             vault=None):
    last_views = None
    cycle = 0
    proxy = None
    proxy_hits = 0
    gain_hist = deque(maxlen=PROXY_NO_GAIN_LIMIT)   # 当前出口最近几轮的播放增量
    last_session_proxy = None                       # 上一轮会话实际用的出口
    try:
        while not stop_event.is_set():
            if max_rounds and cycle >= max_rounds:
                log(f'[CAR] {bv} 已完成 {max_rounds} 轮，收线')
                break
            t0 = time.time()
            cycle += 1
            views = None
            try:
                views = fetch_views(bv)   # 取数保持直连，不占用代理
            except Exception:
                pass
            delta = None
            if views is not None:
                if last_views is not None:
                    delta = views - last_views
                last_views = views

            # 增量是上一轮会话的结果，只在同一出口连续时才算进当前出口的账
            if delta is not None and last_session_proxy and last_session_proxy == proxy:
                gain_hist.append(delta)

            # 出口轮换：连续多轮不计播放 / 跑满额度，都换 IP
            if vault is not None:
                if proxy and len(gain_hist) >= PROXY_NO_GAIN_LIMIT and max(gain_hist) <= 0:
                    # click 回了 0 但 views 不动（机房 IP 被静默丢弃），别继续白耗
                    vault.mark_dead(proxy)
                    log(f'[CAR] {bv} 出口 {proxy} 连续 {PROXY_NO_GAIN_LIMIT} 轮未计播放'
                        f'（views={views if views is not None else "?"}），判死换 IP')
                    proxy, proxy_hits = None, 0
                if proxy and proxy_hits >= PROXY_PER_IP_LIMIT:
                    vault.mark_dead(proxy)
                    log(f'[CAR] {bv} 出口 {proxy} 已贡献 {proxy_hits} 次，轮换下个 IP')
                    proxy, proxy_hits = None, 0
                if not proxy:
                    proxy, proxy_hits = vault.get(), 0
                    gain_hist.clear()

            r = run_session(info, play_s, stop_event, proxy=proxy)
            if not r['done']:
                break
            if vault is not None and proxy and not r['ok']:
                # click 未计：判该出口失效，同窗换一个出口重试一次
                vault.mark_dead(proxy)
                log(f'[CAR] {bv} 出口 {proxy} 未计（click={r["click"][0]}/{r["click"][1]}），'
                    f'换出口重试')
                proxy, proxy_hits = vault.get(), 0
                gain_hist.clear()
                r = run_session(info, play_s, stop_event, proxy=proxy)
                if not r['done']:
                    break
            if proxy:
                proxy_hits += 1
            last_session_proxy = proxy

            click = f"{r['click'][0]}/{r['click'][1]}"
            hb = '/'.join(str(x) for x in r['hb'])
            delta_txt = f'{delta:+d}' if delta is not None else '?'
            proxy_txt = f'proxy={proxy} ({proxy_hits}/{PROXY_PER_IP_LIMIT})' if proxy else 'proxy=direct'
            log(f'[CAR] {bv} #{cycle} views={views if views is not None else "?"} ({delta_txt}) '
                f'click={click} hb={hb} {proxy_txt}')
            if on_cycle:
                try:
                    on_cycle({'bv': bv, 'cycle': cycle, 'views': views, 'delta': delta,
                              'ok': r['ok'], 'click': click, 'hb': hb,
                              'proxy': proxy or 'direct'})
                except Exception:
                    pass

            wait = interval - (time.time() - t0)
            while wait > 0 and not stop_event.is_set():
                stop_event.wait(min(1.0, wait))
                wait = interval - (time.time() - t0)
    finally:
        if proxy:
            if vault is not None:
                vault.release(proxy, alive=True)


def run(bv_list, play_s=DEFAULT_PLAY_S, interval=DEFAULT_INTERVAL, stop_event=None,
        on_cycle=None, log=None, max_rounds=None):
    """阻塞运行，直到 stop_event 置位或各稿件跑满 max_rounds。每稿一线程，各自 85s 节拍。"""
    stop_event = stop_event or threading.Event()
    log = log or print
    interval = max(MIN_INTERVAL, int(interval))

    vault = None
    if PROXY_ENABLED and get_total_proxies is not None:
        vault = _acquire_vault(log)
        log('[CAR] 免费代理池（实验性）已启用：后台探活 HTTPS CONNECT 出口，取不到时自动直连')

    try:
        infos = {}
        for bv in bv_list:
            try:
                info = fetch_video_info(bv)
                infos[bv] = info
                log(f'[CAR] {bv} 就绪 aid={info["aid"]} cid={info["cid"]} '
                    f'dur={info.get("duration")}s views={info["stat"]["view"]}')
            except Exception as e:
                log(f'[CAR] {bv} 获取稿件信息失败：{type(e).__name__}: {e}')
        if not infos:
            log('[CAR] 没有可用稿件，任务结束')
            return

        log(f'[CAR] 长线启动：{len(infos)} 个稿件，每稿 {interval}s 一轮（play_s={play_s}'
            f'{f"，轮数上限 {max_rounds}" if max_rounds else ""}）')
        threads = []
        for bv, info in infos.items():
            t = threading.Thread(target=_bv_loop,
                                 args=(bv, info, play_s, interval, stop_event, on_cycle, log,
                                       max_rounds, vault),
                                 daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            while t.is_alive():
                t.join(0.5)
        log('[CAR] 长线已停止')
    finally:
        if vault is not None:
            _release_vault()
