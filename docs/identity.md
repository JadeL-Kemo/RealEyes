# RealEyes — Identity & presence（非人脸基线）

> 设计目标：在不看脸的前提下，判断“我是否在电脑前 / 在做什么”，并为未来的身体 Re-ID 与强认证留出接口。

## 当前实现（MVP，`src/identity.py`）

输入：

- `GetLastInputInfo` → 键鼠空闲毫秒（Windows 全局，不读按键内容）
- `GetForegroundWindow` → 前台窗口标题；应用名取标题最后一个 ` - / — / – ` 之后的部分

输出 `IdentityTracker.snapshot()`：

```json
{
  "label": "owner",
  "presence": "at_desk | recent | away",
  "confidence": 0.9,
  "idle_s": 12,
  "foreground": "DeepSeek — Mozilla Firefox",
  "app": "Mozilla Firefox",
  "basis": "input_idle+foreground_window"
}
```

阈值（config `identity`）：`at_desk_idle_s`（默认 60s）、`away_idle_s`（默认 180s，可配）；置信度是启发式，不是生物核验。

服务侧：`_identity_loop` 每 `interval_s` 采样；presence 或前台应用变化时写 `identity_state` 事件（`no_vlm`，不调用模型、不通知），另每小时至少写一条“在场心跳”。

## 为什么不用人脸（当前阶段）

- 用户要求：日常粗粒度身份用**非人脸**特征（体型、衣着、键鼠活动、前台应用、时段），人脸只在强认证动作时使用（且作为独立插件）
- 摄像头可能被遮挡/关闭；键鼠+前台窗口在摄像头不可用时仍然可靠
- 隐私与合规：不做持续人脸识别，避免敏感生物数据落盘

## 预留接口

- `BodyReID`（`identity.py`）：未来接 OSNet / ONNX 身体特征，输入 BGR 帧，输出 embedding/候选 ID；需要目标检测先裁剪人形区域
- 强认证插件：人脸解锁、手机同账号在场、硬件密钥；只在“执行敏感动作”时触发一次
- 时段先验：作息直方图（M2 digests 可提供），用于降低“深夜活动”误报

## 与监控规则的联动

- `at_desk`（键鼠活跃）：摄像头运动只安静记录，不做 VLM（省算力）
- `away`：运动阈值降低、大运动直接触发 L1；可作为“防入室”模式的开关
- `recent`：过渡态，按 `away` 处理但降低通知优先级
