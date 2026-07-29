# CanvasForge

CanvasForge 是面向 AstrBot + NapCat QQ 的图片生成插件。它通过 Sub2API 的 OpenAI Images 兼容接口提供：

- 文本生成图片；
- 回复一条含图片的 QQ 消息后进行多图参考编辑；
- AstrBot LLM Tool 自然语言调用；
- `/canvasforge <提示词>` 命令调用；
- 带高级设置、最近图片缓存和版本更新入口的 AstrBot Plugin Page。

## 运行要求

- AstrBot `>= 4.26.6`
- NapCat / OneBot v11，消息格式必须为 `array`
- Python 依赖见 `requirements.txt`
- 可用的 Sub2API 站点地址与 API Key

NapCat `4.8.98` 已作为兼容样本；推荐使用 `4.18.13`。本插件不声明强制的 NapCat 最低版本。
当前仅支持 AstrBot 的 `aiocqhttp` 适配器，不支持 QQ 官方机器人适配器。

## 配置

安装并重载插件后，在 AstrBot 插件设置中填写：

1. `base_url`：Sub2API 站点地址；
2. `api_key`：Sub2API API Key。

其余模型、画质、超时、冷却、引用图限制与缓存数量，请在插件详情页打开“CanvasForge 控制台”设置。

> [!WARNING]
> AstrBot 当前会把插件配置中的 API Key 明文写入 `data/config/...json`。CanvasForge 不会在日志、错误消息或管理页 API 中回显 Key，但仍需限制配置文件的读取权限。

公网地址必须使用 HTTPS。本机、回环/内网地址及本地容器主机名可使用 HTTP。

使用 LLM Tool 时，AstrBot 自身的 `tool_call_timeout` 也会约束工具执行时间。若保留 CanvasForge 默认的 300 秒请求超时，请把对应聊天 Provider 的工具调用超时设置为不少于 300 秒；命令调用不受这项工具超时影响。

## 使用

### LLM Tool

启用工具 `canvasforge_generate_image` 后，用户可以直接用自然语言让当前 AI 生图。聊天 AI 自己决定如何编写和扩展提示词；CanvasForge 不会额外调用一次聊天模型。

### 命令

```text
/canvasforge 一只坐在窗边看雨的橘猫，日系动画背景
```

命令提示词会直接发送给图像接口，不经过聊天 AI 润色。

### 引用图编辑

回复一条含图片的 QQ 消息，再让 AI 生图或使用 `/canvasforge`。CanvasForge 只读取这次直接引用消息中的图片：

- 默认最多 3 张，可在管理页调整为 1–10 张；
- 保持原消息中的图片顺序；
- 超过数量限制或任意一张图片无效时，整次请求会在调用付费接口前终止；
- 不会读取当前消息附带的图片，也不会递归读取更早一层引用。

支持静态 PNG、JPEG、WebP。单张引用图固定不超过 15 MiB；其他总量、像素和边长限制可在管理页调整。

## 缓存与隐私

生成结果会在发送 QQ 前写入插件数据目录，默认保留最近 3 张。管理页可以预览、下载、删除或清空缓存。

缓存会保存来源 QQ/群聊 ID、可读名称和生成元数据，但不会保存：

- 用户或 AI 的完整提示词；
- 被引用的原始图片；
- API Key；
- Sub2API 原始响应；
- `revised_prompt` 或 usage。

把缓存数量设为 `0` 会停止新增缓存，但不会自动删除已有图片。

## 检查与安装更新

CanvasForge 控制台会检查
[`YuXya/astrbot_plugin_canvasforge`](https://github.com/YuXya/astrbot_plugin_canvasforge)
的最新正式 GitHub Release。发现更高版本后，管理员可以在确认版本变化后执行更新；插件会委托 AstrBot 核心下载、校验并重载自身。

- 仅识别 `vMAJOR.MINOR.PATCH` 格式的正式 Release，不安装 draft 或 prerelease；
- 检查和安装会锁定同一个 Git Commit，不会在确认后改为安装变化后的 `main`；
- 更新期间不会中断正在进行的付费生图；有生成任务时会直接拒绝本次更新；
- 控制台不会读取或转发 Dashboard Token，也不接受页面传入的仓库、下载地址、代理或 Commit；
- GitHub 连接失败时，请改用 AstrBot 插件管理中的“更新/重新安装”，并在那里选择需要的 GitHub 代理。

页内更新会先检测当前 AstrBot 是否提供兼容的内部插件更新能力；接口不存在或签名不兼容时会停止操作并提示使用原生更新入口。AstrBot 核心更新不是完整的事务式安装：如果新代码覆盖后发生依赖或重载失败，可能仍需从插件管理按 GitHub 地址重新安装。

CanvasForge 不保存历史更新包或旧版本备份。AstrBot 核心只复用一个固定名称的临时 ZIP，成功后删除；极端清理失败时，下一次更新也会覆盖同一路径，不会按版本无限累计。

CanvasForge 自身不会记录下载地址、Commit、插件路径或响应正文。AstrBot 核心更新器仍可能按其原生行为在 AstrBot 日志中记录固定 GitHub 归档地址和插件安装路径；其中不含 Sub2API Key 或 Dashboard 凭据，插件也不会通过全局日志修改去掩盖上游日志。

### 发布新版本

1. 同步修改 `metadata.yaml` 的 `version` 和 `main.py` 的 `PLUGIN_VERSION`；
2. 提交并推送同一份代码；
3. 从该提交创建同名的 `vX.Y.Z` Tag；
4. 使用该 Tag 创建正式 GitHub Release。

只有推送分支或创建 Tag、但没有创建正式 Release 时，控制台不会把它识别为可安装的新版本。
首个 `v0.1.0` 正式 Release 创建前会显示“仓库尚无正式 Release”；创建后会显示“已是最新版”，后续可从 `v0.1.1` 开始验证真实更新。

## 行为边界

- 全插件仅允许一个进行中的生成请求，不排队；
- 每位 QQ 用户默认成功生成后冷却 300 秒；
- 管理员绕过用户冷却，但不能绕过全局单任务限制；
- 失败不计入冷却；
- 付费生成或编辑请求不会自动重试；
- 当前版本仅实现 Sub2API GPT Images Provider，不实现 Gemini、流式图片、mask、多输出或本地关键词审核。

## 技术基线

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Pages](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)
- [Sub2API](https://github.com/Wei-Shaw/sub2api)

## License

AGPL-3.0
