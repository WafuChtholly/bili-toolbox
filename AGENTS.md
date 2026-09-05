# bili-toolbox 项目说明

B站工具箱：单文件 Flask 应用（`app.py`，全部路由与调度逻辑）+ `bili-*` 功能子模块（booster / auto / player / redpocket / cat / medal / monitor）+ 单页前端（`templates/index.html`）。通过 pyappify 打包分发（配置见 `pyappify.yml`），用户机器上以 git 拉取方式更新。

## 运行与测试

- 启动：`python app.py`，端口 5678。**必须保持单进程**：禁止 `debug=True` / reloader（会产生父子双 python 进程，打包场景下关窗只杀父进程，真实服务子进程变孤儿继续跑定时任务——v1.4.0 之前的实际生产事故）。
- 回归测试：`python _test_schedule_stop.py`（定时任务停止竞态模拟，全部 PASS 为正常）。
- 生产自诊断：`python tools/diagnose_booster.py`（只读脚本，排查「关闭定时任务后仍自动刷量」）。

## 架构要点

- 所有定时任务状态只存内存（`_booster_schedule` 等），重启即清零，不跨重启恢复。
- booster 任务只有三个创建来源：手动 `/api/booster/run`、定时循环 `_booster_schedule_loop`、手动执行一次 `run-once`；webhook 只接收 BV 号，需前端确认后才建任务。
- 子任务通过共享 `stop_event` + 信号量（并发 3）排队；停止定时时必须同步取消同 event 的子任务（含排队中的），否则排队任务拿到槽位后会照常执行。
- print 输出走 `_SafeStream`（Windows 控制台编码保护）；用户数据在 `data/`（已 gitignore）。

## 发布约定

- tag 命名 `vX.Y.Z`；双远端：`origin`(GitHub) + `cnb`，main 和 tag 都要推两边。
- 生产环境排查入口：`.zcode/commands/booster-diagnose.md`（斜杠命令 `/booster-diagnose`）。
