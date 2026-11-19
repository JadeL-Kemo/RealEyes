# RealEyes — Plan

> 2026-11-19 起：把 `F:\scripts\realeyes` 单文件 MVP 升级为正式项目。用户指示：先做可运行的垂直切片，不要过度设计。

## 当前状态（v0.2 垂直切片，2026-11-19）

已完成并在本机验证：

- 项目结构：`H:\AgentWorkspace\RealEyes\{src,docs,scripts}`
- 信号源：屏幕区域、窗口捕捉（Firefox 直播窗口实测）、摄像头（DirectShow，当前镜头全黑）、本地视频、ffmpeg 网络流/ B 站直播、手机 ADB 截图接口
- L0/L1：运动、画面全黑、屏幕黑屏/冻结；L1 本地 qwen3.5-9b 关键帧 JSON 判断；severity floor；no_vlm 规则事件
- 并行注意力：`attention.workers` + `attention.model_pool` 轮转；网关 `--parallel N`（qwen3.5-9b: 3）
- 记忆：M0 JSONL / M1 SQLite / M2 digest / M3 recall（`--recall` 同时搜 digest）
- 身份基线：input idle + 前台窗口（`identity.py`，无图像）
- 文件监听：`filewatch.py`（新增/修改，首扫只建快照）
- 音频：soundcard loopback 响度事件（`audio_loud`）
- 监督/自启：`supervise_realeyes.ps1`（崩溃 <20s 拉起）、HKCU Run
- 自测：`--selftest` 8/8 PASS

## 阶段

### M0 — 垂直切片（本轮，进行中）

- [x] 代码与配置模块化（identity/filewatch/memory/phone + 主服务接线）
- [x] `src/config.yaml` 增加 identity/filewatch/source 语法
- [x] 文档：SPEC.md / PLAN.md / identity.md / memory.md / README
- [ ] 直播/窗口源短跑验收（motion→L1 verdict→关键帧→recall）
- [ ] 把项目代码与 F: 部署副本对齐（切换方式见下）

验收命令（在 `H:\AgentWorkspace\RealEyes\src`）：

```
python realeyes.py --selftest
python realeyes.py --check
python realeyes.py --config config.live.yaml --duration 75
python realeyes.py --recall 关键词 --hours 3
```

### M1 — 部署切换与稳定性

- 用项目代码替换 `F:\scripts\realeyes\realeyes.py`（先停 supervisor + 服务，保留备份）
- 部署配置单独一份（`F:\scripts\realeyes\config.yaml`），与项目内默认区分
- 24h 观察：事件量、CPU/内存、GPU 占用、digest 质量
- 崩溃恢复与磁盘保留策略回归测试

### M2 — 身份与记忆增强

- OSNet/ONNX 身体 Re-ID（非人脸）接入 `identity.BodyReID`，输出人物候选与置信度
- 记忆 M2 摘要质量（用 9B/27B 生成小时/日摘要），M3 语义检索（可先加 SQLite FTS）
- 强认证插件接缝（人脸/手机解锁/硬件密钥，仅特定动作触发）

### M3 — 手机与硬件

- ADB 设备授权后启用 `camera.source: phone`；考虑 scrcpy 低延迟流
- 机械臂/云台 + 麦克风模拟眼耳；人脸快速追踪（需先解决摄像头可用性与隐私边界）
- 云端 tier（L3）预算闸门：警告线、硬上限、通知阈值、短信/邮件接口

## 部署关系

- 权威源码：`H:\AgentWorkspace\RealEyes`
- 运行中的旧副本：`F:\scripts\realeyes`（数据 `F:\realeyes`，自启用 supervisor）
- 切换前不要在 F: 与 H: 同时跑同一 `data_root`（避免双写）。

## 风险

- 摄像头镜头当前全黑（物理遮挡/隐私开关），真实视觉链路依赖用户处理
- 35B/27B 与 ComfyUI 争显存：网关会 503，RealEyes 侧会降级/跳过
- 直播来源只用于验证，正式使用需尊重平台条款
- 单用户机器假设：身份基线是“在场判断”，不是生物识别
