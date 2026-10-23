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
想放量只能扩 IP，而车机端点全为 https，需要 CONNECT 可用的代理（免费池基本不可用）。

输出统一走 log 回调（默认 print），由 app.py 写入任务日志缓冲。
"""
import hashlib
import random
import threading
import time
import uuid

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


# ── 请求 ──────────────────────────────────────────────────────────────
def _aes_encrypt(plain: bytes) -> bytes:
    return AES.new(AES_KEY, AES.MODE_CBC, AES_IV).encrypt(pad(plain, 16))


def car_click(info, buvid, buvid4, did):
    """车机 click：AES 加密体，返回 (code, rpdid)。首次随机 did 会回 14013 + rpdid。"""
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
        r = requests.post(CAR_CLICK, data=body, timeout=TIMEOUT, verify=False, headers={
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


def hb_mobile(info, session, buvid, buvid4, played, duration, play_type):
    """APP 心跳：FormUrlEncoded + appkey/ts/sign（md5(排序参数 + appsec)）。"""
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
        r = requests.post(HB_MOBILE, data=body, timeout=TIMEOUT, verify=False, headers={
            'User-Agent': HB_UA,
            'Cookie': _cookies(buvid, buvid4),
            'Content-Type': 'application/x-www-form-urlencoded',
        })
        return r.json().get('code')
    except Exception as e:
        return f'exc:{type(e).__name__}'


def fetch_video_info(bv):
    r = requests.get('https://api.bilibili.com/x/web-interface/view', params={'bvid': bv},
                     timeout=TIMEOUT + 2,
                     headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    r.raise_for_status()
    p = r.json()
    if p.get('code') != 0 or 'data' not in p:
        raise RuntimeError(f'bilibili API code={p.get("code")} message={p.get("message")}')
    return p['data']


def fetch_views(bv):
    return int(fetch_video_info(bv)['stat']['view'])


# ── 单次会话 ──────────────────────────────────────────────────────────
def run_session(info, play_s=DEFAULT_PLAY_S, stop_event=None):
    """一次独立设备会话：click -> 复点拿 rpdid -> start/mid/end 三次心跳。"""
    stop_event = stop_event or threading.Event()
    buvid, buvid4, did = generate_buvid3(), generate_buvid4(), generate_did()
    session = uuid.uuid4().hex
    duration = int(info.get('duration') or 60)
    t0 = time.time()

    code1, rpdid = car_click(info, buvid, buvid4, did)
    code2 = None
    if rpdid:
        for _ in range(2):
            code2, _ = car_click(info, buvid, buvid4, rpdid)
            if code2 == 0:
                break
            time.sleep(0.3)
    ok = code1 == 0 or code2 == 0

    hbs = []
    for frac, play_type in ((0.0, '1'), (0.5, '0'), (1.0, '0')):
        remain = t0 + play_s * frac - time.time()
        if remain > 0 and stop_event.wait(remain):
            return {'ok': ok, 'click': (code1, code2), 'hb': hbs, 'done': False}
        hbs.append(hb_mobile(info, session, buvid, buvid4, max(1, int(play_s * frac)),
                             duration, play_type))
    return {'ok': ok, 'click': (code1, code2), 'hb': hbs, 'done': True}


# ── 长线循环 ──────────────────────────────────────────────────────────
def _bv_loop(bv, info, play_s, interval, stop_event, on_cycle, log, max_rounds=None):
    last_views = None
    cycle = 0
    while not stop_event.is_set():
        if max_rounds and cycle >= max_rounds:
            log(f'[CAR] {bv} 已完成 {max_rounds} 轮，收线')
            break
        t0 = time.time()
        cycle += 1
        views = None
        try:
            views = fetch_views(bv)
        except Exception:
            pass
        delta = None
        if views is not None:
            if last_views is not None:
                delta = views - last_views
            last_views = views

        r = run_session(info, play_s, stop_event)
        if not r['done']:
            break
        click = f"{r['click'][0]}/{r['click'][1]}"
        hb = '/'.join(str(x) for x in r['hb'])
        delta_txt = f'{delta:+d}' if delta is not None else '?'
        log(f'[CAR] {bv} #{cycle} views={views if views is not None else "?"} ({delta_txt}) '
            f'click={click} hb={hb}')
        if on_cycle:
            try:
                on_cycle({'bv': bv, 'cycle': cycle, 'views': views, 'delta': delta,
                          'ok': r['ok'], 'click': click, 'hb': hb})
            except Exception:
                pass

        wait = interval - (time.time() - t0)
        while wait > 0 and not stop_event.is_set():
            stop_event.wait(min(1.0, wait))
            wait = interval - (time.time() - t0)


def run(bv_list, play_s=DEFAULT_PLAY_S, interval=DEFAULT_INTERVAL, stop_event=None,
        on_cycle=None, log=None, max_rounds=None):
    """阻塞运行，直到 stop_event 置位或各稿件跑满 max_rounds。每稿一线程，各自 85s 节拍。"""
    stop_event = stop_event or threading.Event()
    log = log or print
    interval = max(MIN_INTERVAL, int(interval))

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
                                   max_rounds),
                             daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        while t.is_alive():
            t.join(0.5)
    log('[CAR] 长线已停止')
