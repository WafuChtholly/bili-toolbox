"""
B站稿件监控 — 定时对稿件页面截图存档
输入 BV 号后，每隔固定间隔（默认每小时）用 Playwright 打开稿件页面整页截图，
同时用百度搜索「北京时间」截取真实时钟小图，以小浮窗形式叠加到稿件截图右上角，
并写入本地存档索引。

说明：
- playwright 为可选依赖（Win7 环境未安装），未安装时截图会返回友好错误。
- 浮窗合成依赖 PIL（qrcode[pil] / pillow 已随 WebUI 安装），缺失时退回纯稿件截图。
- BV 号区分大小写，处理时只统一 BV 前缀，不改动其余字符。
"""
# 兼容 Python 3.8 (Win7)：list[str] / X | None 等注解语法延迟求值
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

# 北京时间时区（UTC+8），与机器本地时区无关
BEIJING_TZ = timezone(timedelta(hours=8))

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 每个 BV 在本地索引中最多保留的条数，防止索引无限膨胀
MAX_ENTRIES_PER_BV = 200


def bj_now() -> datetime:
    """当前北京时间。"""
    return datetime.now(BEIJING_TZ)


def bj_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return bj_now().strftime(fmt)


def extract_bvid(url_or_bvid: str) -> str:
    """从 URL 或纯文本中提取 BV 号。

    注意：BV 号区分大小写（base58），只把前缀统一成大写 BV，
    其余 10 位字符必须保持原样，否则会变成无效稿件被 B 站重定向到主页。
    """
    m = re.search(r"(BV[0-9A-Za-z]{10})", str(url_or_bvid or ""), re.IGNORECASE)
    if m:
        s = m.group(1)
        return "BV" + s[2:]
    return str(url_or_bvid or "").strip()


def is_valid_bvid(bvid: str) -> bool:
    """校验是否为规范的 12 位 BV 号（区分大小写）。"""
    return bool(re.match(r"^BV[0-9A-Za-z]{10}$", str(bvid or "")))


def build_video_url(bvid: str) -> str:
    """构造稿件页面 URL，BV 前固定带 /video/ 前缀。"""
    return f"https://www.bilibili.com/video/{extract_bvid(bvid)}"


def _build_cookies(cookies: dict | None) -> list:
    """把登录 cookies 字典转成 Playwright cookie 列表（挂到 .bilibili.com 域下）。"""
    if not cookies:
        return []
    out = []
    for name, value in (cookies or {}).items():
        if value:
            out.append({"name": name, "value": str(value),
                        "domain": ".bilibili.com", "path": "/"})
    return out


def _merge_images(video_path: Path, clock_path: Path, out_path: Path) -> bool:
    """把百度时钟截图缩小成小浮窗，叠加到稿件截图右上角，返回是否成功。"""
    try:
        from PIL import Image, ImageOps
    except Exception:
        return False
    try:
        base = Image.open(str(video_path)).convert("RGB")
        clock = Image.open(str(clock_path))
        # 透明底先铺白底，避免 convert("RGB") 后出现黑块
        if clock.mode in ("RGBA", "LA", "P"):
            clock = clock.convert("RGBA")
            bg = Image.new("RGB", clock.size, (255, 255, 255))
            bg.paste(clock, mask=clock.split()[-1])
            clock = bg
        else:
            clock = clock.convert("RGB")
        # 浮窗宽度限制在稿件截图宽度的 1/4 以内（且不超过 360px）
        max_w = min(360, max(200, base.width // 4))
        if clock.width > max_w:
            new_h = max(1, round(clock.height * max_w / clock.width))
            clock = clock.resize((max_w, new_h), Image.LANCZOS)
        margin = 12
        x = base.width - clock.width - margin
        y = margin
        # 加一圈浅灰描边，呈现小浮窗效果
        clock = ImageOps.expand(clock, border=2, fill=(190, 190, 190))
        base.paste(clock, (x - 2, y - 2))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        base.save(str(out_path), quality=92)
        return True
    except Exception:
        return False


# 通用时钟定位 JS：先找文本恰好为「时:分:秒」的实时数字元素，再向上爬父级，
# 取尺寸适中的祖先容器；优先选含「UTC+8」的（百度时钟卡片独有特征）
_CLOCK_EL_JS = """() => {
  const re = /^\\s*\\d{1,2}:\\d{2}:\\d{2}\\s*$/;
  const els = document.querySelectorAll('div,span,b,p,time,font');
  const cands = [];
  for (const el of els) {
    if (!re.test(el.textContent || '')) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 30 || r.height < 15) continue;
    cands.push(el);
  }
  for (const needUTC of [true, false]) {
    for (const el of cands) {
      let cur = el;
      for (let i = 0; i < 6 && cur; i++) {
        cur = cur.parentElement;
        if (!cur) break;
        const r = cur.getBoundingClientRect();
        if (r.width < 200 || r.width > 900 || r.height < 80 || r.height > 500) continue;
        const t = cur.textContent || '';
        if (needUTC && t.indexOf('UTC+8') === -1) continue;
        return cur;
      }
    }
  }
  return cands.length ? cands[0] : null;
}"""


async def _capture_baidu_time(context, out_file: Path, log) -> bool:
    """截取真实时钟小图（多级时间源：百度搜索 → 360搜索 → time.is），
    只截时钟卡片小区域，返回是否成功。"""
    q = quote("北京时间")
    sources = (
        (f"https://www.baidu.com/s?wd={q}", 2500),
        (f"https://www.so.com/s?q={q}", 2000),
        ("https://time.is/Beijing", 1500),
    )
    for idx, (url, wait_ms) in enumerate(sources):
        try:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(wait_ms)
                handle = None
                try:
                    handle = await page.evaluate_handle(_CLOCK_EL_JS)
                except Exception:
                    handle = None
                el = handle.as_element() if handle is not None else None
                if el is not None:
                    try:
                        await el.scroll_into_view_if_needed(timeout=5000)
                        await page.wait_for_timeout(300)
                        await el.screenshot(path=str(out_file))
                        return True
                    except Exception:
                        pass
            finally:
                await page.close()
        except Exception:
            pass
        if idx < len(sources) - 1:
            log("  ⚠️ 该时间源未截到时钟卡片，换下一个时间源...")
    log("  ⚠️ 未能截取真实时钟（所有时间源都失败）")
    return False


async def capture_video(bvid: str, out_dir: Path, log=None, stop_event=None,
                        cookies: dict | None = None) -> dict:
    """打开稿件页面整页截图 + 百度「北京时间」时钟截图，上下拼接后存到 out_dir/{bvid}/。

    cookies: 可选登录 cookies（SESSDATA / bili_jct / DedeUserID / buvid3），
             传入后稿件页面将以登录态访问。
    返回 {"ok": bool, "file": str|None, "ts": str, "title": str, "error": str}
    """
    log = log or (lambda m: None)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ok": False, "file": None,
                "ts": bj_str(), "title": "",
                "error": "未安装 playwright，请先安装依赖（pip install playwright && playwright install chromium）"}

    # URL 固定带 /video/ 前缀
    url = build_video_url(bvid)
    ts = bj_now()
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
    title = ""
    saved = None
    error = ""
    tmp_dir = Path(tempfile.mkdtemp(prefix="bili_monitor_"))
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    user_agent=BROWSER_UA,
                    locale="zh-CN",
                )
                # 注入登录 cookie，以登录态访问稿件页面
                if cookies:
                    try:
                        await context.add_cookies(_build_cookies(cookies))
                    except Exception as e:
                        log(f"  ⚠️ 注入 cookie 失败（将匿名访问）: {e}")
                # ① 稿件页面整页截图
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(4000)
                # 若被 B 站重定向（BV 无效等），记录实际 URL 便于排查
                try:
                    cur = page.url or ""
                    if "/video/" not in cur:
                        log(f"  ⚠️ 页面被重定向到 {cur}，请检查 BV 号大小写是否正确")
                except Exception:
                    pass
                if stop_event and stop_event.is_set():
                    return {"ok": False, "file": None, "ts": ts_str, "title": title, "error": "已停止"}
                # 等待标题渲染后抓取标题（超时不阻塞截图）
                try:
                    await page.wait_for_selector("#viewbox_report h1", timeout=12000)
                    await page.wait_for_timeout(800)
                    title = (await page.inner_text("#viewbox_report h1")).strip()
                except Exception:
                    pass
                # 回到顶部后整页截图
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)
                video_file = tmp_dir / "video.png"
                await page.screenshot(path=str(video_file), full_page=True)

                # ② 百度「北京时间」真实时钟截图
                time_file = tmp_dir / "time.png"
                time_ok = await _capture_baidu_time(context, time_file, log)

                # ③ 时钟小浮窗叠加到稿件截图右上角
                bvid_dir = out_dir / bvid
                bvid_dir.mkdir(parents=True, exist_ok=True)
                fname = ts.strftime("%Y%m%d_%H%M%S") + ".png"
                file_path = bvid_dir / fname
                if time_ok and _merge_images(video_file, time_file, file_path):
                    log(f"  ✅ {bvid} 已合成截图（北京时间小浮窗在右上角）: {file_path}（{ts_str}）")
                    saved = str(file_path)
                else:
                    # 时间截图/拼接失败时退回纯稿件截图
                    shutil.copyfile(video_file, file_path)
                    log(f"  ✅ {bvid} 截图已保存（无时间图）: {file_path}（{ts_str}）")
                    saved = str(file_path)
            finally:
                await browser.close()
    except Exception as e:
        error = str(e)
        log(f"  ⚠️ {bvid} 截图失败: {e}")
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
    return {"ok": bool(saved), "file": saved, "ts": ts_str, "title": title, "error": error}


def _append_index(index_path: Path, r: dict, bvid: str) -> None:
    """把一条截图记录追加到本地存档索引（每个 BV 保留最近 N 条）。"""
    try:
        entries = []
        if index_path.exists():
            try:
                entries = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        if not isinstance(entries, list):
            entries = []
        entries.append({
            "bvid": bvid,
            "ts": r.get("ts", ""),
            "file": r.get("file", ""),
            "title": r.get("title", ""),
        })
        by_bv: dict = {}
        for e in entries:
            by_bv.setdefault(str(e.get("bvid", "")), []).append(e)
        cleaned = []
        for _k, v in by_bv.items():
            cleaned.extend(v[-MAX_ENTRIES_PER_BV:])
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(cleaned, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


async def monitor_once(bvids, archive_dir: Path, index_path: Path,
                       log=None, stop_event=None, cookies: dict | None = None) -> list:
    """对一批 BV 号各截图一次，并写入本地存档索引。"""
    log = log or (lambda m: None)
    results = []
    for bvid in bvids:
        if stop_event is not None and stop_event.is_set():
            break
        log(f"  📸 开始截图: {bvid}")
        r = await capture_video(bvid, archive_dir, log=log, stop_event=stop_event,
                                cookies=cookies)
        results.append(r)
        if r.get("ok"):
            _append_index(index_path, r, bvid)
    return results


def run_monitor_loop(bvids, interval: float, archive_dir: Path, index_path: Path,
                     log=None, stop_event: threading.Event | None = None,
                     next_cb=None, cookies: dict | None = None) -> None:
    """线程入口：立即执行一次截图，之后按 interval 秒循环，直到 stop_event 被置位。

    next_cb(deadline_ts) 可选：每个周期开始时回调下次执行时间戳，供上层展示。
    cookies: 可选登录 cookies，传递给每次截图。
    """
    log = log or (lambda m: None)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while stop_event is None or not stop_event.is_set():
            log(f"━━━ 稿件监控执行 ({bj_str()}) ━━━")
            loop.run_until_complete(monitor_once(
                bvids, archive_dir, index_path, log=log, stop_event=stop_event,
                cookies=cookies))
            if stop_event is not None and stop_event.is_set():
                break
            deadline = time.time() + interval
            if next_cb:
                try:
                    next_cb(deadline)
                except Exception:
                    pass
            log(f"⏳ 已截完一轮，{int(interval)} 秒后再次执行（{bj_str()}）...")
            while stop_event is None or (not stop_event.is_set() and time.time() < deadline):
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
    finally:
        loop.close()
