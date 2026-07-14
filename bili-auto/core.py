"""
Core logic: fetch dynamics, check mutual follow, interact.
"""
import asyncio
import json
import logging
import random
import re
import sys
import threading
import time
from pathlib import Path

from bilibili_api import comment, dynamic, user, video
from bilibili_api.utils.network import Credential
from bilibili_api.utils.picture import Picture

logger = logging.getLogger("bili_auto")

# ---- paths ----
_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "bili-auto"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR = _DATA_DIR
CREDENTIAL_FILE = CONFIG_DIR / "credential.json"
MULTI_CREDENTIALS_FILE = CONFIG_DIR / "multi_credentials.json"
PROCESSED_FILE = CONFIG_DIR / "processed_dynamics.json"
INTERACTED_BVIDS_FILE = CONFIG_DIR / "interacted_bvids.json"
CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "auto_config.yaml"
_OLD_CONFIG_FILE = Path(__file__).resolve().parent / "config.yaml"

# ---- comment template ----
COMMENT_PREFIX = "第{idx}时间赶来支持up"
COMMENT_SUFFIX = "[米塔 第二弹_米拉思考][米塔 第二弹_米拉思考][米塔 第二弹_米拉思考]"

# ---- chinese numerals ----
_CN_NUMS = [
    "零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "二十一", "二十二", "二十三", "二十四", "二十五", "二十六", "二十七", "二十八", "二十九", "三十",
]


def to_cn_num(n: int) -> str:
    return _CN_NUMS[n] if 0 <= n < len(_CN_NUMS) else str(n)


import base64
import io
import time as _time

# 活跃的 QR 登录实例
_qr_login_instances: dict[str, object] = {}


# ---- QR 登录 (WebUI 调用) ----
async def qr_generate() -> dict:
    """生成 QR 登录二维码，返回 {session_id, qrcode_key, qr_image (base64 PNG)}。"""
    from bilibili_api.login_v2 import QrCodeLogin
    import qrcode as _qrcode
    import uuid as _uuid

    login = QrCodeLogin()
    await login.generate_qrcode()

    # 从 QR URL 直接生成 PNG 图片
    qr_url = getattr(login, "_QrCodeLogin__qr_link", "")
    if not qr_url:
        raise RuntimeError("无法获取二维码 URL")

    qr = _qrcode.QRCode(error_correction=_qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(qr_url)
    qr.make(fit=True)
    buf = io.BytesIO()
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()

    session_id = str(_uuid.uuid4())[:8]
    _qr_login_instances[session_id] = login
    return {"session_id": session_id, "qrcode_key": session_id, "qr_image": f"data:image/png;base64,{b64}"}


async def qr_poll(session_id: str) -> dict:
    """轮询 QR 登录状态。
    返回: {status: waiting|scanned|success|expired|error, ...}
    """
    from bilibili_api.login_v2 import QrCodeLoginEvents

    login = _qr_login_instances.get(session_id)
    if not login:
        return {"status": "error", "message": "登录会话不存在或已过期"}

    try:
        state = await login.check_state()
        if state == QrCodeLoginEvents.DONE:
            cred = login.get_credential()
            _save_credential(cred)
            _qr_login_instances.pop(session_id, None)
            return {"status": "success", "login_uid": getattr(cred, "dedeuserid", "") or ""}
        elif state == QrCodeLoginEvents.CONF:
            return {"status": "scanned", "message": "已扫码，请在手机上确认登录"}
        elif state == QrCodeLoginEvents.TIMEOUT:
            _qr_login_instances.pop(session_id, None)
            return {"status": "expired", "message": "二维码已过期，请重新生成"}
        else:
            # SCAN — 等待扫码
            return {"status": "waiting", "message": "等待扫码"}
    except Exception as e:
        _qr_login_instances.pop(session_id, None)
        return {"status": "error", "message": str(e)}


# ---- 凭证存储 ----
def _save_credential(cred) -> None:
    """保存凭证到 data/bili-auto/credential.json（项目内独立存储）。
    注意：QrCodeLogin 从 URL query 提取的 SESSDATA 已经是 URL 编码的（%2C, %2A），
    直接使用即可，不要再次编码。
    """
    CREDENTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sessdata": cred.sessdata,
        "bili_jct": cred.bili_jct,
        "ac_time_value": cred.ac_time_value or "",
        "buvid3": "",
        "buvid4": "",
        "dedeuserid": getattr(cred, "dedeuserid", "") or "",
        "saved_at": _time.time(),
    }
    CREDENTIAL_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("凭证已保存: %s", CREDENTIAL_FILE)


# ---- credential ----
def load_credential() -> Credential | None:
    """加载凭证，从 data/bili-auto/credential.json 读取。"""
    if not CREDENTIAL_FILE.exists():
        return None
    try:
        data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
        sessdata = data.get("sessdata", "")
        if not sessdata:
            return None
        # 只加载 sessdata/bili_jct/ac_time_value，不加载 buvid3/buvid4 以避免风控
        cred = Credential(
            sessdata=sessdata,
            bili_jct=data.get("bili_jct", ""),
            ac_time_value=data.get("ac_time_value", ""),
        )
        logger.info("加载凭证: %s", CREDENTIAL_FILE)
        return cred
    except Exception as e:
        logger.warning("读取凭证失败 %s: %s", CREDENTIAL_FILE, e)
        return None


# 兼容旧调用（CLI 模式下不再需要）
def qr_login(stop_event=None):
    """兼容接口，返回 None。请使用 WebUI 扫码登录。"""
    return None


# ---- default config ----
_DEFAULT_CONFIG = {
    "actions": {"triple": True, "like": False, "coin": False, "favorite": False,
                "comment": True, "share": True, "cover_comment": True},
    "history_actions": {"triple": True, "like": False, "coin": False, "favorite": False,
                        "comment": True, "share": True, "cover_comment": True, "play_once": False},
    "interact_own_dynamics": True,  # 是否互动自己的动态
    "coin_target_uid": [],  # 指定投币对象 UID 列表，为空则不指定
    "list_mode": "blacklist",   # "blacklist" | "whitelist"（互斥）
    "blacklist": [],
    "whitelist": [],
    "comment_text": {"prefix": COMMENT_PREFIX, "suffix": COMMENT_SUFFIX},
    "rival_reply": {"witty": True, "witty_ratio": 50, "zalan": True, "zalan_ratio": 50, "zalan_self_name": "喵喵"},
    "schedule": {"enabled": False, "interval_minutes": 30},
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            import yaml
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            # merge defaults
            for k, v in _DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
                elif isinstance(v, dict):
                    for kk, vv in v.items():
                        cfg[k].setdefault(kk, vv)
            return cfg
        except Exception as e:
            logger.warning("加载配置失败: %s, 使用默认配置", e)
    # 迁移：旧配置存在时自动复制到新位置
    if _OLD_CONFIG_FILE.exists():
        try:
            import shutil
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_OLD_CONFIG_FILE, CONFIG_FILE)
            logger.info("已迁移旧配置到: %s", CONFIG_FILE)
            return load_config()
        except Exception:
            pass
    return _DEFAULT_CONFIG.copy()


# ---- processed dynamics ----
def load_processed() -> set[str]:
    if PROCESSED_FILE.exists():
        try:
            return set(json.loads(PROCESSED_FILE.read_text()).get("ids", []))
        except Exception:
            pass
    return set()


def save_processed(ids: set[str]) -> None:
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


# ---- feed ----
async def get_feed_dynamics(cred: Credential) -> list[dict]:
    """Get latest video dynamics from following feed."""
    try:
        data = await dynamic.get_dynamic_page_info(credential=cred, _type=dynamic.DynamicType.VIDEO, pn=1)
        items = data.get("items", [])
        result = []
        for item in items:
            if item.get("type") != "DYNAMIC_TYPE_AV":
                continue
            modules = item.get("modules", {})
            author_info = modules.get("module_author", {})
            major = modules.get("module_dynamic", {}).get("major", {})
            archive = major.get("archive", {}) if major else {}
            result.append({
                "dynamic_id": item.get("id_str", ""),
                "author_name": author_info.get("name", ""),
                "author_uid": author_info.get("mid", 0),
                "title": archive.get("title", ""),
                "bvid": archive.get("bvid", ""),
                "aid": archive.get("aid", 0),
            })
        return result
    except Exception as e:
        logger.error("获取动态失败: %s (%s: %s)", e, type(e).__name__, e)
        return []


# ---- mutual follow ----
async def check_mutual_follow(uid: int, cred: Credential) -> bool:
    """attribute == 6 means mutual follow."""
    try:
        u = user.User(uid=uid, credential=cred)
        rel = await u.get_relation()
        return rel.get("relation", {}).get("attribute", 0) == 6
    except Exception as e:
        logger.warning("互关检测失败 UID=%d: %s", uid, e)
        return False


# ---- following list ----
async def get_following_list(cred: Credential, my_uid: int, max_pages: int = 10) -> list[dict]:
    """拉取当前用户的关注列表，返回 [{uid, name, face}, ...]。
    最多拉取 max_pages 页（每页 50 条），即最多 500 个关注。
    """
    result = []
    try:
        from bilibili_api import user as _user
        me = _user.User(uid=my_uid, credential=cred)
        for pn in range(1, max_pages + 1):
            try:
                data = await me.get_followings(pn=pn, ps=50)
                items = data.get("list", [])
                if not items:
                    break
                for item in items:
                    result.append({
                        "uid": item.get("mid", 0),
                        "name": item.get("uname", ""),
                        "face": item.get("face", ""),
                    })
                total = data.get("total", 0)
                if len(result) >= total:
                    break
            except Exception as e:
                logger.warning("拉取关注列表第 %d 页失败: %s", pn, e)
                break
    except Exception as e:
        logger.warning("拉取关注列表失败: %s", e)
    return result


# ---- chinese numeral parser ----
_CN_DIGIT_MAP = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000, "万": 10000,
}


def _parse_cn_num(s: str) -> int:
    """Parse Chinese numeral string to int. e.g. '二十三' -> 23, '一百零五' -> 105."""
    s = s.strip()
    # Try direct Arabic
    if s.isdigit():
        return int(s)
    # Try simple mapping (up to 30)
    if s in _CN_NUMS:
        return _CN_NUMS.index(s)
    # Fallback: multiplicative parsing
    total = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGIT_MAP:
            val = _CN_DIGIT_MAP[ch]
            if val >= 10:
                if current == 0:
                    current = 1
                total += current * val
                current = 0
            else:
                current = val
        else:
            return 0
    total += current
    return total if total > 0 else 0


# ---- comment index ----
async def get_next_comment_index(
    aid: int, cred: Credential, my_uid: int
) -> tuple[int, dict | None]:
    """Scan for '第X时间赶来支持up', return (next_index, rival_comment_or_none)."""
    pattern = re.compile(r"第([一二三四五六七八九十百千万\d]+)时间")
    max_idx = 0
    rivals = []
    try:
        for page in range(1, 5):
            data = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                order=comment.OrderType.TIME,
                credential=cred,
            )
            replies = data.get("replies", [])
            if not replies:
                break
            for r in replies:
                msg = r.get("content", {}).get("message", "")
                m = pattern.search(msg)
                if m:
                    idx = _parse_cn_num(m.group(1))
                    if idx > max_idx:
                        max_idx = idx
                    # Collect all non-self rival comments
                    if str(r.get("member", {}).get("mid", "")) != str(my_uid):
                        rivals.append({
                            "rpid": r.get("rpid"),
                            "author": r.get("member", {}).get("uname", "某人"),
                            "text": msg,
                        })
        rival = random.choice(rivals) if rivals else None
        return max_idx + 1, rival
    except Exception as e:
        logger.warning("读取评论区失败 aid=%d: %s", aid, e)
        return 1, None


# ---- witty rival reply generator ----
_WITTY_OPENS = [
    "",
    "",
    "",
    "笑麻了 ",
    "哈哈哈哈 ",
    "好家伙 ",
    "笑死 ",
    "沃趣 ",
    "绷不住了 ",
]

_WITTY_BODIES = [
    "你这手速，外卖都比你慢",
    "第几时间不重要，重要的是我来了，而且我带了酱板鸭",
    "抢到前排了是吧，奖励你帮我抢票",
    "你赢了速度，我赢了摸鱼的时间",
    "你这么快，up主都没反应过来，弹幕都懵了",
    "搁这赛跑呢？那我弃赛，我躺平",
    "有没有可能我不是来晚了，是故意压轴",
    "前排气氛组到位，掌声在哪里",
    "你这手速我认可了，但你评论区翻牌子了吗",
    "每天被你抢先，我已经佛系了",
    "又来？你是住在up主的评论区了吗",
    "沙发是你的，板凳是我的，地板是留给后来人的",
    "我愿称你为B站卷王，建议加鸡腿",
    "你跑这么快，up主都跟不上你的节奏了",
    "拼手速的时代，我选择拼才华（不是",
    "我掐表了，你又双叒叕快了",
    "第几无所谓，反正每次都是你先到，我先到家",
    "冲这么快，视频都还没加载完吧",
    "你这速度不去练短跑可惜了",
    "我跟你说，我今天就是来看你表演的",
    "第几时间不重要，重要的是我每次都来了，比上班还准时",
    "你快你赢，我懒我快乐",
    "快是快，但up主的更新速度才是王者",
    "你手速这么快，建议去打节奏大师",
    "有没有一种可能，我是在等你发完我才发的",
    "卷不过你，但我也要来凑个热闹",
    "你每次都是第一，我每次都是来晚了的第三名",
    "前排留名，别挤我，我就坐这儿了",
    "你这速度，建议直接住在b站",
    "快人一步不算啥，能坚持到最后一秒才是真本事",
    "我愿称你为B站闪电侠，建议出周边",
]

_WITTY_CLOSES = [
    "",
    "",
    "",
    "（",
    "[微笑]",
    "[doge]",
    "（bushi",
    "（笑死",
    "（已阅",
    "（已阅，盖章",
    "（下次注意",
    "（建议加精",
]

# ---- zalan-style reply generator ----
_ZALAN_OPENS = [
    "",
    "",
    "",
    "",
    "姐姐！你今天也好漂亮啊～",
    "温柔大方善良可爱美丽绝赞冰雪聪明真令人心动迷人的漂酿姐姐晚上好",
    "栅栏栅吕姐姐姐姐～",
    "姐姐是天，姐姐是地，姐姐是天下第一～",
    "我是见一个姐姐就夸夸",
    "跟xx姐姐大人一样温柔可爱迷人",
    "温柔大方可爱绝赞的漂酿小小姐姐晚上好",
    "偷心怪盗.芳心纵火犯.芳心盗窃贼",
    "姐姐看起来好像很甜的样子",
    "春眠不觉晓，姐姐早/中/晚上好～",
    "现在看xx眼里都是小心心",
    "xx姐姐你到你干了什么！",
    "xx（紫啧）姐姐姐姐姐姐～",
    "漂酿/姐姐/大人晚上好～漂酿/姐姐/大人辛苦啦～",
    "呀漂酿姐姐大人晚上好",
    "姐姐大人你那么可爱",
    "姐姐请不要忘记我...",
    "姐姐怎么又善良又冰雪聪明真令人心动呢～",
    "本宫（划掉，哀家（划掉，咳咳喵喵我",
    "天呐xx姐姐你开播了～",
    "喵喵喜欢姐姐的眼神是藏不住的～",
    "姐姐大人喵喵想见你～",
    "xx姐姐你是不是偷偷学了什么魔法",
    "姐姐你今天有没有好好吃饭呀",
    "姐姐姐姐姐姐姐姐姐姐",
    "随便",
    "咋了",
    "活该",
    "想买",
    "烦s了",
    "怪我咯",
    "我不管",
    "睡不着",
    "我在忙",
    "你管我",
    "你不服",
    "帮我个忙",
    "你敢凶我",
    "你不早说",
    "不用你操心",
    "这都做不好",
    "为啥不找我",
    "那你要来帮我吗",
]

_ZALAN_BODIES = [
    "谁不喜欢这 唱歌可爱 说话好听 萌萌的 还会xx的可爱姐姐呢",
    "可爱的xx姐姐，就竟是谁研究出来的呢",
    "xx姐姐...能不能教教我怎么煮汤圆啊 我太笨了 做什么都会露馅 连喜欢你也是...",
    "所谓三思：今天思考想xx姐姐了没～今天思考爱xx姐姐了没～今天思考能不能让自己更加朝思暮日思夜想xx姐姐了没～",
    "[喝彩]❤️一生唯爱❤️全力应援❤️传奇偶像❤️超级xx❤️永远支持❤️大好き❤️[喝彩]",
    "於你而言我是一顆星嗎",
    "现在的喵喵每天做的事情就是在想姐姐们罢了，小小的脑海里除了姐姐们已经没有别的空余了❤️",
    "（安心的蜷缩在姐姐的腿上）",
    "我是真的想你了姐姐，别的姐姐不是这样的～",
    "请对喵喵我呼噜呼噜毛摸摸头",
    "（蹭蹭～谢谢姐姐～",
    "没办法嘛喵喵那么乖姐姐们那么温柔可爱",
    "又不是喵喵的错~",
    "没关系呀喵喵只是姐姐们池塘一条鱼罢了",
    "只要姐姐的愛雨露均沾我沾到一点就行",
    "真的是可可爱爱温温柔柔举高高抱抱姐姐",
    "姐姐们互相夺愛 跟喵喵有什么关系呢～",
    "让我咬俩口",
    "单推一堆也是单推",
    "只是想给每个妹妹（姐姐）一个家",
    "顺便给每个妹妹（姐姐）多点爱",
    "宝，我偷了个宇宙送给你.",
    "宝宝我看别的v不是因为我不想单推你了是因为我对你的爱太多了已经多到溢出来了然后那个Vtb刚好接住了◇",
    "自己的心碎成了那么多片，必须每一片都住满一个姐姐大人才行。不然，容易得心脏病，心里发空～",
    "姐姐大人我真的只是喜欢你，你唱歌这么好听，我怎么可能会去看别人，单推喵(",
    "没有的宝宝（姐姐，你是我见一个爱一个里面最新的一个宝宝（姐姐",
    "多个朋友多条路，多个老婆多个家，只要老婆（姐姐换的快，没有悲伤只有爱",
    "夜来风雨声，思念姐姐知多少～",
    "宝宝（姐姐，你是我心尖尖上的那个美丽的宝宝（姐姐",
    "没事的宝宝（姐姐，只要你心里有我就足够了～谁让人家没能成为你的心尖宠呢～没事的宝宝（姐姐，你在喊别人宝宝的时候～只要心里想过我就行～没事的宝宝（姐姐你走吧呜呜呜，再也不要回来呜呜呜～",
    "xx你到底给我下了什么药啊",
    "为什么我一闭眼脑海中都是你～",
    "哥们 你放心 我这次真不会被VTB骗了 我装作被她迷的神魂颠倒 只是我计划的一部分 你别说了 我有自己的节奏",
    "不主动，不拒绝，不负责",
    "喵喵的爱是有限的，每天分给各位姐姐大人的有多有少～",
    "（喵喵撒娇",
    "有时候挺羡慕别人的，因为渣到随心所欲，不负责任，什么话都可以随便说出口，不像喵喵我，纯情，专一，谨言慎行，不然要被截图放精华，呜呜呜",
    "不像喵喵，只会姐姐/姐姐大人/漂酿姐姐大人/温柔大方可爱绝赞的漂酿xx姐姐早中晚上好/（紫啧）xx姐姐姐姐姐姐早中晚上好～",
    "5000关注是上限他为了你把别人取关了",
    "5000关注是上限我为了你把别人取关了",
    "小鱼要变成小鱼干了 因为水分变成眼泪流干了 呜呜呜",
    "是你应得的夸奖～",
    "智者不入爱河，喵喵不想负责。",
    "那也是博爱在上！栅栏在下！",
    "我日思夜想xx姐姐你终于开播啦～",
    "喵喵把心中的爱全部分给x姐姐了~",
    "xx姐姐我真的好想你呀~",
    "没有xx姐姐喵喵该怎么办呀~",
    "喵喵脑海里只有xx姐姐~已经被完完全全的占据了~",
    "没有xx姐姐的每一天都忍受不了了。",
    "朝思暮想～",
    "日思夜想～",
    "喵喵已经非常对xx姐姐心动了~",
    "xx姐姐~开播的时候喵喵非常开心见到xx姐姐",
    "xx姐姐你不开播的时候我脑海里都是xx",
    "xx姐姐我会一直一直永远永远延续这份爱意的～",
    "小x参上！/～",
    "xx姐姐～我会一直记住你的样子，记住你的声音，无论是转生还是去面对现实，我都会记住你...",
    "姐姐你笑起来的样子真的好好看啊～喵喵都看呆了",
    "每次听到姐姐的声音，喵喵的心都要化了～",
    "姐姐姐姐，你有没有发现喵喵今天特别乖呀～",
    "喵喵的脑子里装满了姐姐，已经没有地方装别的了～",
    "xx姐姐～你是不是有魔法啊，怎么每次都能把喵喵迷住",
    "姐姐你今天的状态好好呀，是不是偷偷做了什么开心的事～",
    "喵喵对姐姐的喜欢已经多到装不下了，都要溢出来了～",
    "姐姐姐姐～你快看看喵喵呀，喵喵一直在等你呢～",
    "没有姐姐的日子，喵喵就像鱼离开了水，活不下去～",
    "姐姐你笑一个嘛～喵喵想看姐姐笑的样子，一定很好看～",
    "（我听姐姐的就好啦）",
    "（姐姐这是怎么啦）",
    "（怎么办，人家心疼S姐姐了)",
    "（姐姐要是能给人家买个xx就好了)",
    "（一想到见不到姐姐，人家心里就难受得很)",
    "（姐姐不会怪我吧）",
    "（要是姐姐不愿意的话那就算了）",
    "（姐姐真坏，害得人家睡不着）",
    "（虽然我很忙，但是不耽误想姐姐）",
    "（姐姐原来这么关心我呀）",
    "（不可以嘛）",
    "（姐姐帮我个忙好嘛、拜托拜托~）",
    "（姐姐怎么凶人家，人家好害怕）",
    "（姐姐要是早点告诉我就好啦）",
    "（没关系的姐姐，人家一个人也可以)",
    "（我相信姐姐一定没问题的）",
    "（姐姐最近一定有什么心事吧）",
    "（姐姐是想帮我嘛，呜呜好感动）",
    "姐姐你不用说了...",
    "我都懂，我都明白，我是选项E，我planB，是分叉的头发，洗衣机流出的泡沫，超市里被捏碎的饼干，是吃腻的奶油，是落寞的城市，地上的草。我是被踩踏的，是西装的备用扣，是被雨淋湿的小狗，是腐烂的橘子，是过期的牛奶，是断线的风筝，是被随意丢弃的向日葵，是沉默寡言的小朋友。对姐姐你来说的我...随便就打发了..",
    "（姐姐你知道吗，每次你开播我都会笑，不是因为开心，是因为想引起你注意）",
    "（姐姐今天有没有想我呀，反正我想你了）",
    "（人家只是姐姐众多粉丝中的一个，但姐姐是人家唯一的光啊）",
    "（姐姐你别看别人了，看看我嘛～人家也在努力营业呢）",
    "（姐姐今天好好看，每天都好看，每天都想夸一遍）",
    "（姐姐你知道吗，我今天做了个梦，梦见姐姐了，醒来发现现实更好看）",
    "（姐姐姐姐，我今天表现好不好呀，能不能奖励一个晚安～）",
    "（姐姐你知道吗，每次你笑我心都化了，然后又重新凝固，然后又化了，无限循环）",
    "（姐姐你今天有没有好好吃饭呀，不吃饱怎么有力气开播嘛～）",
    "（人家只是个路过的小猫咪，但是被姐姐的美貌绊住了脚）",
]

_ZALAN_CLOSES = [
    "",
    "",
    "",
    "",
    "～",
    "❤️",
    "呜呜呜",
    "（",
    "（笑",
    "（狗头",
    "（bushi",
    "（逃",
    "（已阅",
    "（已阅，盖章",
    "（下次注意",
    "（建议加精",
    "（喵喵式撒娇",
]


def _make_rival_reply(rival_name: str = "", config: dict | None = None) -> str:
    """Each type has independent trigger ratio. If both trigger, randomly pick one."""
    cfg = config or load_config()
    rr = cfg.get("rival_reply", {})
    candidates = []
    if rr.get("witty", True) and random.randint(1, 100) <= rr.get("witty_ratio", 50):
        candidates.append(_make_witty_reply(cfg))
    if rr.get("zalan", True) and random.randint(1, 100) <= rr.get("zalan_ratio", 50):
        candidates.append(_make_zalan_reply(rival_name, rr, cfg))
    if not candidates:
        return ""
    return random.choice(candidates)


def _get_reply_texts(cfg: dict, key: str) -> list[str]:
    """从配置中获取语录列表，如果配置中有自定义数据则使用，否则使用内置默认。"""
    custom = cfg.get("reply_texts", {}).get(key)
    if custom and isinstance(custom, list) and len(custom) > 0:
        return custom
    builtin = {"witty_opens": _WITTY_OPENS, "witty_bodies": _WITTY_BODIES, "witty_closes": _WITTY_CLOSES,
               "zalan_opens": _ZALAN_OPENS, "zalan_bodies": _ZALAN_BODIES, "zalan_closes": _ZALAN_CLOSES}
    return builtin.get(key, [])


def _make_zalan_reply(rival_name: str = "", rr_config: dict | None = None, cfg: dict | None = None) -> str:
    """Generate zalan-style reply by combining random parts."""
    cfg = cfg or {}
    parts = []
    opens = _get_reply_texts(cfg, "zalan_opens")
    bodies = _get_reply_texts(cfg, "zalan_bodies")
    closes = _get_reply_texts(cfg, "zalan_closes")
    open_ = random.choice(opens) if opens else ""
    if open_:
        parts.append(open_)
    parts.append(random.choice(bodies) if bodies else "")
    close_ = random.choice(closes) if closes else ""
    if close_:
        parts.append(close_)
    result = "".join(parts)
    # 替换"喵喵"为配置的自称词
    self_name = (rr_config or {}).get("zalan_self_name", "喵喵") or "喵喵"
    if self_name != "喵喵":
        result = result.replace("喵喵", self_name)
    if rival_name:
        result = result.replace("xx", rival_name)
        result = re.sub(r"(?<=[小跟])x(?=姐姐|参上)", rival_name, result)
    return result


def _make_witty_reply(cfg: dict | None = None) -> str:
    """Generate a unique witty reply by combining random parts."""
    cfg = cfg or {}
    parts = []
    opens = _get_reply_texts(cfg, "witty_opens")
    bodies = _get_reply_texts(cfg, "witty_bodies")
    closes = _get_reply_texts(cfg, "witty_closes")
    open_ = random.choice(opens) if opens else ""
    if open_:
        parts.append(open_)
    parts.append(random.choice(bodies) if bodies else "")
    close_ = random.choice(closes) if closes else ""
    if close_:
        parts.append(close_)
    return "".join(parts)


async def share_video(bvid: str, cred: Credential) -> tuple[bool, bool]:
    """Share video (web API). Returns (success, is_risk_control)."""
    try:
        from bilibili_api import video
        v = video.Video(bvid=bvid, credential=cred)
        await v.share()
        return True, False
    except Exception as e:
        is_403 = "-403" in str(e)
        logger.warning("⚠️ 分享失败: %s — %s", bvid, e)
        return False, is_403


# ---- fixed send_comment (serialize statistics as JSON string) ----
async def send_comment_fixed(
    text: str,
    oid: int,
    type_: comment.CommentResourceType,
    root: int = None,
    parent: int = None,
    credential: Credential = None,
    pic = None,
    pictures_data: str = None,
) -> dict:
    """
    Fixed wrapper for comment.send_comment.
    bilibili_api v17.4.x nests statistics as dict which httpx form-encodes
    incorrectly, causing server to see empty message (error 12066).
    This version serializes statistics to JSON string.

    If pictures_data is provided (pre-uploaded JSON), pic upload is skipped.
    """
    from bilibili_api.comment import API, upload_image
    from bilibili_api.utils.network import Api

    if credential is None:
        credential = Credential()

    credential.raise_for_no_sessdata()
    credential.raise_for_no_bili_jct()

    data = {
        "oid": oid,
        "type": type_.value,
        "message": text,
        "plat": 1,
        "statistics": json.dumps({"appId": 100, "platform": 5}),
        "gaia_source": "main_web",
    }

    if pictures_data:
        # Pre-uploaded pictures — skip curl_cffi upload
        data["pictures"] = pictures_data
    elif pic:
        if isinstance(pic, Picture):
            pic = [pic]
        data["pictures"] = []
        for p in pic:
            res = await upload_image(image=p, credential=credential)
            data["pictures"].append({
                "img_src": res["image_url"],
                "img_width": res["image_width"],
                "img_height": res["image_height"],
                "img_size": res["img_size"],
            })
        data["pictures"] = json.dumps(data["pictures"])

    if root is not None:
        data["root"] = root
        data["parent"] = parent if parent is not None else root

    api_def = API["comment"]["send"]
    return await Api(**api_def, credential=credential).update_data(**data).result


# ---- cover comment ----
COVER_RETRY_MAX = 3
COVER_RETRY_BASE = 1.5  # seconds, exponential backoff: 1.5, 3.0, 6.0
COVER_UPLOAD_URL = "https://api.bilibili.com/x/dynamic/feed/draw/upload_bfs"

# MIME map for common image formats
_COVER_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp",
}


async def _download_cover(pic_url: str) -> bytes:
    """Download cover image via aiohttp."""
    import aiohttp

    headers = {
        "Referer": "https://www.bilibili.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(pic_url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def _upload_cover_direct(content: bytes, fmt: str, cred: Credential) -> dict:
    """Upload image to Bilibili via aiohttp, bypassing curl_cffi.

    Returns a dict with keys: image_url, image_width, image_height, img_size.
    """
    import aiohttp
    import hashlib
    import time as _time
    import urllib.parse

    from bilibili_api.utils.network import _enc_wbi, get_wbi_mixin_key

    # Build WBI-signed params
    params = {"biz": "draw", "category": "daily"}
    mixin_key = await get_wbi_mixin_key()
    params = _enc_wbi(params, mixin_key)
    params["csrf"] = cred.bili_jct

    # Prepare multipart form data
    data = aiohttp.FormData()
    for k, v in params.items():
        data.add_field(k, str(v))
    mime = _COVER_MIME.get(fmt.lower(), "image/jpeg")
    data.add_field("file_up", content, filename=f"cover.{fmt}", content_type=mime)

    cookies = cred.get_cookies()
    headers = {
        "Referer": "https://www.bilibili.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    async with aiohttp.ClientSession(cookies=cookies, headers=headers) as session:
        async with session.post(COVER_UPLOAD_URL, data=data) as resp:
            resp.raise_for_status()
            result = await resp.json()

    if result.get("code") != 0:
        raise RuntimeError(f"上传失败: code={result.get('code')} msg={result.get('message')}")

    return result["data"]


async def post_cover_comment(bvid: str, aid: int, cred: Credential, v: video.Video) -> None:
    """Download video cover, upload to Bilibili and post as picture comment.
    Upload is via aiohttp (not curl_cffi) for reliability.
    Retries up to 3 times with exponential backoff on transient failures.
    """
    info = await v.get_info()
    pic_url = info.get("pic", "")
    if not pic_url:
        logger.warning("⚠️ 未获取到封面 URL: %s", bvid)
        return

    fmt = pic_url.rsplit(".", 1)[-1].split("?")[0] or "jpg"

    last_err = None
    for attempt in range(1, COVER_RETRY_MAX + 1):
        try:
            # 1) Download cover
            content = await _download_cover(pic_url)
            # 2) Upload via aiohttp (bypass unstable curl_cffi)
            upload_result = await _upload_cover_direct(content, fmt, cred)
            pictures_data = json.dumps([{
                "img_src": upload_result["image_url"],
                "img_width": upload_result["image_width"],
                "img_height": upload_result["image_height"],
                "img_size": upload_result["img_size"],
            }])
            # 3) Post comment with pre-uploaded picture data
            await send_comment_fixed(
                text="封面来啦",
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                pictures_data=pictures_data,
                credential=cred,
            )
            logger.info("🖼️ 封面评论已发送: %s", bvid)
            return
        except Exception as e:
            last_err = e
            if attempt < COVER_RETRY_MAX:
                delay = COVER_RETRY_BASE * (2 ** (attempt - 1))
                logger.debug("🔄 封面重试 %d/%d，%.1fs 后重试: %s", attempt, COVER_RETRY_MAX, delay, bvid)
                await asyncio.sleep(delay)
    logger.warning("⚠️ 封面评论发送失败: %s — %s", bvid, last_err)


# ---- default favorite folder cache ----
_default_fav_folder_id: int | None = None
_coin_captcha_warned: bool = False


async def _get_default_fav_folder(cred: Credential, my_uid: int) -> int | None:
    """Get user's default favorite folder ID, cached after first call."""
    global _default_fav_folder_id
    if _default_fav_folder_id is not None:
        return _default_fav_folder_id
    try:
        from bilibili_api import favorite_list
        result = await favorite_list.get_video_favorite_list(uid=my_uid, credential=cred)
        folders = result.get("list", [])
        if folders:
            _default_fav_folder_id = folders[0]["id"]
            return _default_fav_folder_id
    except Exception as e:
        logger.warning("获取默认收藏夹失败: %s", e)
    return None


# ---- interact ----
async def _interruptible_sleep(seconds: float, stop_event: threading.Event | None = None) -> bool:
    """Sleep that can be interrupted by stop_event. Returns True if interrupted."""
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    end_time = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < end_time:
        if stop_event.is_set():
            return True
        await asyncio.sleep(min(1.0, end_time - asyncio.get_event_loop().time()))
    return stop_event.is_set()


async def interact_with_video(
    bvid: str,
    aid: int,
    author_name: str,
    cred: Credential,
    my_uid: int,
    send_cover: bool = True,
    config: dict | None = None,
    stop_event: threading.Event | None = None,
) -> dict:
    """Execute configured interactions on one video.
    Returns a dict of action results, e.g. {"triple": "success", "share": "skipped", ...}
    """
    cfg = config or load_config()
    actions = cfg.get("actions", {})
    v = video.Video(bvid=bvid, credential=cred)
    results = {}

    # triple (mutually exclusive with like/coin/favorite)
    use_triple = actions.get("triple", False)
    if use_triple:
        if stop_event and stop_event.is_set():
            logger.info("⏹️ 收到停止信号，跳过剩余操作")
            return results
        try:
            await v.triple()
            logger.info("✅ 三连: %s (%s)", bvid, author_name)
            results["triple"] = "success"
        except Exception as e:
            err_str = str(e)
            if "65006" in err_str:
                logger.info("✅ 三连: %s (已三连)", bvid)
                results["triple"] = "already_done"
            elif "-101" in err_str:
                logger.error("❌ 凭证失效，请重新登录: %s", bvid)
                results["triple"] = "auth_failed"
            else:
                logger.warning("⚠️ 三连失败: %s — %s", bvid, e)
                results["triple"] = "failed"
        if await _interruptible_sleep(random.uniform(3, 5), stop_event):
            return results

    # like (individual, skipped if triple is enabled)
    if not use_triple and actions.get("like", False):
        if stop_event and stop_event.is_set():
            return results
        try:
            await v.like(status=True)
            logger.info("✅ 点赞: %s", bvid)
            results["like"] = "success"
        except Exception as e:
            err_str = str(e)
            if "65006" in err_str:
                logger.info("✅ 点赞: %s (已点赞)", bvid)
                results["like"] = "already_done"
            else:
                logger.warning("⚠️ 点赞失败: %s — %s", bvid, e)
                results["like"] = "failed"
        if await _interruptible_sleep(random.uniform(2, 4), stop_event):
            return results

    # coin (individual, skipped if triple is enabled)
    if not use_triple and actions.get("coin", False):
        if stop_event and stop_event.is_set():
            return results
        try:
            await v.pay_coin(num=2, like=False)
            logger.info("✅ 投币: %s", bvid)
            results["coin"] = "success"
        except Exception as e:
            err_str = str(e)
            if "34005" in err_str:
                logger.info("✅ 投币: %s (已投币)", bvid)
                results["coin"] = "already_done"
            else:
                logger.warning("⚠️ 投币失败: %s — %s", bvid, e)
                results["coin"] = "failed"
        if await _interruptible_sleep(random.uniform(2, 4), stop_event):
            return results

    # favorite (individual, skipped if triple is enabled)
    if not use_triple and actions.get("favorite", False):
        if stop_event and stop_event.is_set():
            return results
        try:
            fav_id = await _get_default_fav_folder(cred, my_uid)
            if fav_id:
                await v.set_favorite(add_media_ids=[fav_id])
                logger.info("✅ 收藏: %s", bvid)
                results["favorite"] = "success"
            else:
                logger.warning("⚠️ 收藏失败: 未获取到默认收藏夹")
                results["favorite"] = "failed"
        except Exception as e:
            logger.warning("⚠️ 收藏失败: %s — %s", bvid, e)
            results["favorite"] = "failed"
        if await _interruptible_sleep(random.uniform(2, 4), stop_event):
            return results

    # share
    if actions.get("share", True):
        if stop_event and stop_event.is_set():
            return results
        ok, is_403 = await share_video(bvid, cred)
        if ok:
            logger.info("✅ 分享: %s", bvid)
            results["share"] = "success"
        elif is_403:
            logger.info("⏭️ 分享被风控，跳过: %s", bvid)
            results["share"] = "risk_control"
        else:
            logger.warning("⚠️ 分享失败: %s", bvid)
            results["share"] = "failed"
        if await _interruptible_sleep(random.uniform(3, 5), stop_event):
            return results

    # comment
    if actions.get("comment", True):
        if stop_event and stop_event.is_set():
            return results
        next_idx, rival = await get_next_comment_index(aid, cred, my_uid)
        cn_idx = to_cn_num(next_idx)
        ct = cfg.get("comment_text", {})
        c_prefix = ct.get("prefix", COMMENT_PREFIX)
        c_suffix = ct.get("suffix", COMMENT_SUFFIX)
        comment_text = f"{c_prefix.format(idx=cn_idx)}{c_suffix}"
        try:
            await send_comment_fixed(
                text=comment_text,
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                credential=cred,
            )
            logger.info("✅ 评论: %s", comment_text)
            results["comment"] = "success"

            # Witty / Zalan rival reply
            if rival and rival.get("rpid"):
                witty = _make_rival_reply(rival.get("author", ""), config=cfg)
                if witty:
                    if "擅自" in rival.get("text", ""):
                        witty += "又在擅自，点歌 连名带姓"
                    if await _interruptible_sleep(random.uniform(2, 4), stop_event):
                        return results
                    await send_comment_fixed(
                        text=witty,
                        oid=aid,
                        type_=comment.CommentResourceType.VIDEO,
                        root=rival["rpid"],
                        credential=cred,
                    )
                    logger.info("🎯 嘲讽回复: %s → @%s", witty, rival.get("author", "某人"))
                    results["rival_reply"] = "success"
                else:
                    rr = cfg.get("rival_reply", {})
                    logger.info("  ⏭️ 嘲讽回复未触发 (嘲讽=%d%% 栅栏=%d%%)", rr.get("witty_ratio", 50), rr.get("zalan_ratio", 50))
                    results["rival_reply"] = "not_triggered"
        except Exception as e:
            logger.warning("⚠️ 评论失败: %s", e)
            results["comment"] = "failed"

    # Cover picture comment
    if actions.get("cover_comment", True) and send_cover:
        if stop_event and stop_event.is_set():
            return results
        await _interruptible_sleep(random.uniform(2, 4), stop_event)
        if stop_event and stop_event.is_set():
            return results
        try:
            await post_cover_comment(bvid, aid, cred, v)
            results["cover_comment"] = "success"
        except Exception as e:
            logger.warning("⚠️ 封面评论失败: %s", e)
            results["cover_comment"] = "failed"

    return results


# ---- main entry ----
async def run_once(
    stop_event: threading.Event | None = None,
    on_interact=None,
    extra_handler=None,
) -> None:
    """Single execution entry point. Set stop_event to abort gracefully.
    on_interact: optional callback(bvid) called when a video is interacted with.
    extra_handler: optional logging.Handler (or list of handlers) for direct log routing
                   (e.g. WebUI TaskLogHandler). When provided, replaces stdout handler.
    """
    _stop = stop_event or threading.Event()
    log_file = CONFIG_DIR / "bili_auto.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    _formatter = logging.Formatter("[%(asctime)s] [AUTO] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # 清除旧 handlers，重新配置
    logger.handlers.clear()

    # 如果有外部 handler（WebUI 场景），用它替代 stdout handler，避免线程安全问题
    if extra_handler is not None:
        handlers = extra_handler if isinstance(extra_handler, list) else [extra_handler]
        for h in handlers:
            if not h.formatter:
                h.setFormatter(_formatter)
            logger.addHandler(h)
    else:
        # CLI 场景：输出到 stdout
        _stdout_handler = logging.StreamHandler(sys.stdout)
        _stdout_handler.setFormatter(_formatter)
        logger.addHandler(_stdout_handler)

    # 始终写文件日志
    _file_handler = logging.FileHandler(log_file, encoding="utf-8", mode="a")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
    logger.setLevel(logging.INFO)

    logger.info("=" * 40)
    logger.info("启动 B站自动互动")

    cfg = load_config()
    list_mode = cfg.get("list_mode", "blacklist")  # "blacklist" | "whitelist"
    blacklist = set(str(uid) for uid in cfg.get("blacklist", []))
    whitelist = set(str(uid) for uid in cfg.get("whitelist", []))
    interact_own = cfg.get("interact_own_dynamics", True)
    coin_target = cfg.get("coin_target_uid", [])
    if isinstance(coin_target, str):
        coin_target = [coin_target] if coin_target else []
    coin_target_set = set(str(uid) for uid in coin_target)
    actions = cfg.get("actions", {})
    enabled = [k for k, v in actions.items() if v]
    logger.info("启用动作: %s", ", ".join(enabled) if enabled else "无")
    logger.info("互动自己的动态: %s", "是" if interact_own else "否")
    if coin_target_set:
        logger.info("指定投币对象: %d 个 UID（%s）", len(coin_target_set), ", ".join(coin_target_set))
    if list_mode == "whitelist":
        logger.info("📋 白名单模式: %d 个 UID（仅对这些用户执行互动）", len(whitelist))
    else:
        if blacklist:
            logger.info("🚫 黑名单: %s 个 UID", len(blacklist))

    cred = load_credential()
    if not cred:
        logger.info("未找到凭证，请在页面上扫码登录后重试")
        return

    # buvid3/buvid4 由 bilibili_api 的 auto_buvid 自动处理，不手动获取

    logger.info("正在获取用户信息...")
    try:
        me = await asyncio.wait_for(user.get_self_info(cred), timeout=30)
        if not me or not isinstance(me, dict):
            logger.error("获取用户信息失败: 返回数据异常 (%s)，请重新登录", type(me).__name__)
            return
        my_uid = me.get("mid")
        logger.info("登录用户: %s (UID=%s)", me.get("name"), my_uid)
    except asyncio.TimeoutError:
        logger.error("获取用户信息超时(30s)，请检查网络或重新登录")
        return
    except Exception as e:
        logger.error("获取用户信息失败: [%s] %s", type(e).__name__, e)
        logger.error("凭证可能已失效，请在页面上重新扫码登录")
        return

    try:
        dynamics = await get_feed_dynamics(cred)
    except Exception as e:
        logger.error("获取动态失败: %s", e)
        return
    logger.info("获取到 %d 条视频动态", len(dynamics))
    if not dynamics:
        logger.info("无视频动态")
        return

    processed = load_processed()

    today = time.strftime("%Y-%m-%d")
    interacted_data: dict = {}
    if INTERACTED_BVIDS_FILE.exists():
        interacted_data = json.loads(INTERACTED_BVIDS_FILE.read_text(encoding="utf-8"))
    if interacted_data.get("_date") != today:
        interacted_data = {"_date": today, "bvids": []}
    seen_bvids: set[str] = set(interacted_data.get("bvids", []))

    processed_mf: list[dict] = []  # dynamics actually interacted by main (for secondary phase)

    # ── 预筛选新动态，提前告知用户本次工作量 ──
    _ACTION_ICONS = {
        "triple": "三连", "like": "点赞", "coin": "投币", "favorite": "收藏",
        "share": "分享", "comment": "评论", "rival_reply": "嘲讽", "cover_comment": "封面",
    }
    new_dynamics = [d for d in dynamics if d["dynamic_id"] not in processed]
    logger.info("📬 本次扫描: %d 条动态，其中 %d 条新动态待处理", len(dynamics), len(new_dynamics))
    if not new_dynamics:
        logger.info("✅ 无新动态需要处理")
        return
    logger.info("")

    # 统计数据
    stats = {
        "total_dynamics": len(dynamics),
        "new_dynamics": len(new_dynamics),
        "skipped_processed": 0,
        "skipped_self": 0,
        "skipped_not_mutual": 0,
        "skipped_blacklist": 0,
        "skipped_seen": 0,
        "interacted": 0,
        "action_success": {"triple": 0, "like": 0, "coin": 0, "favorite": 0, "share": 0, "comment": 0, "rival_reply": 0, "cover_comment": 0},
        "action_failed": {"triple": 0, "like": 0, "coin": 0, "favorite": 0, "share": 0, "comment": 0, "rival_reply": 0, "cover_comment": 0},
        "stopped": False,
    }

    progress = 0  # 当前已处理的新动态计数
    total_new = len(new_dynamics)

    for d in dynamics:
        if _stop.is_set():
            logger.info("⏹️ 用户中断，停止运行")
            stats["stopped"] = True
            break
        dynamic_id = d["dynamic_id"]
        if dynamic_id in processed:
            stats["skipped_processed"] += 1
            continue

        bvid = d["bvid"]
        progress += 1
        logger.info("━" * 40)
        logger.info("📌 [%d/%d] [%s] %s", progress, total_new, d["author_name"], d["title"])
        logger.info("   BV: %s", bvid)

        is_self = str(d["author_uid"]) == str(my_uid)
        author_uid_str = str(d["author_uid"])

        if is_self and not interact_own:
            logger.info("  ⏭️ 跳过自己的动态")
            processed.add(dynamic_id)
            stats["skipped_self"] += 1
            continue

        if list_mode == "whitelist":
            # 白名单模式：只处理白名单中的用户（自己免检）
            if not is_self and author_uid_str not in whitelist:
                logger.info("  ⏭️ 不在白名单中（UID=%s）", author_uid_str)
                processed.add(dynamic_id)
                stats["skipped_blacklist"] += 1  # 复用统计字段
                continue
        else:
            # 黑名单模式：互关检查 + 黑名单过滤
            if not is_self and not await check_mutual_follow(d["author_uid"], cred):
                logger.info("  ⏭️ 非互关")
                processed.add(dynamic_id)
                stats["skipped_not_mutual"] += 1
                continue

            if blacklist and author_uid_str in blacklist:
                logger.info("  ⏭️ 黑名单用户 UID=%s", d["author_name"])
                processed.add(dynamic_id)
                stats["skipped_blacklist"] += 1
                continue

        if bvid in seen_bvids:
            logger.info("  ⏭️ 已互动过该 BV（合作投稿），跳过")
            processed.add(dynamic_id)
            stats["skipped_seen"] += 1
            continue

        # 显示本次将执行的动作
        act_list = [k for k, v in actions.items() if v]
        logger.info("   📋 执行: %s", " + ".join(act_list) if act_list else "无")

        # 指定投币对象：仅对目标 UID 的视频投币，其他视频跳过投币
        call_cfg = cfg
        if coin_target_set and str(d["author_uid"]) not in coin_target_set:
            call_cfg = dict(cfg)
            call_cfg["actions"] = dict(cfg.get("actions", {}))
            call_cfg["actions"]["triple"] = False
            call_cfg["actions"]["coin"] = False

        action_results = await interact_with_video(bvid, d["aid"], d["author_name"], cred, my_uid, config=call_cfg, stop_event=_stop)
        seen_bvids.add(bvid)
        processed.add(dynamic_id)
        processed_mf.append(d)
        stats["interacted"] += 1
        # 累计动作结果
        for action, result in action_results.items():
            if result in ("success", "already_done"):
                stats["action_success"][action] = stats["action_success"].get(action, 0) + 1
            elif result in ("failed", "auth_failed", "risk_control"):
                stats["action_failed"][action] = stats["action_failed"].get(action, 0) + 1

        # ── 紧凑进度行：每个动作 ✅/❌/— ──
        _RESULT_ICON = {"success": "✅", "already_done": "✅", "failed": "❌", "auth_failed": "❌",
                        "risk_control": "⚠️", "not_triggered": "—", "skipped": "—"}
        parts = []
        for action_key in ("triple", "like", "coin", "favorite", "share", "comment", "rival_reply", "cover_comment"):
            if action_key in action_results or actions.get(action_key):
                icon = _RESULT_ICON.get(action_results.get(action_key, ""), "—")
                parts.append(f"{_ACTION_ICONS[action_key]}{icon}")
        result_line = " | ".join(parts)
        logger.info("   📊 结果: %s", result_line)
        logger.info("   📈 进度: %d/%d 已完成", progress, total_new)

        if on_interact:
            on_interact(bvid)
        if await _interruptible_sleep(random.uniform(10, 20), _stop):
            logger.info("⏹️ 用户中断，停止运行")
            stats["stopped"] = True
            break

    save_processed(processed)
    interacted_data["bvids"] = sorted(seen_bvids)
    INTERACTED_BVIDS_FILE.write_text(
        json.dumps(interacted_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── 结果汇总 ──
    _ACTION_NAMES = {
        "triple": "三连", "like": "点赞", "coin": "投币", "favorite": "收藏",
        "share": "分享", "comment": "评论", "rival_reply": "嘲讽回复", "cover_comment": "封面评论",
    }
    logger.info("")
    logger.info("━" * 40)
    logger.info("📊 执行结果汇总")
    logger.info("━" * 40)
    logger.info("📺 动态总数: %d", stats["total_dynamics"])
    logger.info("✅ 已互动: %d 个视频", stats["interacted"])
    skipped_total = stats["skipped_processed"] + stats["skipped_not_mutual"] + stats["skipped_blacklist"] + stats["skipped_seen"]
    logger.info("⏭️ 跳过: %d (已处理: %d, 非互关/非白名单: %d, 黑名单: %d, 已互动: %d)",
                skipped_total, stats["skipped_processed"], stats["skipped_not_mutual"],
                stats["skipped_blacklist"], stats["skipped_seen"])
    if stats["interacted"] > 0:
        logger.info("┄" * 40)
        logger.info("📈 动作统计:")
        for action_key, name in _ACTION_NAMES.items():
            s = stats["action_success"].get(action_key, 0)
            f = stats["action_failed"].get(action_key, 0)
            if s > 0 or f > 0:
                logger.info("  %s: 成功 %d / 失败 %d", name, s, f)
    if stats["stopped"]:
        logger.info("🛑 状态: 用户中断")
    logger.info("━" * 40)


# =========================================================================
#  历史投稿互动模式
# =========================================================================

async def get_user_videos_in_range(
    uid: int, cred: Credential, days: int = 30, author_name: str = ""
) -> list[dict]:
    """Get a user's videos within the specified time range.
    Returns list of {bvid, aid, title, author_name, author_uid, created}.
    Videos are ordered newest first; pagination stops when cutoff is reached.
    Includes anti-risk-control delays between page requests.
    """
    from bilibili_api.user import User, VideoOrder

    u = User(uid=uid, credential=cred)

    # Get author name if not provided
    if not author_name:
        try:
            info = await u.get_user_info()
            author_name = info.get("name", f"UID:{uid}")
        except Exception:
            author_name = f"UID:{uid}"

    cutoff_ts = time.time() - days * 86400
    result = []

    for pn in range(1, 50):  # max 50 pages (1500 videos)
        # ── 防风控：翻页前加随机延迟（第1页除外） ──
        if pn > 1:
            _delay = random.uniform(2, 5)
            await asyncio.sleep(_delay)

        # ── 每页最多重试 2 次（针对 412 风控） ──
        _page_ok = False
        for _attempt in range(3):
            try:
                data = await u.get_videos(pn=pn, ps=30, order=VideoOrder.PUBDATE)
                _page_ok = True
                break
            except Exception as e:
                err_str = str(e)
                if "412" in err_str and _attempt < 2:
                    _wait = random.uniform(30, 60)
                    logger.warning("⚠️ 拉取用户 %d 第 %d 页触发 412 风控，等待 %.0f 秒后重试 (%d/2)...",
                                   uid, pn, _wait, _attempt + 1)
                    await asyncio.sleep(_wait)
                else:
                    logger.warning("拉取用户 %d 第 %d 页投稿失败: %s", uid, pn, e)
                    return result

        if not _page_ok:
            break

        vlist = data.get("list", {}).get("vlist", [])
        if not vlist:
            break

        out_of_range = False
        for v in vlist:
            created = v.get("created", 0)
            if created < cutoff_ts:
                out_of_range = True
                break
            result.append({
                "bvid": v.get("bvid", ""),
                "aid": v.get("aid", 0),
                "title": v.get("title", ""),
                "author_name": author_name,
                "author_uid": uid,
                "created": created,
            })

        if out_of_range:
            break

        # Check if there are more pages
        page_info = data.get("page", {})
        total = page_info.get("count", 0)
        if pn * 30 >= total:
            break

    return result


async def run_history_interact(
    target_uids: list[int],
    days: int = 30,
    stop_event: threading.Event | None = None,
    on_interact=None,
    extra_handler=None,
) -> None:
    """历史投稿互动入口。对指定用户在时间范围内的投稿逐个执行互动。

    Args:
        target_uids: 目标用户 UID 列表
        days: 时间范围（天）
        stop_event: 停止信号
        on_interact: 回调函数 callback(bvid)
        extra_handler: 日志 Handler（WebUI 场景）
    """
    _stop = stop_event or threading.Event()
    log_file = CONFIG_DIR / "bili_auto.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    _formatter = logging.Formatter("[%(asctime)s] [AUTO-HISTORY] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # 清除旧 handlers，重新配置
    logger.handlers.clear()

    if extra_handler is not None:
        handlers = extra_handler if isinstance(extra_handler, list) else [extra_handler]
        for h in handlers:
            if not h.formatter:
                h.setFormatter(_formatter)
            logger.addHandler(h)
    else:
        _stdout_handler = logging.StreamHandler(sys.stdout)
        _stdout_handler.setFormatter(_formatter)
        logger.addHandler(_stdout_handler)

    # 始终写文件日志
    _file_handler = logging.FileHandler(log_file, encoding="utf-8", mode="a")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
    logger.setLevel(logging.INFO)

    logger.info("=" * 40)
    logger.info("启动历史投稿互动模式")
    logger.info("目标用户: %d 个，时间范围: %d 天内", len(target_uids), days)

    cfg = load_config()
    # 历史投稿使用独立的 history_actions 配置，回退到通用 actions
    history_actions = cfg.get("history_actions", cfg.get("actions", {}))
    # 构建传给 interact_with_video 的配置，用 history_actions 覆盖 actions
    hist_cfg = dict(cfg)
    hist_cfg["actions"] = history_actions
    enabled = [k for k, v in history_actions.items() if v]
    logger.info("启用动作: %s", ", ".join(enabled) if enabled else "无")

    cred = load_credential()
    if not cred:
        logger.info("未找到凭证，请在页面上扫码登录后重试")
        return

    # 获取登录用户信息
    logger.info("正在获取用户信息...")
    try:
        me = await asyncio.wait_for(user.get_self_info(cred), timeout=30)
        if not me or not isinstance(me, dict):
            logger.error("获取用户信息失败: 返回数据异常 (%s)，请重新登录", type(me).__name__)
            return
        my_uid = me.get("mid")
        logger.info("登录用户: %s (UID=%s)", me.get("name"), my_uid)
    except asyncio.TimeoutError:
        logger.error("获取用户信息超时(30s)，请检查网络或重新登录")
        return
    except Exception as e:
        logger.error("获取用户信息失败: [%s] %s", type(e).__name__, e)
        logger.error("凭证可能已失效，请在页面上重新扫码登录")
        return

    # 加载已互动记录
    today = time.strftime("%Y-%m-%d")
    interacted_data: dict = {}
    if INTERACTED_BVIDS_FILE.exists():
        interacted_data = json.loads(INTERACTED_BVIDS_FILE.read_text(encoding="utf-8"))
    if interacted_data.get("_date") != today:
        interacted_data = {"_date": today, "bvids": []}
    seen_bvids: set[str] = set(interacted_data.get("bvids", []))

    # 拉取所有目标用户的投稿
    all_videos: list[dict] = []
    for uid in target_uids:
        if _stop.is_set():
            logger.info("⏹️ 用户中断，停止拉取投稿")
            break
        logger.info("正在拉取用户 UID=%d 的投稿...", uid)
        try:
            videos = await get_user_videos_in_range(uid, cred, days)
            logger.info("  获取到 %d 个视频（%d天内）", len(videos), days)
            all_videos.extend(videos)
        except Exception as e:
            logger.warning("拉取用户 UID=%d 投稿失败: %s", uid, e)

    if not all_videos:
        logger.info("未找到符合条件的投稿")
        return

    logger.info("")
    logger.info("📬 共获取到 %d 个视频待处理", len(all_videos))
    logger.info("")

    # 统计数据
    stats = {
        "total_videos": len(all_videos),
        "skipped_seen": 0,
        "interacted": 0,
        "action_success": {"triple": 0, "like": 0, "coin": 0, "favorite": 0, "share": 0, "comment": 0, "rival_reply": 0, "cover_comment": 0},
        "action_failed": {"triple": 0, "like": 0, "coin": 0, "favorite": 0, "share": 0, "comment": 0, "rival_reply": 0, "cover_comment": 0},
        "stopped": False,
    }

    _ACTION_ICONS = {
        "triple": "三连", "like": "点赞", "coin": "投币", "favorite": "收藏",
        "share": "分享", "comment": "评论", "rival_reply": "嘲讽", "cover_comment": "封面",
    }

    progress = 0
    total = len(all_videos)
    _consecutive_risk = 0  # 连续风控计数
    _videos_since_break = 0  # 距上次长休息的视频数
    _BREAK_EVERY = random.randint(10, 15)  # 每 10-15 个视频长休息一次

    for v in all_videos:
        if _stop.is_set():
            logger.info("⏹️ 用户中断，停止运行")
            stats["stopped"] = True
            break

        bvid = v["bvid"]
        if bvid in seen_bvids:
            stats["skipped_seen"] += 1
            continue

        # ── 防风控：累计一定数量后长休息 ──
        if _videos_since_break >= _BREAK_EVERY:
            _rest_secs = random.uniform(60, 180)  # 1-3 分钟
            logger.info("😴 防风控休息：已处理 %d 个视频，休息 %.0f 秒...", _videos_since_break, _rest_secs)
            if await _interruptible_sleep(_rest_secs, _stop):
                logger.info("⏹️ 用户中断，停止运行")
                stats["stopped"] = True
                break
            _videos_since_break = 0
            _BREAK_EVERY = random.randint(10, 15)  # 重置下次休息阈值

        progress += 1
        logger.info("━" * 40)
        logger.info("📌 [%d/%d] [%s] %s", progress, total, v["author_name"], v["title"])
        logger.info("   BV: %s", bvid)

        # 显示本次将执行的动作
        act_list = [k for k, v2 in history_actions.items() if v2]
        logger.info("   📋 执行: %s", " + ".join(act_list) if act_list else "无")

        action_results = await interact_with_video(
            bvid, v["aid"], v["author_name"], cred, my_uid,
            config=hist_cfg, stop_event=_stop,
        )
        seen_bvids.add(bvid)
        stats["interacted"] += 1
        _videos_since_break += 1

        # ── 防风控：检测风控错误，连续触发则延长休息 ──
        _has_risk = any(r in ("risk_control", "auth_failed") for r in action_results.values())
        if _has_risk:
            _consecutive_risk += 1
            if _consecutive_risk >= 3:
                _long_rest = random.uniform(180, 300)  # 3-5 分钟
                logger.info("⚠️ 连续 %d 次风控告警，强制休息 %.0f 秒...", _consecutive_risk, _long_rest)
                if await _interruptible_sleep(_long_rest, _stop):
                    stats["stopped"] = True
                    break
                _consecutive_risk = 0
        else:
            _consecutive_risk = 0

        # 累计动作结果
        for action, result in action_results.items():
            if result in ("success", "already_done"):
                stats["action_success"][action] = stats["action_success"].get(action, 0) + 1
            elif result in ("failed", "auth_failed", "risk_control"):
                stats["action_failed"][action] = stats["action_failed"].get(action, 0) + 1

        # 紧凑进度行
        _RESULT_ICON = {"success": "✅", "already_done": "✅", "failed": "❌", "auth_failed": "❌",
                        "risk_control": "⚠️", "not_triggered": "—", "skipped": "—"}
        parts = []
        for action_key in ("triple", "like", "coin", "favorite", "share", "comment", "rival_reply", "cover_comment"):
            if action_key in action_results or history_actions.get(action_key):
                icon = _RESULT_ICON.get(action_results.get(action_key, ""), "—")
                parts.append(f"{_ACTION_ICONS[action_key]}{icon}")
        result_line = " | ".join(parts)
        logger.info("   📊 结果: %s", result_line)
        logger.info("   📈 进度: %d/%d 已完成", progress, total)

        if on_interact:
            on_interact(bvid)
        if await _interruptible_sleep(random.uniform(10, 20), _stop):
            logger.info("⏹️ 用户中断，停止运行")
            stats["stopped"] = True
            break

    # 保存已互动记录
    interacted_data["bvids"] = sorted(seen_bvids)
    INTERACTED_BVIDS_FILE.write_text(
        json.dumps(interacted_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ── 结果汇总 ──
    _ACTION_NAMES = {
        "triple": "三连", "like": "点赞", "coin": "投币", "favorite": "收藏",
        "share": "分享", "comment": "评论", "rival_reply": "嘲讽回复", "cover_comment": "封面评论",
    }
    logger.info("")
    logger.info("━" * 40)
    logger.info("📊 历史投稿互动执行结果汇总")
    logger.info("━" * 40)
    logger.info("📺 投稿总数: %d", stats["total_videos"])
    logger.info("✅ 已互动: %d 个视频", stats["interacted"])
    logger.info("⏭️ 跳过: %d (已互动过)", stats["skipped_seen"])
    if stats["interacted"] > 0:
        logger.info("┄" * 40)
        logger.info("📈 动作统计:")
        for action_key, name in _ACTION_NAMES.items():
            s = stats["action_success"].get(action_key, 0)
            f = stats["action_failed"].get(action_key, 0)
            if s > 0 or f > 0:
                logger.info("  %s: 成功 %d / 失败 %d", name, s, f)
    if stats["stopped"]:
        logger.info("🛑 状态: 用户中断")
    logger.info("━" * 40)
