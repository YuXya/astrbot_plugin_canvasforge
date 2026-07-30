# CanvasForge

CanvasForge 是面向 AstrBot + NapCat QQ 的图片生成插件。它通过 Sub2API 的 OpenAI Images 兼容接口提供：

- 文本生成图片；
- 回复一条含图片的 QQ 消息后进行多图参考编辑；
- 按聊天 AI 的人物意图使用发送者、机器人或被 `@` 群友的 QQ 头像作为参考；
- AstrBot LLM Tool 自然语言调用；
- `/canvasforge <提示词>` 命令调用；
- 带高级设置和最近图片缓存的 AstrBot Plugin Page。

## 运行要求

- AstrBot `>= 4.26.7`
- NapCat / OneBot v11，消息格式必须为 `array`
- Python 依赖见 `requirements.txt`
- 可用的 Sub2API 站点地址与 API Key

NapCat `4.8.98` 已作为兼容样本；推荐使用 `4.18.13`。本插件不声明强制的 NapCat 最低版本。
当前仅支持 AstrBot 的 `aiocqhttp` 适配器，不支持 QQ 官方机器人适配器。
CanvasForge 使用 AstrBot 4.26.7 提供的异步配置保存接口，4.26.6 及更早版本不受支持。

## 配置

安装并重载插件后，在 AstrBot 插件设置中填写：

1. `base_url`：Sub2API 站点地址；
2. `api_key`：Sub2API API Key。

其余模型、画质、超时、冷却、引用图限制与缓存数量，请在插件详情页打开“CanvasForge 控制台”设置。

> [!WARNING]
> AstrBot 当前会把插件配置中的 API Key 明文写入 `data/config/...json`。CanvasForge 不会在日志、错误消息或管理页 API 中回显 Key，但仍需限制配置文件的读取权限。

公网地址必须使用 HTTPS。本机、回环/内网地址及本地容器主机名可使用 HTTP。

LLM Tool 和 `/canvasforge` 命令完成基础校验并占用唯一任务位后会立即返回，生图在插件后台继续执行；成功后图片由插件直接发送到 QQ。此流程不会等待第二次聊天模型调用。

## 使用

### LLM Tool

CanvasForge 提供两个职责分离的 LLM 工具，由当前聊天 AI 按任务选择：

- `canvasforge_text_to_image(prompt)`：仅用于从零创作的纯文生图，可以按用户要求设计外貌；如果当前消息直接回复了图片，插件会在付费请求前拒绝，并明确要求改用图生图工具。
- `canvasforge_image_to_image(prompt, avatar_targets)`：仅用于直接回复图片编辑或 QQ 人物头像参考；至少必须有一张回复图片或一个头像，没有参考图时会要求改用文生图工具。

聊天 AI 自己决定如何编写和扩展提示词；CanvasForge 不会额外调用一次聊天模型。人物头像和回复图片会直接作为同一次 Images 编辑请求的参考图片上传，不会先调用视觉聊天模型进行看图或转述。旧的组合工具 `canvasforge_generate_image` 已删除。

CanvasForge 不再限制同一条消息只能调用一次工具。失败后，当前 AI 可以依据错误原因再次尝试；若插件明确返回工具模式不匹配，也可以在文生图和图生图工具之间切换。一次失败不会禁止后续新消息继续生图，但应避免在条件没有变化时无限循环调用。每次工具调用仍是独立请求，插件不会在内部自动重试付费接口。

从旧版本升级后，请在 AstrBot 工具管理中确认两个新工具均已启用；旧工具不会继续保留在可调用列表中。

### QQ 头像人物参考

聊天 AI 可以在确实需要把相关人物画进图片时，为工具传入有序的 `avatar_targets`：

- `mention:1`、`mention:2`……：当前消息直接层中按原始顺序出现的有效群友 `@`；
- `sender`：发送当前消息的 QQ 用户；
- `bot`：当前 NapCat 机器人账号。

`avatar_targets` 是每次调用 `canvasforge_image_to_image` 都必须明确提交的字段；只使用回复图片、不使用人物头像时传空数组 `[]`。这样可以防止聊天 AI 漏掉头像参数后静默退化成文本生图。若该字段仍被省略，CanvasForge 会在调用付费图像接口前拒绝本次请求。纯文生图工具没有该参数。

例如，“画出 @小明 抱住 @小红”可对应 `["mention:1", "mention:2"]`；“把我和你画成合照”可对应 `["sender", "bot"]`；“你抱住 @小明，@小红在旁边”可对应 `["bot", "mention:1", "mention:2"]`。这里“我、本人、发送者”始终使用 `sender`，“你、机器人、助手”始终使用 `bot`，不能把它们计入 `mention:N`。普通的“@小明 来看一下”不代表要把小明画进图片，聊天 AI 不应选择其头像。

选择头像后，聊天 AI 应在提示词中用“人物参考1、人物参考2……”分配人物、关系和位置。参考图用于保持脸部、发型等稳定身份外貌；用户明确要求改变外貌时，以用户要求为准。表情、视线、姿势、动作、服装、构图和场景由聊天 AI 根据任务自行决定并写入提示词，可以保留参考图效果，也可以调整；不强制改变，也不要求复刻参考图。

群聊支持上述三类选择器；私聊只支持 `sender` 和 `bot`。机器人唤醒用的 `@` 和 `@全体成员` 会被排除，重复 `@` 会按首次出现位置去重；被回复消息中的 `@` 和更深层引用不会成为人物参考。`/canvasforge` 命令不进行 AI 意图判断，因此不支持头像选择。

头像和直接回复图片共用同一引用图数量与体积限制：默认合计最多 3 张，可在控制台调整为 1–10 张。超过上限、选择器无效或任意头像/回复图获取及校验失败时，整次请求会在调用付费接口前终止，不会截断或退化为纯文生图。

### 命令

```text
/canvasforge 一只坐在窗边看雨的橘猫，日系动画背景
```

命令提示词会直接发送给图像接口，不经过聊天 AI 润色。`/canvasforge` 保持自动模式：当前消息直接回复了图片时执行图生图，否则执行文生图。

### 引用图编辑

回复一条含图片的 QQ 消息，再让 AI 调用 `canvasforge_image_to_image`，或使用 `/canvasforge`。CanvasForge 只读取这次直接引用消息中的图片：

- 默认最多 3 张，可在管理页调整为 1–10 张；
- 保持原消息中的图片顺序；
- 超过数量限制或任意一张图片无效时，整次请求会在调用付费接口前终止；
- 不会读取当前消息附带的图片，也不会递归读取更早一层引用。

支持静态 PNG、JPEG、WebP。单张引用图固定不超过 15 MiB；其他总量、像素和边长限制可在管理页调整。

## 缓存与隐私

生成结果会在发送 QQ 前写入插件数据目录，默认保留最近 3 张。管理页可以预览、下载、删除或清空缓存；每张缓存都会明确标记实际走的是“文生图（generations）”还是“图生图（edits）”，旧缓存无法确认时显示“模式未知”。

控制台不会在打开页面或切换标签时自动读取图片缓存。管理员点击“刷新”后才会读取缓存列表，并按当前可见区域懒加载缩略图。

为降低 AstrBot 与 NapCat 处理大段 Base64 图片时的内存压力，超过 6 MiB 的 QQ 发送副本会自动压缩；缓存和下载仍保留图像接口返回的原图。

启用“允许 AI 使用 QQ 头像作为人物参考”后，被选择人物的 QQ 头像和群昵称会发送到管理员配置的外部 Sub2API 站点。头像仅在单次请求期间保存在内存中，不会写入 CanvasForge 图片缓存；关闭此功能不会影响普通文生图和回复图片编辑。

缓存会保存来源 QQ/群聊 ID、可读名称和生成元数据，但不会保存：

- 用户或 AI 的完整提示词；
- 被引用的原始图片；
- 人物参考头像；
- API Key；
- Sub2API 原始响应；
- `revised_prompt` 或 usage。

把缓存数量设为 `0` 会停止新增缓存，但不会自动删除已有图片。

QQ 头像可能是默认头像、旧缓存或卡通形象，只能作为外观参考。图像模型无法保证真人身份、全身服装、多人物面部或人物关系百分之百准确。

## 检查与安装更新

CanvasForge 不再提供自有更新面板或更新 API。请由管理员统一通过 AstrBot 插件管理检查和安装更新。

如果原生更新失败或插件异常，请在 AstrBot 插件管理中使用以下固定 GitHub 地址重新安装：

[`https://github.com/YuXya/astrbot_plugin_canvasforge`](https://github.com/YuXya/astrbot_plugin_canvasforge)

仓库地址应保持固定；GitHub 代理、下载和插件重载均交由 AstrBot 原生插件管理处理。

### 发布新版本

1. 同步修改 `metadata.yaml` 的 `version` 和 `main.py` 的 `PLUGIN_VERSION`；
2. 提交并推送同一份代码；
3. 从该提交创建同名的 `vX.Y.Z` Tag；
4. 使用该 Tag 创建正式 GitHub Release。

只有推送分支或创建 Tag、但没有创建正式 Release 时，AstrBot 插件管理可能无法识别为可安装的新版本。
当前代码版本为 `v0.1.7`。

## 行为边界

- 全插件仅允许一个进行中的生成请求，不排队；
- 工具和命令在受理后立即返回，后台完成后由插件直接发送图片或安全错误提示；
- “仅允许管理员使用”默认开启，同时限制两个 LLM 工具和 `/canvasforge` 命令；可在生成设置中关闭；
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
