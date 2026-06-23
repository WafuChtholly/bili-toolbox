<div align="center">

# Bilibili Toolbox

**B站工具箱** — 一站式 B站自动化 WebUI

![Python](https://img.shields.io/badge/Python-3.13-blue?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0+-green?logo=flask&logoColor=white)
![License](https://img.shields.io/github/license/WafuChtholly/bili-toolbox)
![Release](https://img.shields.io/github/v/release/WafuChtholly/bili-toolbox?color=f472b6&label=Latest)

![Preview](icons/icon.png)

</div>

---

## 功能模块

| 模块 | 说明 | 模式 |
|------|------|------|
| **自动互动** | 关注列表动态检测，自动三连（点赞/投币/收藏）、互关检测、智能评论 | WebUI |
| **播放量提升 (代理池)** | 通过代理 IP 池高并发刷播放量，理论上限 200 并发 | WebUI |
| **播放量提升 (浏览器模拟)** | Playwright 驱动真实浏览器模拟播放，速度较慢但更真实 | WebUI |
| **直播间红包助手** | 监听指定直播间，自动抢发红包弹幕 | WebUI |

## 快速开始

### 方式一：下载打包版本（推荐）

前往 [Releases](https://github.com/WafuChtholly/bili-toolbox/releases) 下载最新版本：

- **Online** 版本（推荐） — 体积极小，首次启动自动下载 Python 环境及依赖
- **China** 版本 — 国内用户，开箱即用，使用 cnb.cool 镜像源
- **Global** 版本 — 海外用户，开箱即用，使用 GitHub 源

双击运行即可，首次启动会自动安装 Playwright Chromium。

### 方式二：源码运行

```bash
git clone https://github.com/WafuChtholly/bili-toolbox.git
cd bili-toolbox
pip install -r requirements.txt
python app.py
```

启动后访问 **http://localhost:5678**

> 播放量提升（浏览器模拟）功能需要额外安装 Playwright 浏览器后实现

## 使用说明

### 自动互动

1. 在 WebUI 中扫描二维码登录 B 站账号
2. 选择**动态互动**（关注列表新动态）或**历史投稿互动**（指定 UP 主历史视频）
3. 配置互动行为：三连、评论、播放等
4. 点击运行，日志实时输出

支持**定时任务**模式，可设置间隔自动循环执行。

### 播放量提升

**代理池模式：**
- 需要配置代理 IP，适合大批量快速提升
- 上限200播

**浏览器模拟模式：**
- 使用 Playwright 打开真实浏览器页面
- 模拟用户观看行为，速度约 30 播/小时
- 适合少量视频的精准播放

### 直播间红包助手

- 配置目标直播间 ID 和红包 ID
- 自动监听直播间弹幕，触发时发送预设消息

## 项目结构

```
bili-toolbox/
├── app.py                  # Flask WebUI 主入口
├── pyappify.yml            # PyAppify 打包配置
├── requirements.txt        # Python 依赖
├── templates/
│   └── index.html          # 前端页面（单文件 SPA）
├── bili-auto/              # 自动互动模块
│   ├── core.py             # 核心逻辑：动态检测/三连/评论
│   └── config.yaml         # 互动配置
├── bili-booster/           # 代理池播放量提升
│   └── booster.py          # 代理池并发请求
├── bili-player/            # 浏览器模拟播放
│   ├── player.py           # Playwright 模拟播放
│   └── config.yaml         # 播放配置（cookie 等）
├── bili-redpocket/         # 直播间红包助手
│   ├── auto_send_red_pocket.py
│   ├── config.py
│   └── blivelisten/        # B站直播弹幕监听库
├── icons/                  # 应用图标
└── data/                   # 运行时数据（凭证、缓存等）
```

## 在线更新

打包版本支持在线更新。启动时会检查 GitHub / cnb.cool 上的最新 Release，发现新版本后可一键升级。

## 开发

```bash
# 安装依赖
pip install -r requirements.txt

# 启动开发服务器
python app.py
```

## License

[MIT](LICENSE)
