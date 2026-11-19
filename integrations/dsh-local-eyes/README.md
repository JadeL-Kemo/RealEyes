# integrations/dsh-local-eyes

DSH 插件 `local_eyes`：把屏幕/本地图片/视频交给本机 Qwen VLM，**只返回文字**，不经过会话模型、不花 API token。RealEyes 使用同一个本地网关，这个插件是“手动看一帧”的轻量入口。

- 依赖：本机 8322 网关（见 `../local-llm-gateway`）
- 安装：把本目录放进 DSH profile 依赖（`pnpm add file:<path>`），并在 `dsh.profile.bundles` 登记 `dsh-local-eyes`，重启 DSH；详见 DSH 文档与项目根 README
- 默认：`qwen3.5-9b`，屏幕采集 1600×900；`model: "qwen3.8-27b"` 仅用于难图像（小截图容易误读）

| 文件 | 说明 |
|---|---|
| `src/index.js` | 工具注册：`local_eyes(prompt, image_path, source, video_path, seconds, model, max_tokens)` |
| `cordis.patch.yml` | 插件行 insert（id `dsh-local-eyes`） |
| `package.json` | `dsh.bundle.patch` 声明；依赖 `dsh-see-now` 的抓屏/编码 API |
