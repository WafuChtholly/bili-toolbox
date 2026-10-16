# -*- coding: utf-8 -*-
"""端点存在性探测：B站除了 click/web/h5、click/h5 之外，还有没有别的「端」的计数入口。

背景
----
已确认：click/web/h5（网页端）与 click/h5（移动端）是**两份独立额度**（各约 200/稿件）。
本脚本用来找「第三份额度」属于哪个端点（TV / OTT / 车载 / 小程序 ...）。

做法
----
对一批候选 URL 各发少量 POST（本机直连，不计数，只看返回），打印 HTTP 状态 + code + message：
  - HTTP 404 / code=-404  -> 该路由不存在（排除）
  - code=0                -> 路由存在且接受参数（值得做代理池真计数验证）
  - code=-400/-101 等     -> 路由存在但参数不对（也可能可用，需补参数）

注意：直连只用本机一个 IP，**不会**让播放量上涨，所以这一步只能判「路由是否存在」，
不能判「是否独立计额度」。后者要另起代理池实测。

用法
----
    python tools/probe_endpoints.py BV1smJ26xEXP
    python tools/probe_endpoints.py BV1smJ26xEXP --n 3
"""
from __future__ import annotations

import argparse
import random
import re
import string
import sys
import time
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
_BOOSTER_DIR = str(ROOT / "bili-booster")
if _BOOSTER_DIR not in sys.path:
    sys.path.insert(0, _BOOSTER_DIR)

import booster  # noqa: E402

_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
              "Mobile/15E148 Safari/604.1")
# 云视听小电视（TV 端）常见 UA
_TV_UA = ("Mozilla/5.0 (Linux; Android 9; SHIELD Android TV Build/PPR1.180610.011) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/80.0.3987.99 "
          "Mobile Safari/537.36 BiliApp/1.0.0 os/tv")

# 候选端点：(标签, URL, UA)
CANDIDATES = [
    ("[对照] web/h5        ", "http://api.bilibili.com/x/click-interface/click/web/h5", _MOBILE_UA),
    ("[对照] h5            ", "http://api.bilibili.com/x/click-interface/click/h5", _MOBILE_UA),
    ("tv                  ", "http://api.bilibili.com/x/click-interface/click/tv", _TV_UA),
    ("ott                 ", "http://api.bilibili.com/x/click-interface/click/ott", _TV_UA),
    ("ios                 ", "http://api.bilibili.com/x/click-interface/click/ios", _MOBILE_UA),
    ("android             ", "http://api.bilibili.com/x/click-interface/click/android", _MOBILE_UA),
    ("android_tv_yst      ", "http://api.bilibili.com/x/click-interface/click/android_tv_yst", _TV_UA),
    ("bangumi             ", "http://api.bilibili.com/x/click-interface/click/bangumi", _MOBILE_UA),
    ("v2                  ", "http://api.bilibili.com/x/click-interface/click/v2", _MOBILE_UA),
    ("click(无后缀)        ", "http://api.bilibili.com/x/click-interface/click", _MOBILE_UA),
    ("h5/tv               ", "http://api.bilibili.com/x/click-interface/click/h5/tv", _TV_UA),
    ("report/click/h5     ", "http://api.bilibili.com/x/report/click/h5", _MOBILE_UA),
    ("report/click/web/h5 ", "http://api.bilibili.com/x/report/click/web/h5", _MOBILE_UA),
    ("click-interface/heartbeat", "http://api.bilibili.com/x/click-interface/web/heartbeat", _MOBILE_UA),
    ("report/web/heartbeat", "http://api.bilibili.com/x/report/web/heartbeat", _MOBILE_UA),
    ("[TV宿主] aisee click/h5 ", "https://api.snm0516.aisee.tv/x/click-interface/click/h5", _TV_UA),
    ("[TV宿主] aisee click/tv ", "https://api.snm0516.aisee.tv/x/click-interface/click/tv", _TV_UA),
    ("[TV宿主] tv.bili click/tv", "https://api.tv.bilibili.com/x/click-interface/click/tv", _TV_UA),
]


def line(ch="-", n=78):
    print(ch * n)


def extract_bvid(text):
    m = re.search(r"(BV[0-9A-Za-z]{10})", text or "")
    return m.group(1) if m else (text or "").strip()


def rand_did():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def make_data(info, bvid, spmid):
    now = int(time.time())
    return {
        "aid": info["aid"],
        "cid": info["cid"],
        "bvid": bvid,
        "part": "1",
        "mid": info.get("owner", {}).get("mid", ""),
        "lv": "0",
        "did": rand_did(),
        "ftime": now,
        "stime": now,
        "sub_type": "0",
        "type": "1",
        "spmid": spmid,
        "from_spmid": "undefined",
        "jsonp": "jsonp",
        "csrf": "",
    }


def probe(label, url, ua, info, bvid, spmid, n, referer):
    hdr = {
        "User-Agent": ua,
        "Referer": referer,
        "Origin": referer.rstrip("/"),
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    got = []
    for _ in range(n):
        try:
            r = requests.post(url, headers=hdr, data=make_data(info, bvid, spmid), timeout=8)
            code, msg = None, ""
            try:
                j = r.json()
                code = j.get("code")
                msg = str(j.get("message", ""))[:40]
            except Exception:
                msg = (r.text or "")[:40].replace("\n", " ")
            got.append((r.status_code, code, msg))
        except Exception as e:
            got.append((None, None, str(e)[:40]))
    # 汇总：取最常见的一条
    http = got[0][0]
    code = got[0][1]
    msgs = {m for _, _, m in got if m}
    verdict = ""
    if code == -404 or http == 404:
        verdict = "路由不存在"
    elif code == 0:
        verdict = "路由存在且接受"
    elif code is not None:
        verdict = "路由存在(参数/风控)"
    print(f"{label} {url}")
    print(f"    HTTP={http} code={code} msg={list(msgs)[:2]}   => {verdict}")


def main():
    ap = argparse.ArgumentParser(description="候选端点存在性探测")
    ap.add_argument("bvid", help="BV 号或含 BV 号的链接")
    ap.add_argument("--n", type=int, default=2, help="每个端点请求次数，默认 2")
    args = ap.parse_args()

    bvid = extract_bvid(args.bvid)
    if not bvid.startswith("BV"):
        print(f"[X] 无法识别 BV 号: {args.bvid!r}")
        return

    info = booster.fetch_video_info(bvid)
    print(f"稿件: {info.get('title')}   aid={info['aid']} cid={info['cid']}")
    line("=")
    print("A. 端点探测（移动端 spmid=333.401 / TV UA）")
    line("=")
    for label, url, ua in CANDIDATES:
        referer = "https://m.bilibili.com/" if ua is _MOBILE_UA else "https://www.bilibili.com/"
        probe(label, url, ua, info, bvid, "333.401", args.n, referer)

    line("=")
    print("B. 同一端点 click/h5 换不同 spmid（看 spmid 是否是额度分池的键）")
    line("=")
    for spmid in ["333.401", "333.788", "333.1359", "333.1007", "333.999", "333.069"]:
        probe(f"h5 spmid={spmid:<8}", "http://api.bilibili.com/x/click-interface/click/h5",
              _MOBILE_UA, info, bvid, spmid, args.n, "https://m.bilibili.com/")


if __name__ == "__main__":
    main()
