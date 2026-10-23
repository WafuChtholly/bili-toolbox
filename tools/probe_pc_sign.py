"""PC 通道自签名验证探针：不依赖官方客户端，纯 HTTP 复现 x/report/click/pc/client。

签名规则（由客户端 native 反向确证）：
    sign = md5("&".join(f"{k}={quote(str(v), safe='')}" for k, v in sorted(params))
               + appsec)
其中 params = payload + appkey + build + ts(毫秒)，空值参与、不剔除 sign 之外的任何字段；
body 按 payload 原插入序发送，appkey/build/ts 追加在末尾，sign 最后。

子命令：
  sign     用不存在的 aid 发一次请求，判断服务端是否接受自签名
  variants 对比几种签名/编码变体，定位 -3 的具体原因
  run      指定 BV 发送 N 次自签名点击，每轮前后读 view 统计增量
  ab       A/B 对照：B 组复用同 buvid + 首轮 rpdid 作为 did，A 组每轮全新 buvid
  burst    并发批量：同时打 N 次（各自新 buvid），再按间隔读 view 看总增量
  proxies  代理实测：拉代理池 → 探测存活 → 1 个 IP 只打 1 次，再看 view 总增量
  buvid4   A/B/C：不传 buvid4 / 每次新 buvid4 / 全局共用同一 buvid4，比较命中率
  reuse    单 IP 冷却窗口：同一批已点击 IP 在间隔 N 秒后重放，看是否还能计数

用法：
  python tools/probe_pc_sign.py sign
  python tools/probe_pc_sign.py run --bv BV1xx --rounds 10 --interval 85
  python tools/probe_pc_sign.py ab  --bv BV1xx --rounds 10 --interval 20
  python tools/probe_pc_sign.py burst --bv BV1xx --rounds 10 --workers 10
  python tools/probe_pc_sign.py proxies --bv BV1xx --scan 600 --limit 15
可选：--proxy http://1.2.3.4:8080  不传则直连
"""
import argparse
import base64
import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

PC_CLICK_URL = 'https://api.bilibili.com/x/report/click/pc/client'
PC_CLICK_HTTP = 'http://api.bilibili.com/x/report/click/pc/client'  # 走代理时免 TLS 隧道
PC_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         '(KHTML, like Gecko) bilibili_pc/1.19.0 Chrome/108.0.5359.215 '
         'Electron/22.3.27 Safari/537.36 build/1001019000')
PC_APPKEY = 'aa1e74ee4874176e'
PC_APPSEC = '54e6a9a31b911cd5fc0daa66ebf94bc4'
PC_BUILD = '1001019000'


def generate_buvid3():
    d = hashlib.md5(f'{random.random()}{time.time()}'.encode()).hexdigest()
    return f'{d[:8]}-{d[8:12]}infoc'


def generate_buvid4():
    """按 spi 返回的真实格式伪造 buvid4：hex 段 + 9 位数字 + base64(16B)。"""
    h = uuid.uuid4().hex.upper()
    head = f'{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:36]}{random.randint(10000, 99999)}'
    tail = base64.b64encode(os.urandom(16)).decode()
    return f'{head}-{random.randint(100000000, 999999999)}-{tail}'


def build_headers(buvid, buvid4=''):
    cookie = f'buvid3={buvid}'
    if buvid4:
        cookie += f'; buvid4={buvid4}'
    return {
        'User-Agent': PC_UA,
        'Origin': 'https://www.bilibili.com',
        'Referer': 'https://www.bilibili.com/client',
        'Content-Type': 'application/x-www-form-urlencoded',
        'x-app-version': '1.19.0',
        'buvid': buvid,
        'Cookie': cookie,
    }


def build_payload(aid, cid, buvid, did=''):
    now_ms = int(time.time() * 1000)
    return {
        'aid': str(aid), 'cid': str(cid), 'part': '1', 'lv': '0',
        'ftime': str(now_ms // 1000), 'stime': str(now_ms // 1000),
        'type': '3', 'sub_type': '0', 'refer_url': '', 'outer': '0',
        'statistics': json.dumps({'appId': 112, 'platform': 4}, separators=(',', ':')),
        'mobi_app': 'pc_electron',
        'device': 'win', 'platform': 'web', 'cur_language': '', 'perfer_type': '',
        'play_mode': '1', 'spmid': 'main.play-detail.0.0.pv', 'from_spmid': '',
        'session': uuid.uuid4().hex,
        'extra': json.dumps({'from_outer_spmid': ''}, separators=(',', ':')),
        'build': PC_BUILD, 'buvid': buvid, 'did': did,
        'auto_play': '0', 'track_id': '', 'playlist_type': '', 'playlist_id': '',
    }


def _enc(s):
    return urllib.parse.quote(str(s), safe='')


def pc_body(payload, mode='native'):
    """生成可直接 POST 的 form body（含 sign）。mode 仅用于诊断变体。"""
    params = {k: v for k, v in payload.items() if k != 'sign'}
    params['appkey'] = PC_APPKEY
    params['build'] = PC_BUILD
    if mode == 'ts_seconds':
        params['ts'] = str(int(time.time()))
        params['ftime'] = params['stime'] = params['ts']
    else:
        params['ts'] = str(int(time.time() * 1000))

    if mode == 'sorted':
        body_items = sorted(params.items())
    else:
        body_items = list(params.items())

    sign_items = sorted(params.items())
    if mode == 'drop_empty':
        body_items = [(k, v) for k, v in body_items if v != '']
        sign_items = [(k, v) for k, v in sign_items if v != '']
    if mode == 'raw_values':
        sign_str = '&'.join(f'{k}={v}' for k, v in sign_items)
    elif mode == 'quote_plus':
        sign_str = '&'.join(f'{k}={urllib.parse.quote_plus(str(v))}' for k, v in sign_items)
    else:
        sign_str = '&'.join(f'{k}={_enc(v)}' for k, v in sign_items)

    sign = hashlib.md5((sign_str + PC_APPSEC).encode()).hexdigest()
    body = '&'.join(f'{k}={_enc(v)}' for k, v in body_items)
    return f'{body}&sign={sign}'


def proxies_of(proxy):
    return {'http': proxy, 'https': proxy} if proxy else None


def send_pc(aid, cid, proxy=None, mode='native', did='', buvid=None, url=None, buvid4=''):
    buvid = buvid or generate_buvid3()
    body = pc_body(build_payload(aid, cid, buvid, did), mode=mode)
    try:
        r = requests.post(url or PC_CLICK_URL, headers=build_headers(buvid, buvid4),
                          data=body.encode(),
                          proxies=proxies_of(proxy), timeout=(5, 10), allow_redirects=False)
    except Exception as err:
        return {'http': None, 'code': None, 'error': f'{type(err).__name__}: {err}'}
    try:
        payload = r.json()
    except Exception:
        payload = {'_raw': r.text[:300]}
    data = payload.get('data')
    return {'http': r.status_code, 'code': payload.get('code'),
            'message': payload.get('message'), 'data': data,
            'rpdid': (data or {}).get('rpdid') if isinstance(data, dict) else None,
            'raw': payload if payload.get('code') is None else None}


def fetch_video(bv, proxy=None):
    r = requests.get('https://api.bilibili.com/x/web-interface/view', params={'bvid': bv},
                     proxies=proxies_of(proxy), timeout=10,
                     headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    p = r.json()
    if p.get('code') != 0:
        raise RuntimeError(f'view API code={p.get("code")} message={p.get("message")}')
    return p['data']


def cmd_sign(args):
    """用不存在的 aid 探测：-3 → 签名不符；其他错误/0 → 签名已被接受。"""
    print('[probe] native 签名（插入序 body）')
    print('  ', send_pc(args.aid, args.cid, args.proxy, mode='native'))
    print('[probe] 其他变体')
    for mode in ('sorted', 'drop_empty', 'raw_values', 'quote_plus', 'ts_seconds'):
        print(f'  {mode:<12}', send_pc(args.aid, args.cid, args.proxy, mode=mode))
        time.sleep(1.0)


def cmd_variants(args):
    cmd_sign(args)


def run_batch(tag, bv, aid, cid, rounds, interval, proxy, reuse_did):
    """跑一批点击。reuse_did=True 时同 buvid 复用首轮 rpdid 作为 did。"""
    buvid = generate_buvid3()
    did = ''
    before = int(fetch_video(bv, proxy)['stat']['view'])
    start_view = before
    print(f'[{tag}] start view={before} rounds={rounds} interval={interval}s '
          f'buvid={buvid} mode={"rpdid" if reuse_did else "fresh"}', flush=True)
    ok = counted = 0
    codes = {}
    for i in range(1, rounds + 1):
        t0 = time.time()
        cur_buvid = buvid if reuse_did else generate_buvid3()
        res = send_pc(aid, cid, proxy, did=did, buvid=cur_buvid)
        codes[res.get('code')] = codes.get(res.get('code'), 0) + 1
        if res.get('code') == 0:
            ok += 1
        if reuse_did and not did and res.get('rpdid'):
            did = res['rpdid']
        wait = interval - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)
        try:
            after = int(fetch_video(bv, proxy)['stat']['view'])
        except Exception as err:
            print(f'[{tag} {i}] code={res.get("code")} view=? ({err})', flush=True)
            continue
        if after > before:
            counted += 1
        print(f'[{tag} {i}] code={res.get("code")} did={did[:10] or "-"} '
              f'view={after} delta={after - before}', flush=True)
        before = after
    summary = {'tag': tag, 'rounds': rounds, 'code0': ok, 'counted': counted,
               'start': start_view, 'view': before, 'delta': before - start_view,
               'codes': codes}
    print(f'[{tag}] done code0={ok}/{rounds} counted={counted} '
          f'view={start_view}->{before} (+{before - start_view}) codes={codes}', flush=True)
    return summary


def cmd_run(args):
    info = fetch_video(args.bv, args.proxy)
    run_batch('run', args.bv, info['aid'], info['cid'], args.rounds, args.interval,
              args.proxy, reuse_did=False)


def cmd_ab(args):
    """B 组先跑（复用 rpdid），再用 A 组（每轮新 buvid）复基。"""
    info = fetch_video(args.bv, args.proxy)
    aid, cid = info['aid'], info['cid']
    print(f'[ab] {args.bv} aid={aid} cid={cid}', flush=True)
    b = run_batch('B-rpdid', args.bv, aid, cid, args.rounds, args.interval, args.proxy, True)
    time.sleep(10)
    a = run_batch('A-fresh', args.bv, aid, cid, args.rounds, args.interval, args.proxy, False)
    print('[ab] B-rpdid : code0={}/{} counted={} view={}->{}(+{})'.format(
        b['code0'], b['rounds'], b['counted'], b['start'], b['view'], b['delta']), flush=True)
    print('[ab] A-fresh : code0={}/{} counted={} view={}->{}(+{})'.format(
        a['code0'], a['rounds'], a['counted'], a['start'], a['view'], a['delta']), flush=True)


def cmd_burst(args):
    """并发批量点击：同时打 N 次（各自新 buvid），再按固定间隔读 view 看总增量。"""
    info = fetch_video(args.bv, args.proxy)
    aid, cid = info['aid'], info['cid']
    start = int(info['stat']['view'])
    print(f'[burst] {args.bv} aid={aid} cid={cid} start view={start} '
          f'n={args.rounds} workers={args.workers}', flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda _: send_pc(aid, cid, args.proxy), range(args.rounds)))
    codes = {}
    for r in results:
        codes[r.get('code')] = codes.get(r.get('code'), 0) + 1
    print(f'[burst] sent {args.rounds} in {time.time() - t0:.1f}s codes={codes}', flush=True)
    for step in range(1, args.settle // args.check + 1):
        time.sleep(args.check)
        try:
            cur = int(fetch_video(args.bv, args.proxy)['stat']['view'])
        except Exception as err:
            print(f'[burst +{step * args.check}s] read fail {err}', flush=True)
            continue
        print(f'[burst +{step * args.check}s] view={cur} total=+{cur - start}', flush=True)


def _probe_proxy(proxy, bv):
    """轻量探测代理是否能连通 B 站（只等响应头，不读 body）。"""
    try:
        r = requests.get('http://api.bilibili.com/x/web-interface/view',
                         params={'bvid': bv},
                         proxies={'http': f'http://{proxy}'},
                         headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
                         timeout=(2, 3), allow_redirects=False, stream=True)
        try:
            return r.status_code in (200, 301, 302, 307, 308)
        finally:
            r.close()
    except Exception:
        return False


def collect_candidates(bv, scan, exclude=None):
    """拉代理池 → 按 IP 去重 → 排除已用 IP → 打乱 → 返回至多 scan 个候选。"""
    sys.path.insert(0, str(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'bili-common')))
    from proxy_pool import get_total_proxies

    exclude = exclude or set()
    pool = get_total_proxies(lambda m: print(f'[pool] {m}', flush=True))
    seen, candidates = set(), []
    random.shuffle(pool)
    for p in pool:
        ip = str(p).split(':')[0]
        if ip in seen or ip in exclude:
            continue
        seen.add(ip)
        candidates.append(p)
    print(f'[pool] total={len(pool)} usable-ip={len(candidates)} '
          f'(excluded={len(exclude)}) scan={min(scan, len(candidates))}', flush=True)
    return candidates[:scan]


def _click_round(tag, bv, aid, cid, scan, used_ips, args, mode='none', fixed_buvid4=''):
    """拉一批新 IP → 探测通过立刻点击（1 IP 1 次）→ 返回投递结果。

    mode: none=不传 buvid4；fresh=每次请求新 buvid4；fixed=全局共用 fixed_buvid4。
    """
    candidates = collect_candidates(bv, scan, used_ips)
    if not candidates:
        return {'sent': [], 'clicked': 0, 'code0': 0, 'codes': {}}

    lock = threading.Lock()
    sent = []
    dead = 0

    def _one(p):
        nonlocal dead
        with lock:
            if args.limit and len(sent) >= args.limit:
                return
        if not _probe_proxy(p, bv):
            with lock:
                dead += 1
            return
        buvid4 = generate_buvid4() if mode == 'fresh' else (fixed_buvid4 if mode == 'fixed' else '')
        res = send_pc(aid, cid, f'http://{p}', url=PC_CLICK_HTTP, buvid4=buvid4)
        with lock:
            sent.append((p, res))
            used_ips.add(str(p).split(':')[0])

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(_one, candidates))
    codes = {}
    for _, r in sent:
        codes[r.get('code')] = codes.get(r.get('code'), 0) + 1
    print(f'[{tag}] probe-fail={dead} clicked={len(sent)} code0={codes.get(0, 0)} '
          f'codes={codes} in {time.time() - t0:.1f}s', flush=True)
    return {'sent': sent, 'clicked': len(sent), 'code0': codes.get(0, 0), 'codes': codes}


def _replay(tag, bv, aid, cid, proxies, workers):
    """用同一批 IP 重放（每次全新 buvid3），不做 IP 去重。"""
    lock = threading.Lock()
    sent = []
    dead = 0

    def _one(p):
        nonlocal dead
        if not _probe_proxy(p, bv):
            with lock:
                dead += 1
            return
        res = send_pc(aid, cid, f'http://{p}', url=PC_CLICK_HTTP)
        with lock:
            sent.append((p, res))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_one, proxies))
    codes = {}
    for _, r in sent:
        codes[r.get('code')] = codes.get(r.get('code'), 0) + 1
    print(f'[{tag}] probe-fail={dead} clicked={len(sent)} code0={codes.get(0, 0)} '
          f'codes={codes} in {time.time() - t0:.1f}s', flush=True)
    return {'sent': sent, 'clicked': len(sent), 'code0': codes.get(0, 0), 'codes': codes}


def _settle_view(tag, bv, before, settle, check, max_wait, stable=2):
    """先等 settle，再轮询到读数平台期（连续 stable 次不变或达 max_wait），返回最终 view。
    用途：规避计数延迟到账把上一轮增量算进下一轮。"""
    t_click = time.time()
    time.sleep(settle)
    prev = int(fetch_video(bv)['stat']['view'])
    same = 1
    print(f'[{tag}] +{settle}s view={prev} delta=+{prev - before}', flush=True)
    while time.time() - t_click < settle + max_wait:
        time.sleep(check)
        cur = int(fetch_video(bv)['stat']['view'])
        print(f'[{tag}] +{int(time.time() - t_click)}s view={cur} '
              f'delta=+{cur - before}', flush=True)
        if cur == prev:
            same += 1
            if same >= stable:
                break
        else:
            same = 1
            prev = cur
    print(f'[{tag}] SETTLED view {before}->{prev} delta=+{prev - before} '
          f'(stable={same})', flush=True)
    return prev


def cmd_proxies(args):
    """按 web/H5 范式跑多轮：每轮重拉代理池、跨轮不复用 IP、每 IP 只打 1 次。"""
    info = fetch_video(args.bv)
    aid, cid = info['aid'], info['cid']
    start = int(info['stat']['view'])
    print(f'[proxies] {args.bv} aid={aid} cid={cid} start view={start} '
          f'rounds={args.rounds} scan={args.scan} limit={args.limit or "all"}', flush=True)

    used_ips = set()
    idle_rounds = 0
    total_clicked = 0
    total_code0 = 0

    for rnd in range(1, args.rounds + 1):
        round_start = int(fetch_video(args.bv)['stat']['view'])
        print(f'\n[round {rnd}/{args.rounds}] base view={round_start}', flush=True)
        r = _click_round(f'round {rnd}', args.bv, aid, cid, args.scan, used_ips, args)
        if not r['clicked']:
            print('[round] no unused candidate, stop', flush=True)
            break
        total_clicked += r['clicked']
        total_code0 += r['code0']

        for step in range(1, args.settle // args.check + 1):
            time.sleep(args.check)
            try:
                cur = int(fetch_video(args.bv)['stat']['view'])
            except Exception as err:
                print(f'  [+{step * args.check}s] read fail {err}', flush=True)
                continue
            print(f'  [+{step * args.check}s] view={cur} round=+{cur - round_start} '
                  f'total=+{cur - start}', flush=True)
        delta = int(fetch_video(args.bv)['stat']['view']) - round_start
        print(f'[round {rnd}] delta=+{delta}', flush=True)

        if delta <= 0:
            idle_rounds += 1
            if idle_rounds >= args.max_idle_rounds:
                print(f'[proxies] {idle_rounds} idle rounds in a row, stop', flush=True)
                break
        else:
            idle_rounds = 0

    end = int(fetch_video(args.bv)['stat']['view'])
    gained = end - start
    hit = f'{gained / total_code0:.0%}' if total_code0 else 'n/a'
    print(f'\n[proxies DONE] view {start} -> {end} total=+{gained} '
          f'clicked={total_clicked} code0={total_code0} hit={hit}', flush=True)


def cmd_buvid4(args):
    """三组对照：不传 / 每次新 buvid4 / 全局共用同一 buvid4（各自独立 IP 集）。

    --bvs 可指定三条 BV，一臂一条，避免同一稿件的额度上限与延迟到账互相串扰。
    """
    bvs = [b.strip() for b in (args.bvs or args.bv).split(',') if b.strip()]
    shared4 = generate_buvid4()

    used_ips = set()
    rows = []
    arms = (('A-no-buvid4', 'none', ''),
            ('B-fresh-buvid4', 'fresh', ''),
            ('C-shared-buvid4', 'fixed', shared4))
    pick = set(args.arms.upper())
    arms = [a for a in arms if a[0][0] in pick]
    if len(pick) != len(arms) or not arms:
        print(f'[buvid4] --arms 只能是 A/B/C 的组合，收到 {args.arms}', flush=True)
        return
    if len(bvs) == 1:
        bvs = bvs * len(arms)
    if len(bvs) != len(arms):
        print(f'[buvid4] --bvs 需与 --arms 数量一致（arms={len(arms)} bvs={len(bvs)}）',
              flush=True)
        return
    print(f'[buvid4] bvs={bvs} scan={args.scan} limit={args.limit} '
          f'shared_buvid4={shared4}', flush=True)

    for (tag, mode, fixed), bv in zip(arms, bvs):
        info = fetch_video(bv)
        aid, cid = info['aid'], info['cid']
        before = int(info['stat']['view'])
        print(f'\n[{tag}] {bv} aid={aid} cid={cid} base view={before}', flush=True)
        r = _click_round(tag, bv, aid, cid, args.scan, used_ips, args,
                         mode=mode, fixed_buvid4=fixed)
        if not r['clicked']:
            print(f'[{tag}] no click, abort', flush=True)
            break
        prev = _settle_view(tag, bv, before, args.settle, args.check,
                            args.max_wait, args.stable)
        rows.append((tag, bv, r['clicked'], r['code0'], prev - before))

    print('\n[buvid4 SUMMARY]', flush=True)
    for tag, bv, clicked, code0, delta in rows:
        per = f'{delta / clicked:.0%}' if clicked else 'n/a'
        print(f'  {tag:<16} {bv:<14} clicked={clicked:<4} code0={code0:<4} '
              f'delta=+{delta:<4} view/click={per}', flush=True)


def cmd_reuse(args):
    """单 IP 冷却窗口：R1 用全新 IP；R2/R3 在间隔 gap/gap2 后重放同一批 IP。"""
    info = fetch_video(args.bv)
    aid, cid = info['aid'], info['cid']
    start = int(info['stat']['view'])
    print(f'[reuse] {args.bv} aid={aid} cid={cid} start view={start} scan={args.scan} '
          f'gap1={args.gap}s gap2={args.gap2}s', flush=True)

    used_ips = set()
    before = int(fetch_video(args.bv)['stat']['view'])
    r1 = _click_round('R1-fresh-ip', args.bv, aid, cid, args.scan, used_ips, args)
    last_end = time.time()
    proxies = [p for p, _ in r1['sent']]
    if not proxies:
        print('[reuse] no proxy clicked, abort', flush=True)
        return
    after = _settle_view('R1', args.bv, before, args.settle, args.check,
                         args.max_wait, args.stable)
    rows = [('R1-fresh-ip', len(proxies), r1['code0'], after - before)]
    before = after

    for label, gap in (('R2', args.gap), ('R3', args.gap2)):
        print(f'[{label}] wait {gap}s then replay {len(proxies)} same IPs', flush=True)
        time.sleep(gap)
        elapsed = int(time.time() - last_end)
        before = int(fetch_video(args.bv)['stat']['view'])
        r = _replay(f'{label}-same-ip+{elapsed}s', args.bv, aid, cid, proxies, args.workers)
        last_end = time.time()
        after = _settle_view(label, args.bv, before, args.settle, args.check,
                             args.max_wait, args.stable)
        rows.append((f'{label}-+{elapsed}s', r['clicked'], r['code0'], after - before))
        before = after

    print('\n[reuse SUMMARY]', flush=True)
    for tag, clicked, code0, delta in rows:
        per = f'{delta / clicked:.0%}' if clicked else 'n/a'
        print(f'  {tag:<14} clicked={clicked:<4} code0={code0:<4} '
              f'delta=+{delta:<4} view/click={per}', flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('sign')
    p1.add_argument('--aid', default='1234567890')
    p1.add_argument('--cid', default='1')
    p1.add_argument('--proxy', default=None)
    p1.set_defaults(func=cmd_sign)

    p2 = sub.add_parser('run')
    p2.add_argument('--bv', required=True)
    p2.add_argument('--rounds', type=int, default=10)
    p2.add_argument('--interval', type=int, default=85)
    p2.add_argument('--proxy', default=None)
    p2.set_defaults(func=cmd_run)

    p3 = sub.add_parser('variants')
    p3.add_argument('--aid', default='1234567890')
    p3.add_argument('--cid', default='1')
    p3.add_argument('--proxy', default=None)
    p3.set_defaults(func=cmd_variants)

    p4 = sub.add_parser('ab')
    p4.add_argument('--bv', required=True)
    p4.add_argument('--rounds', type=int, default=10)
    p4.add_argument('--interval', type=int, default=20)
    p4.add_argument('--proxy', default=None)
    p4.set_defaults(func=cmd_ab)

    p5 = sub.add_parser('burst')
    p5.add_argument('--bv', required=True)
    p5.add_argument('--rounds', type=int, default=10)
    p5.add_argument('--workers', type=int, default=10)
    p5.add_argument('--settle', type=int, default=120)
    p5.add_argument('--check', type=int, default=30)
    p5.add_argument('--proxy', default=None)
    p5.set_defaults(func=cmd_burst)

    p6 = sub.add_parser('proxies')
    p6.add_argument('--bv', required=True)
    p6.add_argument('--rounds', type=int, default=3)
    p6.add_argument('--scan', type=int, default=3000)
    p6.add_argument('--limit', type=int, default=0)
    p6.add_argument('--workers', type=int, default=128)
    p6.add_argument('--settle', type=int, default=60)
    p6.add_argument('--check', type=int, default=30)
    p6.add_argument('--max-idle-rounds', type=int, default=2)
    p6.set_defaults(func=cmd_proxies)

    p7 = sub.add_parser('buvid4')
    p7.add_argument('--bv', required=True)
    p7.add_argument('--bvs', default='', help='三条 BV，逗号分隔，一臂一条；不传则三臂共用 --bv')
    p7.add_argument('--arms', default='ABC', help='只跑指定臂，如 A / BC / ABC')
    p7.add_argument('--scan', type=int, default=2500)
    p7.add_argument('--limit', type=int, default=0)
    p7.add_argument('--workers', type=int, default=128)
    p7.add_argument('--settle', type=int, default=240)
    p7.add_argument('--check', type=int, default=60)
    p7.add_argument('--max-wait', type=int, default=900)
    p7.add_argument('--stable', type=int, default=3)
    p7.set_defaults(func=cmd_buvid4)

    p8 = sub.add_parser('reuse')
    p8.add_argument('--bv', required=True)
    p8.add_argument('--scan', type=int, default=2500)
    p8.add_argument('--limit', type=int, default=0)
    p8.add_argument('--workers', type=int, default=128)
    p8.add_argument('--settle', type=int, default=240)
    p8.add_argument('--check', type=int, default=60)
    p8.add_argument('--max-wait', type=int, default=900)
    p8.add_argument('--stable', type=int, default=3)
    p8.add_argument('--gap', type=int, default=300)
    p8.add_argument('--gap2', type=int, default=1200)
    p8.set_defaults(func=cmd_reuse)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    sys.exit(main())
