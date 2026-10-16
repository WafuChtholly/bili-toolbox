# -*- coding: utf-8 -*-
"""试探 A：移动端 H5（m.bilibili.com）是否存在「独立播放配额」。

背景
----
booster 走网页端 click/web/h5 接口，把单个稿件的匿名播放配额打到约 200 后就卡住
（按稿件计、不按天重置，已实测）。本脚本换一条上报入口验证：
用 Playwright 以「移动端 UA + 移动端视口 + 匿名（不注入任何登录 Cookie）」打开
m.bilibili.com 视频页，真实触发播放，抓取所有播放上报请求，最后对比 API 播放量。

重要前提（实测踩过的坑）
----
Playwright 自带的 chromium / chromium_headless_shell 都缺 H.264/AAC 编解码器，
B站 的流会被播放器判定为不支持 → 切到 error.mp4 → 一条上报都不发、播放量永不涨。
所以必须用系统浏览器：--channel msedge（或 --channel chrome）。
脚本会自动检测这个错误并提示。

判定
----
- 单轮：Δ≥1 只说明「这个匿名会话的播放被计入」。
- 要判断是否有「独立配额池」，用 --rounds 跑多个匿名会话：若累计增量能突破
  网页端的 ~200 上限 → 移动端是独立池；若在 200 左右停住 → 全端共享同一配额。

用法
----
    python tools/probe_mobile_playback.py BV1xx411c7mD --channel msedge
    python tools/probe_mobile_playback.py BV1xx411c7mD --channel msedge --rounds 20 --seconds 20
    python tools/probe_mobile_playback.py BV1xx411c7mD --channel msedge --headed
    python tools/probe_mobile_playback.py BV1xx411c7mD --channel msedge --proxy 1.2.3.4:8080
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
for _sub in ("bili-booster", "bili-player", "bili-common"):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

# 移动端 Safari UA（iPhone）—— 让 B站 下发移动端 H5 页面
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
              "Mobile/15E148 Safari/604.1")
_MOBILE_VIEWPORT = {"width": 390, "height": 844}

# 命中即记录的播放上报相关请求（已实测的移动端真实入口）
_REPORT_KEYS = ("click-interface", "report/web/heartbeat", "report/click", "history/report", "/x/report")

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
Object.defineProperty(document, 'hidden', {get: () => false});
"""


def line(ch="-", n=64):
    print(ch * n)


def section(title):
    print()
    line("=")
    print(title)
    line("=")


def extract_bvid(text):
    m = re.search(r"(BV[0-9A-Za-z]{10})", text or "")
    return m.group(1) if m else (text or "").strip()


def get_view_count(bvid):
    """读播放量；测量失败返回 None（与 0 区分，避免把读失败误判为「没涨」）。"""
    try:
        resp = httpx.get(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers={"User-Agent": _MOBILE_UA},
            timeout=10,
        )
        return int(resp.json().get("data", {}).get("stat", {}).get("view", 0))
    except Exception:
        return None


async def _video_state(page):
    """读取 video 元素的真实状态，判断「页面打开了但根本没播」这类问题。"""
    try:
        return await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                if (!v) return {found: false};
                return {
                    found: true,
                    paused: v.paused,
                    currentTime: Number(v.currentTime.toFixed(2)),
                    duration: Number((v.duration || 0).toFixed(2)),
                    readyState: v.readyState,
                    networkState: v.networkState,
                    error: v.error ? (v.error.code + ':' + v.error.message) : null,
                    src: (v.currentSrc || v.src || '').slice(0, 80),
                };
            }
        """)
    except Exception as e:
        return {"found": False, "err": str(e)[:80]}


async def _kick_play(page):
    """尽量让视频处于播放状态：muted 后 play()，再兜底点播放按钮。"""
    try:
        await page.evaluate("""
            () => {
                const v = document.querySelector('video');
                if (v) { v.muted = true; if (v.paused) v.play().catch(() => {}); }
            }
        """)
    except Exception:
        pass
    for sel in ('.bpx-player-ctrl-play', '.bilibili-player-video-btn-start',
                '.mplayer-play-btn', '.play-btn', '.bili-video-card__play'):
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click(timeout=1500)
                break
        except Exception:
            continue


async def play_one_session(args, bvid, verbose):
    """跑一个匿名移动端会话（全新 context = 全新 buvid/did），返回统计信息。"""
    captured = []
    all_xhr = set()
    title = ""
    last_state = {}

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": not args.headed,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if args.channel:
            launch_kwargs["channel"] = args.channel
        browser = await p.chromium.launch(**launch_kwargs)

        ctx_kwargs = {
            "viewport": _MOBILE_VIEWPORT,
            "user_agent": _MOBILE_UA,
            "locale": "zh-CN",
            "is_mobile": True,
            "has_touch": True,
            "device_scale_factor": 3,
        }
        if args.proxy:
            ctx_kwargs["proxy"] = {"server": f"http://{args.proxy}"}
        context = await browser.new_context(**ctx_kwargs)
        await context.add_init_script(_STEALTH_JS)
        page = await context.new_page()

        def on_request(req):
            if "bilibili.com/x/" in req.url:
                all_xhr.add(req.url.split("?")[0])
            if any(k in req.url for k in _REPORT_KEYS):
                captured.append({
                    "url": req.url, "method": req.method, "post": req.post_data,
                    "headers": dict(req.headers),
                    "status": None, "resp_code": None, "resp_body": None,
                })

        async def on_response(resp):
            if not any(k in resp.url for k in _REPORT_KEYS):
                return
            body, code = None, None
            try:
                body = await resp.text()
                try:
                    code = json.loads(body).get("code")
                except Exception:
                    pass
            except Exception:
                pass
            for item in reversed(captured):
                if item["url"] == resp.url and item["status"] is None:
                    item["status"] = resp.status
                    item["resp_code"] = code
                    item["resp_body"] = (body or "")[:200]
                    break

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(f"https://m.bilibili.com/video/{bvid}",
                            wait_until="domcontentloaded", timeout=45000)
            title = await page.title()
            await page.wait_for_timeout(4000)
            await _kick_play(page)
            last_state = await _video_state(page)
            if verbose:
                print(f"  视频状态(起播后): {last_state}")

            elapsed = 0
            while elapsed < args.seconds:
                await page.wait_for_timeout(3000)
                elapsed += 3
                # 每 15s 保活一次，防止移动端页面自动暂停
                if elapsed % 15 == 0:
                    await _kick_play(page)
                    last_state = await _video_state(page)
                    if verbose:
                        print(f"  ...已播放 {elapsed}/{args.seconds}s  状态: {last_state}")
            await page.wait_for_timeout(3000)  # 等最后一次上报落地
        except Exception as e:
            print(f"[!] 页面处理异常: {e}")
        finally:
            await context.close()
            await browser.close()

    return {
        "captured": captured, "xhr": all_xhr, "title": title, "state": last_state,
        "click": sum(1 for c in captured if "click-interface" in c["url"]),
        "hb": sum(1 for c in captured if "heartbeat" in c["url"]),
    }


async def run(args):
    bvid = extract_bvid(args.bvid)
    if not bvid.startswith("BV"):
        print(f"[X] 无法识别 BV 号: {args.bvid!r}")
        return

    section(f"试探 A · 移动端 H5 播放配额  |  {bvid}")
    print(f"目标地址 : https://m.bilibili.com/video/{bvid}")
    print(f"身份     : 匿名（每轮全新 context = 全新 buvid/did）")
    print(f"浏览器   : {args.channel or 'Playwright 自带 chromium（缺编解码器，多半播不了）'}"
          f"{'  headed' if args.headed else '  headless'}")
    print(f"出口     : {args.proxy or '直连'}")
    print(f"轮次     : {args.rounds}    每轮播放: {args.seconds}s")

    before = get_view_count(bvid)
    print(f"初始播放量: {before if before is not None else '读取失败'}")

    prev = before
    last = {}
    for r in range(1, args.rounds + 1):
        verbose = (r == 1)
        if args.rounds > 1:
            print(f"\n-- 第 {r}/{args.rounds} 轮 --")
        last = await play_one_session(args, bvid, verbose)

        if verbose:
            section("第 1 轮抓到的播放上报请求")
            print(f"页面标题: {last['title']}")
            if not last["captured"]:
                print("[!] 一条上报请求都没抓到。")
            for it in last["captured"]:
                print(f"  {it['method']} {it['url'][:110]}")
                if it["post"]:
                    print(f"      post: {it['post']}")
                if args.dump_headers:
                    for k, v in (it.get("headers") or {}).items():
                        print(f"      hdr {k}: {v}")
                print(f"      HTTP={it['status']}  code={it['resp_code']}  body={it['resp_body']}")
            section("页面实际发出的 bilibili.com/x/ 请求路径（供核对上报入口）")
            for u in sorted(last["xhr"]):
                print(f"  {u}")

        await asyncio.sleep(args.settle)  # 等统计落地
        cur = get_view_count(bvid)
        if cur is None:
            print(f"  第 {r} 轮: 播放量读取失败（上报 click={last['click']} hb={last['hb']}）")
            continue
        print(f"  第 {r} 轮: {prev} -> {cur}  (Δ={cur - prev:+d})  "
              f"上报 click={last['click']} hb={last['hb']}  "
              f"video.error={last['state'].get('error')}")
        prev = cur

    after = prev
    section("结论")
    state = last.get("state") or {}
    if state.get("error"):
        print("[X] 视频没有真正播放，播放器报错：")
        print(f"    {state['error']}")
        print("    => Playwright 自带 chromium/chromium_headless_shell 缺 H.264/AAC 编解码器。")
        print("    => 本脚本必须加 --channel msedge（或 --channel chrome），本次结论无效。")
        return
    if before is None or after is None:
        print("[!] 播放量读取失败，无法判定，请稍后重试。")
        return
    delta = after - before
    print(f"播放量总变化: {before} -> {after}  (Δ = {delta:+d})")
    print(f"最后一轮上报: click/h5={last['click']}  heartbeat={last['hb']}")
    if not last["captured"]:
        print("[!] 没抓到任何播放上报 —— 页面可能没真正播放，试 --headed 观察。")
    if args.rounds == 1:
        if delta >= 1:
            print("[√] 移动端匿名播放被计入（1 个匿名会话 ≈ 1 次播放）。")
            print("    但这只证明「能计」，不能证明有独立配额池；用 --rounds 跑多轮看累计增量是否突破 200。")
        else:
            print("[×] 播放量未变化。看上方上报 code 区分：")
            print("    -352=风控 / -412=IP 拦截 / 0=接受但未计数（疑似 aid 级配额已满或设备被判重）。")
    else:
        print(f"多轮累计增量 Δ={delta:+d}（{args.rounds} 个匿名会话）。")
        if delta > 200:
            print("[√] 累计已突破 200 → 移动端 H5 存在独立于网页端的配额池。")
        elif delta >= args.rounds:
            print("[?] 基本是「1 会话 1 播放」在累积，未触及上限，加更多轮次再看是否在 200 附近停住。")
        else:
            print("[×] 累计增量远小于会话数 → 大量会话被判重/未计数，配额可能已满或风控生效。")


def parse_args():
    ap = argparse.ArgumentParser(description="试探 A：移动端 H5 播放配额")
    ap.add_argument("bvid", help="BV 号或含 BV 号的视频链接")
    ap.add_argument("--rounds", type=int, default=1, help="匿名会话轮数（每轮一个新设备身份），默认 1")
    ap.add_argument("--seconds", type=int, default=65, help="每轮模拟播放时长（秒），默认 65")
    ap.add_argument("--settle", type=int, default=20, help="每轮结束后等统计落地的秒数，默认 20")
    ap.add_argument("--proxy", default=None, help="可选出口代理 ip:port；不填则直连")
    ap.add_argument("--headed", action="store_true", help="有头模式（肉眼观察页面是否真的在播放）")
    ap.add_argument("--dump-headers", action="store_true",
                    help="打印上报请求的完整请求头（用于纯 HTTP 复刻）")
    ap.add_argument("--channel", default=None,
                    help="浏览器通道。必填 msedge 或 chrome —— Playwright 自带的 chromium 缺 "
                         "H.264/AAC 编解码器，B站 流会被判不支持并切到 error.mp4")
    return ap.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
