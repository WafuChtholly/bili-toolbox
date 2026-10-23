# -*- coding: utf-8 -*-
"""播放来源「客户端桶」实验：一条稿件只打一种指纹，靠图表的桶分布判归属。

背景：
  creator 后台「播放来源 — 客户端类型」分为 H5 / PC / 移动 / TV / 其他。
  历史数据显示 H5≈50、PC≈37、移动≈8、TV/其他=0，
  说明 click/web/h5 与 click/h5 各自落进独立桶，且服务端按请求特征硬归桶。

  因此第三条额度不是「换 click 参数」，而是「抢到第三个桶（移动/APP 或 TV）」。

方法：
  不再做时间窗口轮转（延迟结算 5–30min，归因必然串扰），改为平行对照：
  一条稿件 = 一种指纹，打满即收手；数小时后直接看该稿件的播放来源形状。

已确认结论（2026-10，勿重复验证）：
  1. click/web/h5 -> PC 桶，click/h5 -> H5 桶，两份额度独立且都真实计数。
  2. android2 / click/android / click/ios 对匿名一律 14001，空请求体也是 14001
     => 端点在参数校验前就拒绝匿名流量，不是签名问题（签名算法本身已在 view 接口验证通过）。
  3. os/tv 指纹（profile=tv）判死：同池同分钟平行对照，tv 69 次 code=0 在 60 分钟内 +0，
     h5 92 次 code=0 同期 +224。code=0 只代表端点收下，不代表进计数链路。
  4. 播放量延迟结算 5–30+ 分钟，短窗读数不可用于归因。
  5. 单稿件单通道累计约 200 次后额度耗尽，之后该通道对该稿件不再计数
     （BV14ZJR6DEaf 吃过近 200 次 h5 后，h5 再打 57 次 code=0 也是 +0）。
     => 判定必须用「从没被碰过的稿件」，一条稿件只打一种指纹。
  6. 第三方 in-app 浏览器 UA 能真实计数（code=0 -> 播放增量，落账需 5–35 分钟）：
       wx      29 -> +26
       qqb     22 -> +22
       aweme   19 -> +44   （涨得最慢，T+10 才开始，60 分钟仍在涨）
       wx      96 -> +84   （另一条全新稿件，T+30 一次性落账）
     同一批代理、同一时间窗内落账，说明是 UA 通过校验，不是代理运气。
  6b. tvbox 判死（自有稿件复核）：BV1dYnV6SEts（431 播放、单 P）连打两轮共 107 次 code=0，
     55 分钟只 +1（自然流）=> /click/h5 + 小米电视 UA + spmid 333.999 不进计数链路。
     此前他人稿件 BV1Vqnw6EEcS 的 19 -> +19 属自然流，不能作证；电视 UA 一律不要再用。
  7. UA 白名单是硬门槛，以下 UA 一律 code=0 但 35–60 分钟只 +1（判死）：
       pad 17、harmony 15、weibo 20、app_android 18、tv 69、
       xhs 17、kuaishou 14、quark 17、uc 12、mqq 11
     => 「换个 UA 就能计数」只对特定第三方 App 内置浏览器成立。
  8. 桶由「端点 + UA」共同决定：PC 端点 /click/web/h5 无论换微信还是 QQ 浏览器 UA
     都不计数（wx_pc 21 -> +0、qqb_pc 34 -> +0）；UA 只在 /click/h5 上生效。
  9. 端点级拒绝：/click/h5 + 小程序 referer = 403，/click-interface/click = 404，
     /click-interface/click/ott = 404，均与参数无关，直接判死。
 10. 原生 iPhone UA 的 /click/h5 已失效：两条全新稿件 23 次 / 15 次 code=0
     在 60 / 35 分钟内均 +0，而同期 wx / qqb / aweme 正常计数
     => 是该 UA 指纹被风控，不是 H5 通道死。修法是轮换 UA，不是换代理。
 11. 额度是「稿件级」不是「UA 级」：BV1JzJf6JEfW 先打 wx 252 次（+89），
     再换 qqb 20 次 code=0，37 分钟 +0
     => UA 轮换不产生新额度，同一稿件不要叠加第二种 UA，换 UA 必须换稿件。
 12. 落账量普遍低于 code=0（wx 96 -> +84、wx 252 -> +89），且延迟 5–35 分钟，
     所以短窗读数只能看「有没有涨」，不能按比例估量。
 13. alipay 判死（自有已满稿件复核）：BV14ZJR6DEaf（H5 已打满，439 播放）打 300 次、
     code=0 112 次，30 分钟只 +1（自然流）。
     => 稿件额度耗尽后换 alipay UA 也不产生新计数，第三条额度不能靠 /click/h5 换 UA 拿到。
     wx / qqb / aweme 是否复用 H5 桶同样待后台「播放来源 — 客户端类型」核对。
 14. 网页心跳 web/heartbeat 判死：匿名 250/250 code=0（端点全收），
     BV14ZJR6DEaf 30 分钟 +1（自然流）。
     => 心跳只上报观看时长/留存，不产生播放计数，不是第三条额度。
     匿名请求可被接受但服务端不按它累加 view。
 15. 移动端 UA 扩展扫描（13 个，全部判死）：在已满稿件 BV14ZJR6DEaf 上共 233 次 code=0，
     30 分钟 +0。
       桌面/平板/鸿蒙：mac_safari 24、ipad_17 25、harmony_next 21、
                    huawei_browser 18、samsung_browser 17、miui_browser 23
       App 内置：dingtalk 15、taobao 10、baidu 19、toutiao 16、zhihu 13、
                edge_mobile 15、firefox_mobile 17
     => 移动端没有任何 UA 能突破「稿件级额度」，换平板/桌面 UA 也拿不到额外计数。
     注：taobao / zhihu 偶发 code=0x01900012（参数校验拒绝），与计数无关。
 16. 「web 段可替换」判死：把 /click/web/h5 的 web 换成
     tv/mobile/app/pad/ott/pc/m/android/other 共 9 个（配已验证可计数的微信 UA），
     直连全部 http404（统一 nginx 404 页）=> 该段是固定路由，不是桶选择器。
     另测 6 种段位形态（click/{seg}、click/web/{seg}、click/{seg}/web、
     click/h5/{seg}、{seg}/h5、click/{seg}/h5）× tv/mobile/other 共 18 个，同样全 404；
     对照组 h5 / pc / wx 同批 http200 code=0，排除「整站拒绝」的可能。
     结论：TV / 移动 / 其他桶不可能通过改 URL 段名拿到。
 17. 裸段 /click/{seg}（不带 /h5）穷举 16 个段名，只有 3 个路由存在：
       /click/h5        http200 code=0     （已验证计数，H5 桶）
       /click/android   http200 code=14001 （匿名一律拒，同第 2 条）
       /click/ios       http200 code=14001 （同上）
     其余 tv/mobile/app/pad/ott/pc/m/iphone/ipad/web/other/tvbox/xbox 全部 http404。
     => click-interface 下不存在「TV / 移动 / 其他」的裸段路由，
      TV 桶在这套 HTTP 端点上没有任何入口。
 18. TV / 车机 APK 静态解析（bilithings=云视听TV全量API、iBiliTV=第三方TV）：
     全量 /x/tv/... 清单 283 条，与计数相关的只有
       /x/report/click/android/tv       （TV 播放上报）
       /x/tv/playurl/report             （播放地址解析上报）
       /x/tv/history/add                （历史上报，-101 需登录）
       /x/report/click/android2         （APP 点击上报的另一前缀）
       /x/report/heartbeat/mobile       （APP 心跳）
       /x/car/history/report            （车机历史上报，app.bilibili.com）
       /x/tv/guest/dummy_login          （TV 游客登录，iBiliTV 的 BiliAuthService）
       /x/click-interface/click/now     （时钟同步，TV 专用域名 api.snm0516.aisee.tv）
     注：guest_access_key / dummy_login 属于 iBiliTV（TV 端），不在 bilithings（车机端）。
     appkey/appsec 是服务端下发或 native 载体，dex 内无 const-string 引用。
     两个 APK 的 DEX 注解区被改写/错位（@BaseUrl、@POST value 指向无关字符串），
     无法从注解恢复 URL 与 @Query 参数名，只能靠 const-string 交叉引用 + 运行时探测。
     iBiliTV 真实播放上报链路：PlayerStat.d() 组 body（aid/cid/ftime/stime/type/
     access_key/guest_id/spmid...）-> AES 加密 + &sign= -> HeartbeatApiService.reportV2
     （@FormUrlEncoded @POST），日志 TAG PlayReportServiceV3。
 19. /x/report/click/android2 与 /x/report/click/android/tv 判死：
     空体 / 基础参数 / 6 组 appkey-appsec 真 sign / buvid3+buvid4 cookie+Buvid 头
     全部 http200 code=14001。14001 与 sign、UA、buvid 都无关，是端点级风控，
     TV 桶在 HTTP 上必须登录态 + 设备指纹才可能通过。
 20. /x/tv/playurl/report 是「不校验」端点：空体、乱填、全套参数都 code=0，
     说明它对入参不敏感（纯日志上报），不可能是播放计数入口。
 21. 新发现两个匿名可用的上报端点（已注册 profile）：
       hb_mobile    /x/report/heartbeat/mobile + 安卓 appkey 真 sign -> code=0
                    （缺 sign -400；tv_new 签名 -3、tv_car 签名 -404，
                      android/android_hd/783bbb/iphone 四组均 0）
       car_history  app.bilibili.com/x/car/history/report + Buvid 头 -> code=0
                    （缺 buvid 时 90006「缺少buvid或者mid~」）
 22. hb_mobile / car_history 判死（两次独立计数实验闭环）：
     ① BV14ZJR6DEaf（已打满）两通道各打 120 次 code=0，32 分钟 17 点采样，
        两列 454/441 -> 503/488（各 +49 / +47），与自然流同斜率（约 1.5/min），无跳变。
     ② BV1BqJR6NETx（422 播放、全新未碰过）复测：hb_mobile 123 次 code=0、
        car_history 121 次 code=0（失败重试到成功数收敛，三分之一走直连、其余走代理），
        34 分钟 17 点采样 422 -> 422，全程零增长（该稿自然流也为 0，任何计数都藏不住）。
     => 两个端点都只回执不计数，不进播放计数链路，不是额度。
 23. /x/tv/guest/dummy_login 匿名可用（首次拿到 TV 游客身份）：
     POST https://api.bilibili.com/x/tv/guest/dummy_login，必须下划线（驼峰 dummyLogin 404，
     GET 405），一次性灌全参数集（约 40 个候选名）明文请求 -> code=0，返回
       {"guest_id":"9371710286492","access_key":"GT_9371710286492"}
     反差点：带 sign/ts 反而 -400（sink_signed -400、min_signed「请求错误」），
     只带 did/appkey/build 也 -400（缺必填参数）=> -400 是参数校验，不是端点拒绝。
     同端点打到 TV 专属域名 api.snm0516.aisee.tv 一律 -400，凭证仅在 api.bilibili.com 有效。
 24. TV 桶匿名无入口（最终裁决）：
     用第 23 条的 guest 凭证，对 /x/report/click/android/tv、/x/click-interface/click/android/tv
     （以及 api.snm0516.aisee.tv 同路径）测试 3 种凭证模式（none / access_key+guest_id /
     guest_access_key+guest_id）× 5 种请求形态（plain / signed / signed_ts_only /
     signed_headers / plain_headers，带 BiliTV UA、Buvid/Display-ID/Device-ID 头、
     buvid3/buvid4 cookie）=> 3 端点 × 15 组全部 code=14001。
     同期对照 /x/click-interface/click/web/h5 三种凭证模式均 code=0。
     => TV 桶在 HTTP 上对匿名完全封闭（端点级风控），换 host / UA / 凭证 / 签名 / 头都无效。
 25. /x/report/click/now 与 /x/click-interface/click/now 不是计数：
     smali 核对都是 ServerClock 的时钟同步 GET（读 data.now 算本地偏移），
     两个 APK 里各自只此一处，别再当候选通道测。
 26. iBiliTV 的 AES 上报链路已完整还原（DEX 注解 + smali + 实测三重确认）：
     端点  POST http://api.bilibili.com/x/report/click/android/tv
           （PlayerStat$d 类注解 @BaseUrl("http://api.bilibili.com")，
             方法注解 @POST("/x/report/click/android/tv")）
     形态  body = AES/CBC/PKCS5 的原始密文字节（非 Base64），
           Content-Type: application/octet-stream，UA: Mozilla/5.0 BiliTV/1.8.9
     明文  TreeMap 字典序拼 k=v（空值也输出 "k="），尾部 &sign=<hex>
     签名  sign = lower(hex(SHA256(query_utf8 || signKey)))   ← 拼接后哈希，不是 HMAC
     常量（fill-array-data 存的是这串 hex 字符本身的 ASCII 字节，不是 hex 解码！）
           AES key = b"fd6b639dbcff0c2a1b03b389ec763c4b"（32B -> AES-256）
           IV      = b"77b07a672d57d64c"（16B）
           signKey = b"9cafa6466a028bfb"（16B）
     参数  platform=android, mobi_app=android_tv_yst, build=108900,
           appkey=4409e2ce8ffd12b8（LibBili.getAppKey("android_tv") 的首个命中值）,
           type=1, sub_type=0, part=0, play_mode=0, ftime=0,
           extra = 单引号 JSON {'k':'v',...} 再 URLEncode(US-ASCII)
 27. 第 19/24 条「14001 = 端点级风控、TV 桶匿名封闭」的结论作废（负对照推翻了它）：
     同一端点、同一匿名 guest 凭证（guest_id=9371710286492），只改密文：
       正确 key+sign -> code=0 且返回 data.rpdid（rpdid 是可用的播放回执）
       坏 AES key    -> code=14001
       坏 sign       -> code=14004
       乱码明文      -> code=14005
     => 14001 是「密文解不开」，不是风控；TV 桶在 HTTP 上对匿名是开放的，
        此前所有 14001 都因为没走 AES 加密体。
 28. /x/report/click/android/tv 的 AES 链路能匿名打通（code=0 + data.rpdid），但**不产生播放计数**（判死）：
     计数实验（全部 30 分钟以上采样）：
       ① 唯一 guest × 同一出口 IP，40 次 code=0：BV1E9nN6PEXM 0 -> 1（+1，自然流）
       ② 唯一 guest × 唯一代理 IP，17 次 code=0：BV16DnN6KEEc 0 -> 15
       ③ 唯一 guest × 唯一代理 IP，13 次 code=0：BV1Z9nN6PEbb 1 -> 16
       ④ 同 guest，40 次 code=0：BV179nN6PEqW 0 -> 39，与同批稿件自然流同步
       ⑤ 终局对照（关键证据）：BV1BqJR6NETx（老稿、426 播放、自然流≈0）
          唯一 guest × 唯一代理 IP，20 次 code=0 -> T+12min 427、T+27min 仍 427（+1）
     ②③ 的 +15 是噪音：这几条是同一位 UP 主同批上传的稿件（同批 BV1E9nN6PEr4 = 215 播放、
     BV179nN6PEqW 37 播放 6 赞 2 币），存在真实观众/分享流量；同期未打的 BV1E9nN6PEXM 恒为 1。
     ③ 后续 46 分钟只从 15 爬到 17，与同批未打稿件同斜率（PEXM 1->2、PESk 2->3、PEqW 39->44），
     与上报次数无比例关系。
     ⑤ 在自然流≈0 的老稿上零增长，才是可归因的读数。
     => 与 hb_mobile / car_history / os-tv 指纹同类：端点收下并回 rpdid，但不累加 view。
        TV 桶在匿名 HTTP 上拿不到额度；第 24 条「无入口」应更正为「有入口、有回执、不计数」。
 29. did 参数会被参数校验直接拒绝（14013），与取值 / 格式 / 是否与 guest 匹配都无关：
     用 did=D 领 guest 后，上报 body 带 D、随机 did、32hex、buvid、guest_id、纯数字、
     uuid、大写 hex => 全部 14013；不带 did 立即 code=0。
     did 放请求头（Device-ID / device_id / did / X-Bili-Device-Id / X-Bili-Device）不报错，仍 code=0。
     dummy_login 只返回 {"guest_id","access_key"}，不下发 did 或设备令牌
     => 匿名侧无法构造「已注册设备」，PlayerStat.d() 的 did 字段无法完整复刻
        （这是本地实现与真机唯一已知差异；但第 28 条已证明计数本身不依赖 did）。
 30. /x/tv/guest/dummy_login 每次调用都发全新 guest_id，与设备参数无关：
     连发 5 次（4 次随机设备参数 + 1 次固定参数）-> 9372133789156、9372133793871、
     9372133801019、9372133805689、9372133810347，连续递增。
     => 可无限领取 TV 游客身份；第 28 条的「唯一 guest」实验即据此构造。
 31. click/android2（车机 AES）+ hb_mobile 组合是目前唯一「点击即涨」的通道：
     单会话链路：click1(did=随机 8 位) -> 14013 但 data.rpdid 有值；
                  click2(did=rpdid，同一 buvid/buvid4) -> code=0；
                  随后 start/mid/end 三次心跳 -> code=0。
     BV1SKH96WEnh 657 -> 658，**t+0 立即可见**（h5 通道的 5–30 分钟延迟不适用于此通道）。
     并发吞吐（同一稿件、Barrier 同刻起跑、每会话全新 buvid3/buvid4/did/session）：
        single 同出口 IP 5 会话（5/5 复点成功）-> +3
        multi  5 个不同出口 IP（4/5 复点成功）-> +2
        burst  同 IP 4 批 × 5 会话，批间隔 65s -> [1, 0, 2, 0]，合计 +3
      N 扫描（同 IP 单批，全部 click2=0，基线均静止）：
        N=1 -> +1     N=5 -> +3     N=10 -> +4
        N=20 -> +1（连批时）+1（冷却 300s、基线 5 分钟 +0 后复测，仍 +1）
      => 去重键**不含出口 IP**：换代理拿不到 N 倍，代理只解决「请求能出去」不解决额度。
         单批最优 N≈5–10（+3~4），N=20 反向劣化到 +1：20/20 全部 code=0 但计数侧
         把同 IP 高并发会话归并/压制，不是请求失败。也不是严格的「60s 一次」。
         规模化不能靠「单稿件堆并发」，只能「多稿件 × N≈5–10」。
     与第 22 条不冲突：hb_mobile 单独打不计数，这里是 click 打底后心跳才进入链路。
 32. 非登录态单稿件上限会随当日累计请求量衰减（同一天内复测，第 31 条之后）：
     测试对象：本账号（mid 3546857487731451，用 APP 匿名接口
       https://app.bilibili.com/x/v2/space/archive/cursor + android appkey 签名拉到）
       400+ 播放稿件，每条跑 burst N=5 / N=10 两批（play_s=20，批间 65s，click2 全 0）：
         BV1j6JR6AEGq  424 -> +1 / +1
         BV1VcnP6DEWV  451 -> +1 / +1
         BV1Utnq63E5G  601 -> +2 / +1（该稿自然增长中，20 分钟 +7，数据不可用）
         BV1AFnA6YEEh  887 -> +1 / +1
       4 分钟后复查无延迟补记（426/453/889 不变）。
       同参数对照组 BV1SKH96WEnh 也由数小时前的 +3/+4 降到 +1/+1；
       再用 5 个不同出口 IP 的 multi 组复测（5/5 复点成功）仍只有 +1。
     => 当前单稿件上限 ≈ **每批 +1**：N=5/10/20 无差别，换出口 IP 也无效，
        压制发生在会话指纹/稿件层，不按 IP 也不按并发数。上限随本机当日累计请求量
        衰减（早前 +3~4 -> 现在 +1）。代理池只解决连通性；规模化只能「多稿件 × 每稿 1」，
        且要按衰减后的真实额度估算，不能沿用第 31 条的 +3~4。
 33. 单会话串行（N=1、批间不加间隔）的吞吐与「每批 +1」等价，不比并发差：
     A) play_s=20、10 个串行会话（每个 28s，共 280s）：426 -> 430 = +4，
        入账落在第 1/4/7/10 个会话，间隔恰好 84s。
     B) play_s=10、10 个串行会话（每个 15s，共 150s）：895 -> 898 = +3
        （第 1 个 +1，第 9 个一次性 +2）。
     C) play_s=10、20 个串行会话（每个 13s，共 260s）：430 -> 433 = +3
        （第 1 个 +1，第 11 个一次性 +2），再等 2 分钟无补记。
     => 单稿件稳定产出 ≈ **每 65~85s 1 个播放，即 40~45/h**，与会话时长无关：
        13s 会话虽然请求密度翻倍，产出和 28s 会话一模一样，只在计数侧攒成 +2 批量补记。
        所以正确用法是「play_s=20 + 每 ~85s 一个会话」，不要压满请求密度。
        横向扩容只能靠稿件数（N 稿件 × 40/h），不是靠并发或缩短播放。
 34. 1 小时长时串行观测：84s 节拍不随时间衰减，且两稿件同 IP 并行互不干扰。
     play_s=20、每批 observe 55 + gap 85（实测每批 75~76s）、42 批、两稿件同时跑：
     A) BV1j6JR6AEGq 433 -> 476 = +43（仅第 1 批 +2，其余 41 批每批 +1，42/42 复点成功）
     B) BV1VcnP6DEWV  453 -> 495 = +42（42 批全部每批 +1，42/42 复点成功）
     => 单稿件 42~43/h 连续 1 小时不掉速，推翻「当日累计请求量导致衰减到 +1 以下」的担心：
        第 32 条的衰减是相对早前 +3~4 的批次上限，而不是会一路衰减到 0。
        同 IP 两路并行各拿满 42/h（合计 85/h），说明限额按稿件算、不按 IP 算，
        「多稿件 × 1 路/稿件」的横向扩容模型成立。
        落地参数：play_s=20、会话间隔 75~85s，不需要代理，单进程串行足够。
 35. 换 cid 上报不能翻倍：额度按 aid 立，cid 不参与分桶。
     目标 BV1j6JR6AEGq（aid=117467657540031, cid=42875685136, 单P），8 个 85s 窗口、
     每窗口 2 路并发，第二路分别用 real(本稿 cid) / donor(另一稿真实 cid) / fake(不存在)：
       W1 [real,real]  +1    W2 [real,donor] +1   W3 [real,fake]   +1   W4 [donor,donor] +1
       W5 [real,donor] +1    W6 [real,fake]  +1   W7 [donor,donor] +1   W8 [real,real]   +1
     每路 click1=14013 -> click2=0、hb0/50/100 全 0，即服务端全部接受；
     donor 稿件全程 495 -> 495（+0），说明换 cid 既不给目标翻倍，也不会涨到 cid 归属稿。
     W4 两路都是非法 (aid,cid) 配对仍拿到 +1 => 服务端不校验 cid 是否属于该 aid，
     直接按 aid 记一次浏览。故：
       - 「稿件不是多P、上报时换别的 cid」这条路无效，只能拿 1 份额度；
       - 真正的多P（同 aid 下合法 pages[].cid）是否各立额度仍未验证，但 cid 已被证明
         不进入分桶键，多P 翻倍的可能性很低，且必须先有真实多P稿件才能测。

用法：
  # 1) 扫候选稿件（单P、低播放）
  python tools/probe_source_bucket.py scan --limit 20

  # 2) 对某稿件打满一种指纹
  python tools/probe_source_bucket.py run BV1xxxxxxxxx --profile h5_spmid337 --count 60

  # 3) 看一眼某指纹实际发出的请求
  python tools/probe_source_bucket.py run BV1xxxxxxxxx --profile app_android --dry-run

  # 4) 列出所有指纹
  python tools/probe_source_bucket.py profiles
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
_BOOSTER_DIR = str(ROOT / 'bili-booster')
if _BOOSTER_DIR not in sys.path:
    sys.path.insert(0, _BOOSTER_DIR)

import booster  # noqa: E402

CLICK_WEB = 'http://api.bilibili.com/x/click-interface/click/web/h5'
CLICK_H5 = 'http://api.bilibili.com/x/click-interface/click/h5'
CLICK_ANDROID2 = 'http://api.bilibili.com/x/click-interface/click/android2'
CLICK_ROOT = 'http://api.bilibili.com/x/click-interface/click'
CLICK_OTT = 'http://api.bilibili.com/x/click-interface/click/ott'
HEARTBEAT = 'https://api.bilibili.com/x/click-interface/web/heartbeat'
HB_MOBILE = 'https://api.bilibili.com/x/report/heartbeat/mobile'
CAR_HISTORY = 'https://app.bilibili.com/x/car/history/report'
WORKERS = 50

DESKTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
IPHONE_UA = booster._mobile_ua
# BiliApp 客户端 UA 形如：Mozilla/5.0 BiliDroid/<ver> (bbcallen@gmail.com) os/android ...
APP_ANDROID_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 '
                  'Mobile Safari/537.36 BiliApp/7660300 os/android model/Pixel 7 '
                  'mobi_app/android build/7660300 channel/master innerVer/7660300 '
                  'osVer/13 network/2')
APP_TV_UA = ('Mozilla/5.0 (Linux; Android 9; SHIELD Android TV Build/PPR1.180610.011) '
             'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/90.0.4430.66 '
             'Mobile Safari/537.36 BiliApp/100000 os/tv model/SHIELD Android TV '
             'mobi_app/android build/100000 channel/master innerVer/100000 osVer/9 network/2')

# 以下指纹全部指向「其他」桶候选：第三方浏览器 / 微信 / 小程序 / 平板 / 鸿蒙
WECHAT_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
             'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
             'Mobile Safari/537.36 XWEB/1160065 MMWEBSDK/20231202 MMWEBID/1234 '
             'MicroMessenger/8.0.44.2500(0x28002C35) WeChat/arm64 Weixin NetType/WIFI '
             'Language/zh_CN ABI/arm64')
QQBROWSER_UA = ('Mozilla/5.0 (Linux; U; Android 13; zh-cn; Pixel 7 Build/TQ3A.230805.001) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/111.0.5563.116 '
                'MQQBrowser/14.6 Mobile Safari/537.36')
PAD_UA = ('Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 '
          '(KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1')
HARMONY_UA = ('Mozilla/5.0 (Phone; OpenHarmony 4.0) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36 '
              'ArkWeb/4.1.6.1 Mobile')
WEIBO_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
            'Mobile Safari/537.36 Weibo (Pixel 7__weibo__14.2.0__android__android13)')
AWEME_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
            'Mobile Safari/537.36 aweme_290000')
XHS_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
          'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
          'Mobile Safari/537.36 xhsdiscover/8.11.0')
KUAISHOU_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
               'Mobile Safari/537.36 Kwai/11.3.20')
ALIPAY_UA = ('Mozilla/5.0 (Linux; U; Android 13; zh-CN; Pixel 7 Build/TQ3A.230805.001) '
             'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 '
             'UWS/3.22.2.65 Mobile Safari/537.36 AlipayChannelId/3754')
QUARK_UA = ('Mozilla/5.0 (Linux; U; Android 13; zh-CN; Pixel 7 Build/TQ3A.230805.001) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 '
            'Quark/6.9.0.350 Mobile Safari/537.36')
UC_UA = ('Mozilla/5.0 (Linux; U; Android 13; zh-CN; Pixel 7 Build/TQ3A.230805.001) '
         'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 '
         'UCBrowser/15.5.2.1250 Mobile Safari/537.36')
MQQ_UA = ('Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
          'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
          'Mobile Safari/537.36 V1_AND_SQ_9.0.0_4000_YYB_D QQ/9.0.0.4000')
TVBOX_UA = ('Mozilla/5.0 (Linux; Android 9; MiTV-AESP0 Build/PI) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Version/4.0 Chrome/90.0.4430.66 Mobile Safari/537.36')
MP_REFERER = 'https://servicewechat.com/wx756f9c2f4e1a3b57/1/page-frame.html'


def _now():
    return int(time.time())


def _session():
    return ''.join(random.choices('0123456789abcdef', k=8))


def _spmid_payload(spmid):
    return {
        'spmid': spmid,
        'from_spmid': 'undefined',
        'session': _session(),
        'csrf': '',
    }


def _base_payload(info, bv):
    return {
        'aid': info['aid'],
        'cid': info['cid'],
        'bvid': bv,
        'part': '1',
        'mid': info['owner']['mid'],
        'type': info['desc_v2'][0]['type'] if info['desc_v2'] else '1',
        'sub_type': '0',
    }


def _web_headers(ua, bv, cookies):
    return booster.build_click_headers(ua, bv, cookies)


def _m_headers(ua, bv, cookies, referer=None):
    return {
        'User-Agent': ua,
        'Referer': referer or f'https://m.bilibili.com/video/{bv}',
        'Origin': 'https://m.bilibili.com',
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Cookie': cookies,
    }


# APP 端 appkey/appsec（社区公开的客户端常量），用于复现 sign
APPKEYS = {
    'android': ('1d8b6e7d45233436', '560c52ccd288fed045859ed18bffd973'),
    'android_bili': ('783bbb7264451d82', '2653583c8873dea268ab9386918b1d65'),
    'tv': ('4409e2ce8ffd12b8', '59b43e04ad6965f34319062b478f83dd'),
    'iphone': ('27eb53fc9058f8c3', 'c2ed53a74eeefe3cf99fbd01d8c9c375'),
}


def _sign(data, appsec):
    """B 站 APP 通用签名：参数按 key 排序 urlencode 后拼 appsec 取 md5。"""
    items = sorted((k, str(v)) for k, v in data.items() if k != 'sign')
    q = '&'.join(f'{k}={v}' for k, v in items)
    return hashlib.md5((q + appsec).encode('utf-8')).hexdigest()


# ───────────────────────── 指纹定义 ─────────────────────────
# 每条返回 (url, headers, data)。spmid 的桶归属未知，属于候选值，靠图表反向核对。
def _p_pc(info, bv, cookies):
    data = _base_payload(info, bv)
    data.update({'jsonp': 'jsonp'})
    return CLICK_WEB, _web_headers(DESKTOP_UA, bv, cookies), data


def _p_h5(info, bv, cookies, spmid='333.401'):
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload(spmid))
    return CLICK_H5, _m_headers(IPHONE_UA, bv, cookies), data


def _p_h5_337(info, bv, cookies):
    return _p_h5(info, bv, cookies, '333.337')


def _p_h5_788(info, bv, cookies):
    return _p_h5(info, bv, cookies, '333.788')


def _p_h5_999(info, bv, cookies):
    return _p_h5(info, bv, cookies, '333.999')


def _p_app(info, bv, cookies):
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now,
                 'mobi_app': 'android', 'platform': 'android',
                 'build': '7660300', 'appkey': '1d8b6e7d45233436'})
    data.update(_spmid_payload('333.788'))
    return CLICK_H5, _m_headers(APP_ANDROID_UA, bv, cookies), data


def _p_app_android2(info, bv, cookies):
    """APP 专用端点，无 sign 尝试（此前带假 sign 返回 14001）。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now,
                 'mobi_app': 'android', 'platform': 'android',
                 'build': '7660300', 'appkey': '1d8b6e7d45233436', 'sign': ''})
    data.update(_spmid_payload('333.788'))
    return CLICK_ANDROID2, _m_headers(APP_ANDROID_UA, bv, cookies), data


def _android2_signed(info, bv, cookies, key, ua, build):
    """android2 端点 + 真 sign（按 key 对应的 appkey/appsec 计算）。"""
    now = _now()
    ak, sec = APPKEYS[key]
    mobi = 'iphone' if key == 'iphone' else 'android'
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now,
                 'mobi_app': mobi, 'platform': mobi,
                 'build': build, 'appkey': ak})
    data.update(_spmid_payload('333.788'))
    data['sign'] = _sign(data, sec)
    return CLICK_ANDROID2, _m_headers(ua, bv, cookies), data


def _p_android2_sign(info, bv, cookies):
    return _android2_signed(info, bv, cookies, 'android', APP_ANDROID_UA, '7660300')


def _p_android2_sign_bili(info, bv, cookies):
    return _android2_signed(info, bv, cookies, 'android_bili', APP_ANDROID_UA, '7660300')


def _p_android2_sign_tv(info, bv, cookies):
    return _android2_signed(info, bv, cookies, 'tv', APP_TV_UA, '100000')


def _p_android2_sign_ios(info, bv, cookies):
    return _android2_signed(info, bv, cookies, 'iphone', IPHONE_UA, '7660300')


def _p_tv(info, bv, cookies):
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now,
                 'mobi_app': 'android', 'platform': 'android', 'build': '100000'})
    data.update(_spmid_payload('333.999'))
    return CLICK_H5, _m_headers(APP_TV_UA, bv, cookies), data


# ── 「其他」桶候选 ──────────────────────────────────────────
def _p_wx(info, bv, cookies):
    """微信内置浏览器（第三方 WebView）。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_H5, _m_headers(WECHAT_UA, bv, cookies), data


def _p_wx_mp(info, bv, cookies):
    """微信小程序：referer 走 servicewechat.com。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_H5, _m_headers(WECHAT_UA, bv, cookies, MP_REFERER), data


def _p_qqb(info, bv, cookies):
    """QQ/UC 等第三方手机浏览器。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_H5, _m_headers(QQBROWSER_UA, bv, cookies), data


def _p_pad(info, bv, cookies):
    """iPad Safari。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_H5, _m_headers(PAD_UA, bv, cookies), data


def _p_harmony(info, bv, cookies):
    """鸿蒙 ArkWeb。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_H5, _m_headers(HARMONY_UA, bv, cookies), data


def _p_click_root(info, bv, cookies):
    """无后缀端点 /click-interface/click。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_ROOT, _m_headers(IPHONE_UA, bv, cookies), data


def _p_wx_pc(info, bv, cookies):
    """PC 端点 + 微信 UA：判定「桶」由端点还是 UA 决定。"""
    data = _base_payload(info, bv)
    data.update({'jsonp': 'jsonp'})
    return CLICK_WEB, _web_headers(WECHAT_UA, bv, cookies), data


def _p_qqb_pc(info, bv, cookies):
    """PC 端点 + QQ 浏览器 UA。"""
    data = _base_payload(info, bv)
    data.update({'jsonp': 'jsonp'})
    return CLICK_WEB, _web_headers(QQBROWSER_UA, bv, cookies), data


def _p_weibo(info, bv, cookies):
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_H5, _m_headers(WEIBO_UA, bv, cookies), data


def _p_aweme(info, bv, cookies):
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload('333.401'))
    return CLICK_H5, _m_headers(AWEME_UA, bv, cookies), data


def _h5_ua(ua, info, bv, cookies, spmid='333.401'):
    """移动端点 + 任意 UA（批量筛 UA 白名单用）。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now})
    data.update(_spmid_payload(spmid))
    return CLICK_H5, _m_headers(ua, bv, cookies), data


def _p_xhs(info, bv, cookies):
    return _h5_ua(XHS_UA, info, bv, cookies)


def _p_kuaishou(info, bv, cookies):
    return _h5_ua(KUAISHOU_UA, info, bv, cookies)


def _p_alipay(info, bv, cookies):
    return _h5_ua(ALIPAY_UA, info, bv, cookies)


def _p_quark(info, bv, cookies):
    return _h5_ua(QUARK_UA, info, bv, cookies)


def _p_uc(info, bv, cookies):
    return _h5_ua(UC_UA, info, bv, cookies)


def _p_mqq(info, bv, cookies):
    return _h5_ua(MQQ_UA, info, bv, cookies)


def _p_tvbox(info, bv, cookies):
    """TV 盒子 UA 走移动端点（TV 桶候选二）。"""
    return _h5_ua(TVBOX_UA, info, bv, cookies, '333.999')


def _p_ott(info, bv, cookies):
    """OTT / TV 专用端点（TV 桶候选）。"""
    now = _now()
    data = _base_payload(info, bv)
    data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                 'ftime': now, 'stime': now,
                 'mobi_app': 'ott', 'platform': 'ott'})
    data.update(_spmid_payload('333.999'))
    return CLICK_OTT, _m_headers(APP_TV_UA, bv, cookies), data


def _hb_payload(info, bv):
    """网页播放器心跳请求体（匿名：mid/csrf 为空）。

    字段取自网页播放器 web/heartbeat：played_time/realtime/start_ts 构成一次
    「播到 dur 秒」的完整观看，服务端据此判有效播放。
    """
    dur = int(info.get('duration') or 0)
    return {
        'aid': info['aid'],
        'cid': info['cid'],
        'bvid': bv,
        'mid': '',
        'csrf': '',
        'played_time': dur,
        'realtime': dur,
        'start_ts': _now(),
        'type': '3',
        'sub_type': '0',
        'dt': '2',
        'play_type': '0',
        'video_duration': dur,
        'last_play_progress_time': dur,
        'max_play_progress_time': dur,
        'outer': '0',
        'spmid': '333.788.recommend_more_video.0',
        'from_spmid': '333.788',
        'extra': json.dumps({'scene': '', 'index': 0}, separators=(',', ':')),
        'session': ''.join(random.choices('0123456789abcdef', k=32)),
        'is_auto': '0',
    }


def _p_heartbeat(info, bv, cookies):
    """网页播放器心跳（独立计数链路，非 click 家族）。"""
    return HEARTBEAT, _web_headers(DESKTOP_UA, bv, cookies), _hb_payload(info, bv)


def _buvid_of(cookies):
    for part in cookies.split(';'):
        k, _, v = part.strip().partition('=')
        if k == 'buvid3':
            return v
    return ''


def _p_hb_mobile(info, bv, cookies):
    """APP 心跳 /x/report/heartbeat/mobile（TV/车机 APK 内挖出，需真 sign）。

    实测：缺 sign 一律 -400；android / android_hd / 783bbb / iphone 四组
    appkey-appsec 真 sign 均 code=0（TV 两组签名反而 -3 / -404）。
    """
    now = _now()
    data = _base_payload(info, bv)
    data.update(_hb_payload(info, bv))
    # extra 字段不进服务端签名（带上会 -3 签名不匹配），必须剔除
    data.pop('extra', None)
    ak, sec = APPKEYS['android']
    data.update({'appkey': ak, 'ts': now, 'mobi_app': 'android',
                 'platform': 'android', 'build': '7660300'})
    data['sign'] = _sign(data, sec)
    headers = _m_headers(APP_ANDROID_UA, bv, cookies)
    headers['Buvid'] = _buvid_of(cookies)
    return HB_MOBILE, headers, data


def _p_car_history(info, bv, cookies):
    """车机/云视听历史上报 app.bilibili.com/x/car/history/report。

    实测：无 buvid 时 90006「缺少buvid或者mid~」；带 Buvid 头 + buvid3 cookie
    且带 aid/cid/bvid 时 code=0，匿名可用。
    """
    data = _base_payload(info, bv)
    data.update({'mid': '', 'progress': '-1', 'type': '1', 'sub_type': '0'})
    headers = _m_headers(APP_TV_UA, bv, cookies)
    headers['Buvid'] = _buvid_of(cookies)
    return CAR_HISTORY, headers, data


# 扩展 UA 扫描：桌面/平板/鸿蒙/其他 App 内置浏览器
EXTRA_UA = {
    'mac_safari': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
                  '(KHTML, like Gecko) Version/17.4 Safari/605.1.15',
    'ipad_17': 'Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 '
               '(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    'harmony_next': 'Mozilla/5.0 (Phone; OpenHarmony 5.0) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36 ArkWeb/4.1.6.1 Mobile',
    'huawei_browser': 'Mozilla/5.0 (Linux; Android 12; ELS-AN00; HMSCore 6.11.0.302) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.88 '
                      'HuaweiBrowser/14.0.3.312 Mobile Safari/537.36',
    'miui_browser': 'Mozilla/5.0 (Linux; U; Android 13; zh-cn; 2211133C Build/TKQ1.220905.001) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/109.0.5414.86 '
                    'MQQBrowser/13.3 Mobile Safari/537.36 XiaoMi/MiuiBrowser/17.3.9',
    'samsung_browser': 'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) SamsungBrowser/23.0 Chrome/115.0.0.0 '
                       'Mobile Safari/537.36',
    'dingtalk': 'Mozilla/5.0 (Linux; U; Android 13; zh-CN; Pixel 7 Build/TQ3A.230805.001) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 '
                'UWS/3.22.2.65 Mobile Safari/537.36 AliApp(DingTalk/7.5.0) '
                'com.alibaba.android.rimet/30351114 Channel/227200 language/zh-CN',
    'taobao': 'Mozilla/5.0 (Linux; U; Android 13; zh-CN; Pixel 7 Build/TQ3A.230805.001) '
              'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 '
              'UWS/3.22.2.65 Mobile Safari/537.36 AliApp(TB/10.30.0) WindVane/8.5.0 1080X2340',
    'baidu': 'Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
             'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
             'Mobile Safari/537.36 baiduboxapp/13.40.0.10',
    'toutiao': 'Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
               'Mobile Safari/537.36 NewsArticle/9.9.9',
    'zhihu': 'Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230805.001; wv) '
             'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 '
             'Mobile Safari/537.36 Zhihu/10.53.0',
    'edge_mobile': 'Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36 '
                   'EdgA/120.0.2210.126',
    'firefox_mobile': 'Mozilla/5.0 (Android 13; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0',
}


def _mk_h5(ua):
    def _p(info, bv, cookies):
        return _h5_ua(ua, info, bv, cookies)
    return _p


# 端点路径变体：官方口径还有 TV / 移动 / 其他 三个桶，
# 试 /click-interface/click/{seg}/h5 是否存在独立计数链路。
# 统一配微信 UA（已验证可计数），这样路径通就能看到增量。
PATH_SEGS = ('tv', 'mobile', 'app', 'pad', 'ott', 'pc', 'm', 'android', 'other')


def _p_route(tpl, seg):
    """按模板拼端点，段名当桶选择器试。tpl 例：'click/{seg}/h5'、'click/web/{seg}'。"""
    url = 'http://api.bilibili.com/x/click-interface/' + tpl.format(seg=seg)

    def _p(info, bv, cookies):
        now = _now()
        data = _base_payload(info, bv)
        data.update({'mid': '', 'lv': '0', 'did': booster.generate_did(),
                     'ftime': now, 'stime': now})
        data.update(_spmid_payload('333.401'))
        return url, _m_headers(WECHAT_UA, bv, cookies), data

    return _p


# 段位形态矩阵：段名可能不在原来 web 的位置，六种常见摆法各试一遍。
PATH_ROUTES = (
    ('seg_h5', 'click/{seg}/h5'),
    ('seg', 'click/{seg}'),
    ('root_seg_h5', '{seg}/h5'),
    ('web_seg', 'click/web/{seg}'),
    ('h5_seg', 'click/h5/{seg}'),
    ('seg_web', 'click/{seg}/web'),
)

# 裸段 /click/{seg}（不带 /h5）单独扫一遍，段名放宽到所有客户端类型。
BARE_SEGS = ('tv', 'mobile', 'app', 'pad', 'ott', 'pc', 'm', 'android',
             'ios', 'iphone', 'ipad', 'web', 'h5', 'other', 'tvbox', 'xbox')


PROFILES = {
    'pc': ('网页端对照（应落 PC 桶）', _p_pc),
    'h5': ('移动端对照（应落 H5 桶）', _p_h5),
    'h5_spmid337': ('移动端点 + spmid 333.337', _p_h5_337),
    'h5_spmid788': ('移动端点 + spmid 333.788', _p_h5_788),
    'h5_spmid999': ('移动端点 + spmid 333.999', _p_h5_999),
    'app_android': ('移动端点 + BiliApp/Android 指纹', _p_app),
    'app_android2': ('android2 端点 + BiliApp 指纹（无 sign）', _p_app_android2),
    'android2_sign': ('android2 + 安卓 appkey 真 sign', _p_android2_sign),
    'android2_sign_bili': ('android2 + 安卓(783bbb) appkey 真 sign', _p_android2_sign_bili),
    'android2_sign_tv': ('android2 + TV appkey 真 sign', _p_android2_sign_tv),
    'android2_sign_ios': ('android2 + iOS appkey 真 sign', _p_android2_sign_ios),
    'tv': ('移动端点 + os/tv 指纹（已判死，见文件头）', _p_tv),
    'wx': ('移动端点 + 微信内置浏览器 UA（其他桶候选）', _p_wx),
    'wx_mp': ('移动端点 + 微信小程序 referer（其他桶候选）', _p_wx_mp),
    'qqb': ('移动端点 + QQ 浏览器 UA（其他桶候选）', _p_qqb),
    'pad': ('移动端点 + iPad Safari UA（其他桶候选）', _p_pad),
    'harmony': ('移动端点 + 鸿蒙 ArkWeb UA（其他桶候选）', _p_harmony),
    'click_root': ('无后缀端点 /click-interface/click（其他桶候选）', _p_click_root),
    'wx_pc': ('PC 端点 + 微信 UA（桶归属：端点 vs UA 判别）', _p_wx_pc),
    'qqb_pc': ('PC 端点 + QQ 浏览器 UA（桶归属判别）', _p_qqb_pc),
    'weibo': ('移动端点 + 微博内置浏览器 UA（其他桶候选）', _p_weibo),
    'aweme': ('移动端点 + 抖音内置浏览器 UA（其他桶候选）', _p_aweme),
    'ott': ('OTT 端点 /click-interface/click/ott（TV 桶候选）', _p_ott),
    'xhs': ('移动端点 + 小红书 UA（其他桶候选）', _p_xhs),
    'kuaishou': ('移动端点 + 快手 UA（其他桶候选）', _p_kuaishou),
    'alipay': ('移动端点 + 支付宝 UA（其他桶候选）', _p_alipay),
    'quark': ('移动端点 + 夸克 UA（其他桶候选）', _p_quark),
    'uc': ('移动端点 + UC 浏览器 UA（其他桶候选）', _p_uc),
    'mqq': ('移动端点 + 手机QQ 内置浏览器 UA（其他桶候选）', _p_mqq),
    'tvbox': ('移动端点 + 小米电视 UA（TV 桶候选二，已判死）', _p_tvbox),
    'heartbeat': ('网页播放器心跳 web/heartbeat（独立通道）', _p_heartbeat),
    'hb_mobile': ('APP 心跳 /x/report/heartbeat/mobile + 安卓真 sign（移动桶候选）', _p_hb_mobile),
    'car_history': ('车机历史 /x/car/history/report（TV 桶候选）', _p_car_history),
}

for _name, _ua in EXTRA_UA.items():
    PROFILES[_name] = (f'移动端点 + {_name} UA（扩展扫描）', _mk_h5(_ua))

for _seg in PATH_SEGS:
    PROFILES[f'path_{_seg}'] = (f'/click/{_seg}/h5 + 微信 UA（路径变体）', _p_route('click/{seg}/h5', _seg))

# 段位形态矩阵：只跑 tv / mobile / other 三个桶名 × 六种摆法。
for _seg in ('tv', 'mobile', 'other'):
    for _tag, _tpl in PATH_ROUTES:
        PROFILES[f'route_{_tag}_{_seg}'] = (f'/{_tpl.format(seg=_seg)} + 微信 UA（段位形态）',
                                            _p_route(_tpl, _seg))

for _seg in BARE_SEGS:
    PROFILES[f'bare_{_seg}'] = (f'/click/{_seg} 裸段（无 /h5）+ 微信 UA', _p_route('click/{seg}', _seg))


def line(ch='-', n=78):
    print(ch * n)


def click_once(proxy, info, bv, profile):
    try:
        cookies = booster.build_random_cookies()
        url, headers, data = PROFILES[profile][1](info, bv, cookies)
        r = requests.post(url,
                          proxies={'http': f'http://{proxy}'},
                          headers=headers,
                          timeout=booster.timeout,
                          data=data)
        try:
            return f'code={r.json().get("code")}'
        except Exception:
            return f'http{r.status_code}'
    except Exception:
        return 'exc'


def _probe_pool(bv, need):
    """抓一次代理池并探活，凑够 need 个存活即提前收工（分块提交便于中断）。"""
    print('拉取代理池 ...')
    proxies = booster.get_total_proxies(print)
    if not proxies:
        return []
    print(f'共 {len(proxies)} 个，开始探活（目标 {need} 个存活）...')
    random.shuffle(proxies)
    cache, alive, lock = {}, [], threading.Lock()
    stop = threading.Event()

    def probe(p):
        if stop.is_set():
            return
        ok, _ = booster.probe_cached(p, bv, cache)
        if ok:
            with lock:
                alive.append(p)
                if len(alive) >= need:
                    stop.set()

    chunk = 600
    for i in range(0, len(proxies), chunk):
        if stop.is_set():
            break
        part = proxies[i:i + chunk]
        with ThreadPoolExecutor(max_workers=max(1, min(booster.thread_num, len(part)))) as pool:
            list(pool.map(probe, part))
    print(f'存活 {len(alive)} 个（已探测 {min(len(proxies), i + chunk)} / {len(proxies)}）')
    return alive


def batch(plan, per_video):
    """一次抓池、一次探活，按计划分发给多条稿件。"""
    print('实验计划：')
    infos = {}
    for bv, profile in plan:
        info = booster.fetch_video_info(bv)
        infos[bv] = info
        print(f'  {bv}  {profile:<14} views={info["stat"]["view"]:<6} '
              f'{PROFILES[profile][0]}  | {info.get("title", "")[:24]}')

    need = per_video * len(plan)
    alive = _probe_pool(plan[0][0], need)
    if not alive:
        print('[X] 没有可用代理')
        return
    if len(alive) < need:
        per_video = max(1, len(alive) // len(plan))
        print(f'存活不足，每组降为 {per_video} 次')

    slices = [alive[i * per_video:(i + 1) * per_video] for i in range(len(plan))]
    results = []
    for (bv, profile), ps in zip(plan, slices):
        line('=')
        print(f'[{profile}] {bv}  发送 {len(ps)} 次')
        codes, lock = Counter(), threading.Lock()

        def worker(proxy, bv=bv, profile=profile):
            res = click_once(proxy, infos[bv], bv, profile)
            with lock:
                codes[res] += 1

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(ps)))) as pool:
            list(pool.map(worker, ps))
        ok = codes.get('code=0', 0)
        print(f'  返回 {dict(codes)}   code=0 {ok} 次  耗时 {int(time.time() - t0)}s')
        results.append((bv, profile, len(ps), ok))

    line('=')
    print('汇总（播放量受延迟结算影响，此处仅记录基线，判定看稿件后台「播放来源」）')
    for bv, profile, n, ok in results:
        cur = booster.fetch_video_info(bv)['stat']['view']
        print(f'  {profile:<14} {bv}  发送 {n:<3} code=0 {ok:<3} '
              f'基线 {infos[bv]["stat"]["view"]:<5} 当前 {cur}')
    print('\n请等待 6 小时以上，再看每条稿件的播放来源桶分布。')


def signcheck(bv, profiles):
    """直连（不走代理）验证 sign 是否被接受，只看 code，不看计数。"""
    info = booster.fetch_video_info(bv)
    print(f'稿件 {info.get("title", "")[:30]}  views={info["stat"]["view"]}')
    for p in profiles:
        cookies = booster.build_random_cookies()
        url, headers, data = PROFILES[p][1](info, bv, cookies)
        try:
            r = requests.post(url, headers=headers, timeout=booster.timeout, data=data)
            body = r.text[:160].replace('\n', ' ')
            print(f'  {p:<18} http{r.status_code}  {body}')
        except Exception as e:
            print(f'  {p:<18} ERR {e}')


def run(bv, profile, count):
    info = booster.fetch_video_info(bv)
    print(f'稿件 {info.get("title")}')
    print(f'  aid={info["aid"]} cid={info["cid"]} P数={len(info.get("pages") or [])} '
          f'views={info["stat"]["view"]}')
    print(f'指纹 {profile} — {PROFILES[profile][0]}')
    print(f'目标 {count} 次有效点击\n（打完请等 6 小时以上，再看这条稿件的「播放来源」形状）')

    line('=')
    print('拉取代理池 ...')
    proxies = booster.get_total_proxies(print)
    if not proxies:
        print('[X] 代理池为空')
        return
    print(f'共 {len(proxies)} 个，开始探活 ...')
    cache, alive, lock = {}, [], threading.Lock()

    def probe(p):
        ok, _ = booster.probe_cached(p, bv, cache)
        if ok:
            with lock:
                alive.append(p)

    with ThreadPoolExecutor(max_workers=max(1, min(booster.thread_num, len(proxies)))) as pool:
        list(pool.map(probe, proxies))
    print(f'存活 {len(alive)}/{len(proxies)}')
    random.shuffle(alive)
    if not alive:
        return

    codes = Counter()
    done = [0]
    lock2 = threading.Lock()
    target = count

    def worker(proxy):
        with lock2:
            if done[0] >= target:
                return
            done[0] += 1
        res = click_once(proxy, info, bv, profile)
        with lock2:
            codes[res] += 1

    line('=')
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, min(WORKERS, len(alive)))) as pool:
        list(pool.map(worker, alive[:target]))

    ok = sum(v for k, v in codes.items() if k == 'code=0')
    print(f'发送 {min(target, len(alive))} 次，耗时 {int(time.time() - t0)}s')
    print(f'返回 {dict(codes)}   code=0 共 {ok} 次')

    # 只做一次粗看，不做归因：结算延迟 5–30 分钟，短窗读数无意义
    after = booster.fetch_video_info(bv)['stat']['view']
    print(f'当前播放量 {info["stat"]["view"]} -> {after}（仅参考，不作判定）')


def scan(limit):
    """扫单P低播放稿件，作为平行对照实验的载体。

    newlist 响应自带 videos / duration / stat.view，直接就地过滤，
    不再逐条调 view 接口（900 条候选逐条打 view 极易触发 -412 风控）。
    """
    found, seen = [], set()
    rid_list = [1, 3, 4, 5, 36, 160]
    s = requests.Session()
    s.headers.update({'User-Agent': DESKTOP_UA,
                      'Referer': 'https://www.bilibili.com/'})
    for rid in rid_list:
        for pn in range(1, 4):
            try:
                r = s.get('http://api.bilibili.com/x/web-interface/newlist',
                          params={'rid': rid, 'pn': pn, 'ps': 20}, timeout=10)
                payload = r.json()
                if payload.get('code') != 0:
                    print(f'  rid={rid} pn={pn} code={payload.get("code")} 跳过')
                    continue
                archives = payload.get('data', {}).get('archives') or []
            except Exception as e:
                print(f'  rid={rid} pn={pn} 失败 {e}')
                continue
            for a in archives:
                bv = a.get('bvid')
                if not bv or bv in seen:
                    continue
                seen.add(bv)
                if a.get('videos') != 1:
                    continue
                dur = a.get('duration') or 0
                view = (a.get('stat') or {}).get('view') or 0
                if view >= 3000 or dur < 60:
                    continue
                owner = (a.get('owner') or {}).get('name', '?')
                found.append((bv, view, dur, owner, (a.get('title') or '')[:34]))
            time.sleep(0.25)

    found.sort(key=lambda x: x[1])
    line('=')
    print(f'可用载体 {len(found)} 条（单P / view<3000 / 时长≥60s）：')
    print(f'  {"BV号":<14} {"播放":>5} {"时长":>6}  {"UP主":<16} 标题')
    for bv, view, dur, owner, title in found[:limit]:
        print(f'  {bv:<14} {view:>5} {dur:>5}s  {owner:<16} {title}')
    print(f'\n共 {len(found)} 条。建议每种指纹分配一条，且尽量来自不同 UP 主（避免账号级风控互相污染）。')


def main():
    ap = argparse.ArgumentParser(description='播放来源分桶实验探针')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_run = sub.add_parser('run', help='对稿件打满一种指纹')
    p_run.add_argument('bvid')
    p_run.add_argument('--profile', required=True, choices=sorted(PROFILES))
    p_run.add_argument('--count', type=int, default=60)
    p_run.add_argument('--dry-run', action='store_true', help='只打印一次请求，不发')

    p_scan = sub.add_parser('scan', help='扫单P低播放稿件')
    p_scan.add_argument('--limit', type=int, default=20)

    p_batch = sub.add_parser('batch', help='平行对照：一稿一指纹，一次跑完')
    p_batch.add_argument('--plan', required=True,
                         help='形如 BV1xx:pc,BV2yy:h5（逗号分隔，一稿一指纹）')
    p_batch.add_argument('--per-video', type=int, default=60)

    p_sc = sub.add_parser('signcheck', help='直连验证 sign 是否被接受')
    p_sc.add_argument('bvid')
    p_sc.add_argument('--profiles', default='android2_sign,android2_sign_tv,android2_sign_ios')

    sub.add_parser('profiles', help='列出所有指纹')
    args = ap.parse_args()

    if args.cmd == 'batch':
        plan = []
        for item in args.plan.split(','):
            item = item.strip()
            if not item:
                continue
            bv, _, profile = item.partition(':')
            if profile not in PROFILES:
                print(f'[X] 未知指纹 {profile}（可选：{", ".join(sorted(PROFILES))}）')
                return
            plan.append((bv.strip(), profile))
        if plan:
            batch(plan, args.per_video)
        return

    if args.cmd == 'signcheck':
        plist = [x.strip() for x in args.profiles.split(',') if x.strip()]
        bad = [x for x in plist if x not in PROFILES]
        if bad:
            print(f'[X] 未知指纹 {bad}')
            return
        signcheck(args.bvid, plist)
        return

    if args.cmd == 'profiles':
        for name, (desc, _) in PROFILES.items():
            print(f'  {name:<15} {desc}')
        return
    if args.cmd == 'scan':
        scan(args.limit)
        return

    if args.dry_run:
        info = booster.fetch_video_info(args.bvid)
        url, headers, data = PROFILES[args.profile][1](info, args.bvid,
                                                       booster.build_random_cookies())
        print(f'URL: {url}')
        for k, v in headers.items():
            print(f'  {k}: {v}')
        for k, v in data.items():
            print(f'  data.{k} = {v}')
        return

    run(args.bvid, args.profile, args.count)


if __name__ == '__main__':
    main()
