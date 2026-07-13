"""
B站播放量提升 — Playwright 模拟浏览器播放
通过 Playwright 打开视频页面，模拟真实用户行为增加播放量
"""
import os
import random
import re
import sys
import time
import logging
import threading
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

# 抑制 httpx 的 HTTP 请求日志
logging.getLogger("httpx").setLevel(logging.WARNING)

CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"


def load_cookies() -> list[dict]:
    """从 config.yaml 读取登录 cookie，转为 Playwright cookie 格式"""
    import yaml
    if not CONFIG_FILE.exists():
        return []
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        bilibili = cfg.get("bilibili", {})
        sessdata = bilibili.get("sessdata", "")
        bili_jct = bilibili.get("bili_jct", "")
        buvid3 = bilibili.get("buvid3", "")
        login_uid = str(bilibili.get("login_uid", ""))

        if not sessdata:
            return []

        cookies = [
            {"name": "SESSDATA", "value": sessdata, "domain": ".bilibili.com", "path": "/"},
            {"name": "bili_jct", "value": bili_jct, "domain": ".bilibili.com", "path": "/"},
        ]
        if buvid3:
            cookies.append({"name": "buvid3", "value": buvid3, "domain": ".bilibili.com", "path": "/"})
        if login_uid:
            cookies.append({"name": "DedeUserID", "value": login_uid, "domain": ".bilibili.com", "path": "/"})
        return cookies
    except Exception:
        return []


def extract_bvid(url_or_bvid: str) -> str:
    """从 URL 或直接的 BV 号中提取 bvid"""
    url_or_bvid = url_or_bvid.strip()
    # 匹配 BV 开头的 ID
    m = re.search(r'(BV[\w]{10})', url_or_bvid, re.IGNORECASE)
    if m:
        return m.group(1)
    return url_or_bvid


def get_view_count_api(bvid: str, sessdata: str = "") -> int:
    """通过 B站 API 获取视频播放量（更可靠）"""
    import httpx
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        cookies = {}
        if sessdata:
            cookies["SESSDATA"] = sessdata
        resp = httpx.get(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
            headers=headers, cookies=cookies, timeout=10,
        )
        data = resp.json().get("data", {})
        return int(data.get("stat", {}).get("view", 0))
    except Exception:
        return 0


def get_view_count(page) -> int:
    """从页面获取播放量"""
    try:
        # 尝试多种选择器获取播放量
        selectors = [
            '.view-text',
            '.video-info-detail .item:last-child span',
            '[class*="view"]',
        ]
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    # 提取数字
                    num_match = re.search(r'([\d.]+)\s*万?', text)
                    if num_match:
                        num = float(num_match.group(1))
                        if '万' in text:
                            num *= 10000
                        return int(num)
            except Exception:
                continue

        # 兜底：从 meta 标签获取
        meta = page.query_selector('meta[itemprop="interactionCount"]')
        if meta:
            return int(meta.get_attribute('content') or '0')

        # 再兜底：从页面文本搜索
        content = page.content()
        m = re.search(r'"view":\s*(\d+)', content)
        if m:
            return int(m.group(1))

    except Exception:
        pass
    return 0


async def simulate_user_behavior(page, stop_event=None):
    """模拟真实用户行为"""
    if stop_event and stop_event.is_set():
        return

    try:
        # 模拟页面滚动
        scroll_actions = random.randint(1, 3)
        for _ in range(scroll_actions):
            if stop_event and stop_event.is_set():
                return
            scroll_y = random.randint(100, 400)
            await page.evaluate(f"window.scrollBy(0, {scroll_y})")
            await page.wait_for_timeout(random.randint(1000, 3000))

        # 模拟鼠标移动到视频区域
        if stop_event and stop_event.is_set():
            return
        try:
            video = await page.query_selector('video')
            if video:
                box = await video.bounding_box()
                if box:
                    # 随机移动到视频区域内的某个位置
                    x = box['x'] + random.uniform(box['width'] * 0.2, box['width'] * 0.8)
                    y = box['y'] + random.uniform(box['height'] * 0.2, box['height'] * 0.8)
                    await page.mouse.move(x, y)
                    await page.wait_for_timeout(random.randint(500, 1500))
        except Exception:
            pass

        # 偶尔暂停再播放（模拟真实行为）
        if stop_event and stop_event.is_set():
            return
        if random.random() < 0.3:
            try:
                await page.keyboard.press('k')  # B站快捷键：暂停
                await page.wait_for_timeout(random.randint(1000, 3000))
                await page.keyboard.press('k')  # 继续播放
            except Exception:
                pass

    except Exception:
        pass


async def play_video(bvid: str, stop_event=None, log_fn=None, initial_views=None, cookies=None):
    """
    用 Playwright 打开一个视频并模拟播放（异步版本）

    Args:
        bvid: 视频 BV 号
        stop_event: 停止信号
        log_fn: 日志回调函数
        initial_views: 所有轮次开始前的初始播放量（用于计算总变化）
        cookies: 可选，外部传入的 Playwright cookie 列表（优先级高于 config.yaml）
    """
    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    url = f"https://www.bilibili.com/video/{bvid}"
    log(f"[PLAYER] 打开视频: {url}")

    # 使用外部传入的 cookie，或读取本地配置
    if cookies is None:
        cookies = load_cookies()
    sessdata = ""
    for c in cookies:
        if c["name"] == "SESSDATA":
            sessdata = c["value"]
            break

    _result_views = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
            ]
        )
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='zh-CN',
        )
        # 注入反检测脚本
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        # 注入登录 cookie
        if cookies:
            await context.add_cookies(cookies)
            log("[PLAYER] 已注入登录 Cookie")
        else:
            log("[PLAYER] 未登录，将以游客身份播放")

        page = await context.new_page()

        try:
            # 访问视频页面
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(random.randint(3000, 5000))

            if stop_event and stop_event.is_set():
                return

            # 模拟播放时长（10-20秒，低于10秒不计入播放）
            play_duration = random.randint(10, 20)
            log(f"[PLAYER] 模拟播放 {play_duration} 秒...")

            elapsed = 0
            while elapsed < play_duration:
                if stop_event and stop_event.is_set():
                    log("[PLAYER] 收到停止信号")
                    break

                # 每 3-8 秒模拟一次用户行为
                wait_time = random.randint(3, 8)
                await page.wait_for_timeout(wait_time * 1000)
                elapsed += wait_time

                await simulate_user_behavior(page, stop_event)
                log(f"[PLAYER] 已播放 {elapsed}/{play_duration} 秒")

            # 通过 API 获取当前播放量，与初始值对比
            current_views = get_view_count_api(bvid, sessdata)
            _result_views = current_views
            if initial_views is not None:
                diff = current_views - initial_views
                log(f"[PLAYER] 当前播放量: {current_views} (累计变化: +{max(0, diff)}, 初始: {initial_views})")
            else:
                log(f"[PLAYER] 当前播放量: {current_views}")

        except Exception as e:
            log(f"[PLAYER] 播放出错: {e}")
        finally:
            await browser.close()
            log("[PLAYER] 浏览器已关闭")

    return _result_views


async def main(bvid_input: str, rounds: int = 1, stop_event: threading.Event = None, log_fn=None):
    """
    主入口（异步版本，支持并发播放）

    Args:
        bvid_input: BV号，逗号分隔
        rounds: 每个视频播放轮数
        stop_event: 停止信号
        log_fn: 可选日志回调函数，提供时替代 print() 输出
    """
    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg)

    MAX_CONCURRENT = 3  # 最大并发数限制

    bvids = [extract_bvid(b) for b in bvid_input.split(',') if b.strip()]
    if not bvids:
        log("[PLAYER] 未提供有效的 BV 号")
        return

    # 读取 sessdata 用于 API 查询
    cookies = load_cookies()
    sessdata = ""
    for c in cookies:
        if c["name"] == "SESSDATA":
            sessdata = c["value"]
            break

    # 记录每个视频的初始播放量
    initial_views = {}
    for bvid in bvids:
        initial_views[bvid] = get_view_count_api(bvid, sessdata)
        log(f"[PLAYER] {bvid} 初始播放量: {initial_views[bvid]}")

    log(f"[PLAYER] 准备播放 {len(bvids)} 个视频，每轮 {rounds} 次")
    log(f"[PLAYER] BV 列表: {', '.join(bvids)}")
    log(f"[PLAYER] 最大并发数: {MAX_CONCURRENT}")

    # ── 任务概览 ──
    total_planned = len(bvids) * rounds
    log("")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log(f"[PLAYER] 🚀 播放任务启动")
    log(f"[PLAYER]    视频数: {len(bvids)} | 每视频轮次: {rounds} | 总计: {total_planned} 轮")
    log(f"[PLAYER]    并发数: {MAX_CONCURRENT}")
    for i, bvid in enumerate(bvids, 1):
        log(f"[PLAYER]    [{i}/{len(bvids)}] {bvid} | 初始播放量: {initial_views.get(bvid, '?')}")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log("")

    start_time = time.time()
    total_rounds = 0
    success_rounds = 0
    failed_rounds = 0
    video_results = {}  # bvid -> {"rounds": n, "success": n, "failed": n}
    results_lock = asyncio.Lock()

    # 创建信号量限制并发数
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def play_with_semaphore(bvid, round_num):
        """带并发限制的播放任务"""
        nonlocal total_rounds, success_rounds, failed_rounds

        async with semaphore:
            if stop_event and stop_event.is_set():
                return

            async with results_lock:
                total_rounds += 1
                video_results[bvid]["rounds"] += 1
                overall = sum(r["success"] + r["failed"] for r in video_results.values())

            log(f"[PLAYER] ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
            log(f"[PLAYER] 📌 {bvid} | 第 {round_num}/{rounds} 轮")

            try:
                cur_views = await play_video(bvid, stop_event=stop_event, log_fn=log, initial_views=initial_views.get(bvid), cookies=cookies)
                async with results_lock:
                    success_rounds += 1
                    video_results[bvid]["success"] += 1
                # ── RESULT 标记 ──
                growth = 0
                if cur_views is not None:
                    growth = max(0, cur_views - initial_views.get(bvid, cur_views))
                    async with results_lock:
                        video_results[bvid]["last_views"] = cur_views
                    log(f"[RESULT] video|{bvid}|{cur_views}|{growth}|{round_num}|{rounds}")
                # 计算所有视频的真实总增长
                async with results_lock:
                    total_growth = 0
                    for bv, res in video_results.items():
                        if res.get("last_views") is not None:
                            total_growth += max(0, res["last_views"] - initial_views.get(bv, res["last_views"]))
                    log(f"[RESULT] round|{success_rounds + failed_rounds}|{total_planned}|{success_rounds}|{failed_rounds}|{total_growth}")
            except Exception as e:
                log(f"[PLAYER] 播放失败: {e}")
                async with results_lock:
                    failed_rounds += 1
                    video_results[bvid]["failed"] += 1
                    total_growth = 0
                    for bv, res in video_results.items():
                        if res.get("last_views") is not None:
                            total_growth += max(0, res["last_views"] - initial_views.get(bv, res["last_views"]))
                    log(f"[RESULT] round|{success_rounds + failed_rounds}|{total_planned}|{success_rounds}|{failed_rounds}|{total_growth}")

    # 创建所有任务
    tasks = []
    for bvid in bvids:
        if stop_event and stop_event.is_set():
            log("[PLAYER] 收到停止信号，退出")
            break

        video_results.setdefault(bvid, {"rounds": 0, "success": 0, "failed": 0, "last_views": None})

        for round_num in range(1, rounds + 1):
            if stop_event and stop_event.is_set():
                log("[PLAYER] 收到停止信号，退出")
                break
            tasks.append(play_with_semaphore(bvid, round_num))

    # 并发执行所有任务
    if tasks:
        log(f"[PLAYER] 启动 {len(tasks)} 个并发任务...")
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── 结果汇总 ──
    elapsed = int(time.time() - start_time)
    log("")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log(f"[PLAYER] 📊 播放结果汇总")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log(f"[PLAYER] 📹 视频数量: {len(bvids)}")
    log(f"[PLAYER] 🔄 总轮次: {total_rounds}")
    log(f"[PLAYER] ✅ 成功: {success_rounds} / ❌ 失败: {failed_rounds}")
    log(f"[PLAYER] ⏱️ 总耗时: {elapsed // 60}分{elapsed % 60}秒")
    if video_results:
        log(f"[PLAYER] ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
        log(f"[PLAYER] 📋 各视频详情:")
        for bvid, res in video_results.items():
            growth = ""
            try:
                current = get_view_count_api(bvid, sessdata)
                init = initial_views.get(bvid, 0)
                diff = current - init
                growth = f" | 播放量: {init} → {current} (+{diff})"
            except Exception:
                pass
            log(f"[PLAYER]   {bvid}: 播放 {res['rounds']} 轮 (成功 {res['success']}/失败 {res['failed']}){growth}")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python player.py <bvid1,bvid2,...> [rounds]")
        sys.exit(1)

    bv_input = sys.argv[1]
    r = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    asyncio.run(main(bv_input, rounds=r))
