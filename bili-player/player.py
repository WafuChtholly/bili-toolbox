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

# 配置文件统一放到项目 data 目录，避免 cookie 泄露在源码目录
CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "player_config.yaml"
_OLD_CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"


def _ensure_config():
    """迁移旧配置到 data 目录"""
    if not CONFIG_FILE.exists() and _OLD_CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(str(_OLD_CONFIG_FILE), str(CONFIG_FILE))


def _build_cookies(account: dict) -> list[dict]:
    """把单个账号配置转成 Playwright cookie 列表。"""
    sessdata = account.get("sessdata", "")
    bili_jct = account.get("bili_jct", "")
    buvid3 = account.get("buvid3", "")
    login_uid = str(account.get("login_uid", ""))

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


def load_player_config() -> dict:
    """读取完整 player 配置。"""
    _ensure_config()
    import yaml
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def load_accounts(cfg: dict = None) -> list[list[dict]]:
    """从 player_config.yaml 读取登录账号，返回每个账号的 cookie 列表。"""
    if cfg is None:
        cfg = load_player_config()

    accounts = []

    # 新配置：多账号列表
    account_list = cfg.get("bilibili_accounts", [])
    for acc in account_list:
        cookies = _build_cookies(acc)
        if cookies:
            accounts.append(cookies)

    # 兼容旧配置：单个 bilibili 字段
    if not accounts:
        bilibili = cfg.get("bilibili", {})
        cookies = _build_cookies(bilibili)
        if cookies:
            accounts.append(cookies)

    return accounts if accounts else [[]]


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
        accounts = load_accounts()
        cookies = accounts[0] if accounts else []
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
            ],
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
    主入口（异步版本，多账号并行、账号内串行）

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

    bvids = [extract_bvid(b) for b in bvid_input.split(',') if b.strip()]
    if not bvids:
        log("[PLAYER] 未提供有效的 BV 号")
        return

    cfg = load_player_config()
    accounts = load_accounts(cfg)

    # 取第一个有效账号的 sessdata 用于 API 查询（播放量对所有账号相同）
    sessdata = ""
    for cookies in accounts:
        for c in cookies:
            if c["name"] == "SESSDATA":
                sessdata = c["value"]
                break
        if sessdata:
            break

    # 记录每个视频的初始播放量
    initial_views = {}
    for bvid in bvids:
        initial_views[bvid] = get_view_count_api(bvid, sessdata)
        log(f"[PLAYER] {bvid} 初始播放量: {initial_views[bvid]}")

    total_planned = len(bvids) * rounds * len(accounts)
    stagger_seconds = int(cfg.get("stagger_seconds", 10))
    log("")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log(f"[PLAYER] 🚀 播放任务启动")
    log(f"[PLAYER]    账号数: {len(accounts)} | 视频数: {len(bvids)} | 每视频轮次: {rounds} | 总计: {total_planned} 轮")
    log(f"[PLAYER]    执行方式: 账号间并行，账号内串行")
    log(f"[PLAYER]    错开策略: 每个账号启动间隔 {stagger_seconds}-{stagger_seconds + 5} 秒（可用 stagger_seconds 调整）")
    for i, bvid in enumerate(bvids, 1):
        log(f"[PLAYER]    [{i}/{len(bvids)}] {bvid} | 初始播放量: {initial_views.get(bvid, '?')}")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log("")

    start_time = time.time()
    total_rounds = 0
    success_rounds = 0
    failed_rounds = 0
    video_results = {}  # bvid -> {"rounds": n, "success": n, "failed": n, "last_views": int}
    results_lock = asyncio.Lock()

    async def play_for_account(account_idx: int, cookies: list[dict]):
        """单个账号的串行播放任务。"""
        account_label = f"账号{account_idx + 1}"
        nonlocal total_rounds, success_rounds, failed_rounds

        # 账号间错开启动，避免多个账号同时播放同一个视频
        stagger = account_idx * (stagger_seconds + random.randint(0, 5))
        if stagger > 0:
            log(f"[PLAYER] [{account_label}] 延迟 {stagger} 秒启动")
            await asyncio.sleep(stagger)

        for bvid in bvids:
            if stop_event and stop_event.is_set():
                log(f"[PLAYER] [{account_label}] 收到停止信号，退出")
                return

            async with results_lock:
                video_results.setdefault(bvid, {"rounds": 0, "success": 0, "failed": 0, "last_views": None})

            for round_num in range(1, rounds + 1):
                if stop_event and stop_event.is_set():
                    log(f"[PLAYER] [{account_label}] 收到停止信号，退出")
                    return

                async with results_lock:
                    total_rounds += 1
                    video_results[bvid]["rounds"] += 1

                log(f"[PLAYER] [{account_label}] ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
                log(f"[PLAYER] [{account_label}] 📌 {bvid} | 第 {round_num}/{rounds} 轮")

                try:
                    cur_views = await play_video(
                        bvid,
                        stop_event=stop_event,
                        log_fn=lambda m: log(f"[PLAYER] [{account_label}] {m}"),
                        initial_views=initial_views.get(bvid),
                        cookies=cookies,
                    )
                    async with results_lock:
                        success_rounds += 1
                        video_results[bvid]["success"] += 1
                        if cur_views is not None:
                            video_results[bvid]["last_views"] = cur_views

                    growth = 0
                    if cur_views is not None:
                        growth = max(0, cur_views - initial_views.get(bvid, cur_views))
                        log(f"[RESULT] video|{bvid}|{cur_views}|{growth}|{round_num}|{rounds}")

                    async with results_lock:
                        total_growth = 0
                        for bv, res in video_results.items():
                            if res.get("last_views") is not None:
                                total_growth += max(0, res["last_views"] - initial_views.get(bv, res["last_views"]))
                        log(f"[RESULT] round|{success_rounds + failed_rounds}|{total_planned}|{success_rounds}|{failed_rounds}|{total_growth}")
                except Exception as e:
                    log(f"[PLAYER] [{account_label}] 播放失败: {e}")
                    async with results_lock:
                        failed_rounds += 1
                        video_results[bvid]["failed"] += 1
                        total_growth = 0
                        for bv, res in video_results.items():
                            if res.get("last_views") is not None:
                                total_growth += max(0, res["last_views"] - initial_views.get(bv, res["last_views"]))
                        log(f"[RESULT] round|{success_rounds + failed_rounds}|{total_planned}|{success_rounds}|{failed_rounds}|{total_growth}")

    # 账号间并行执行
    tasks = [play_for_account(i, cookies) for i, cookies in enumerate(accounts)]
    if tasks:
        log(f"[PLAYER] 启动 {len(tasks)} 个账号并行任务...")
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── 结果汇总 ──
    elapsed = int(time.time() - start_time)
    log("")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log(f"[PLAYER] 📊 播放结果汇总")
    log(f"[PLAYER] ━━━━━━━━━━━━━━━━━━━━━━━━")
    log(f"[PLAYER] 📹 视频数量: {len(bvids)}")
    log(f"[PLAYER] 👤 账号数量: {len(accounts)}")
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
