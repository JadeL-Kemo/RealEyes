# RealEyes src

主服务与信号源模块；项目说明见上一级 [`../README.md`](../README.md)，规格与计划见 [`../docs/`](../docs/)。

| 文件 | 作用 |
|---|---|
| `realeyes.py` | 主服务：采集线程、L0 规则、L1 本地 VLM、并行注意力、事件库、digest、通知、CLI（`--check/--once/--duration/--recall/--selftest`） |
| `identity.py` | 非人脸身份/在场基线（键鼠空闲 + 前台窗口）；OSNet BodyReID 预留 |
| `filewatch.py` | 文件监听信号源（新增/修改；首扫只建快照） |
| `memory.py` | M0/M1/M2/M3 记忆与 recall |
| `phone.py` | ADB 手机截图接口（需 USB 调试授权） |
| `config.yaml` | 默认配置（含各信号源语法与阈值） |
| `start_realeyes.ps1` / `stop_realeyes.ps1` / `supervise_realeyes.ps1` | 幂等启停、崩溃监督（登录自启可指向监督脚本） |

常用命令（本目录下）：

```
python realeyes.py --selftest
python realeyes.py --check
python realeyes.py --duration 60
python realeyes.py --recall 关键词 --hours 24
```

> `--recall` 读取配置里的 `service.data_root`；若要检索别的数据根，用 `--config` 指定对应配置文件。
