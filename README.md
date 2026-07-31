# CanvasForge

CanvasForge 是面向 AstrBot + NapCat QQ 的图片生成插件。它通过 Sub2API 的 OpenAI Images 兼容接口提供：

- 文本生成图片；
- 使用当前消息附图或直接回复的图片进行多图参考编辑；
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

其余模型、画质、超时、冷却、全局并发、引用图限制与缓存数量，请在插件详情页打开“CanvasForge 控制台”设置。`max_concurrent_generations` 默认是 `3`，可在高级设置中调整为 `1–32`。

> [!WARNING]
> AstrBot 当前会把插件配置中的 API Key 明文写入 `data/config/...json`。CanvasForge 不会在日志、错误消息或管理页 API 中回显 Key，但仍需限制配置文件的读取权限。

公网地址必须使用 HTTPS。本机、回环/内网地址及本地容器主机名可使用 HTTP。

LLM Tool 会先检查参考图、权限、配置、当前会话状态、全局并发与用户冷却。检查失败时返回明确的结构化错误；成功时立即返回 `accepted=true`、`state=generating`、`finished=false`、`completed=false` 和 `task_id`，随后在后台启动图像请求，不等待当前 AI 先发送固定话术。

图片发送成功后，CanvasForge 会先把同一 `task_id` 的权威会话记录改写为 `state=idle`、`finished=true`、`completed=true`、`image_sent=true`，再使用任务发起时的原会话、原人格和原聊天模型生成自然的完成通知。即使通知生成较慢、失败或未能发送，下一轮 AI 也能从结构化终态得知图片已经完成。

后台获取参考图、调用图像接口或发送图片失败时，插件同样先保存与 `task_id` 对应的 `finished=true`、`failed=true` 终态，再让原聊天 AI 根据安全原因生成失败通知。失败不会产生冷却。`/canvasforge` 命令会先发送固定等待语，发送成功后才启动后台生成；后续终态与人格通知和 LLM Tool 一致。

## 使用

### LLM Tool

CanvasForge 提供两个职责分离的 LLM 工具，由当前聊天 AI 按任务选择：

- `canvasforge_text_to_image(prompt)`：根据文字从零生成图片。它不会读取当前消息附图、直接回复图片或聊天参与者头像；即使消息中带有图片，也可以正常调用。
- `canvasforge_image_to_image(prompt, avatar_targets=[])`：使用当前消息附图、直接回复图片和可选的聊天参与者头像生成或修改图片。至少需要一张参考图；没有可用参考时返回 `reference_required`。

两项工具都由当前聊天 AI 自行编写完整提示词。当前附图、直接回复图片和人物头像会按此顺序合并、去重，并作为同一次 Images 编辑请求的参考图片上传，不会先调用视觉聊天模型进行看图或转述。图生图不读取嵌套回复或历史图片。

两项工具采用相同的异步状态语义：`state=generating` 表示后台正在处理；之后同一 `task_id` 会在原会话中变为结构化的 `completed` 或 `failed` 终态。图片成功发送或后台失败后，CanvasForge 会额外调用一次禁用工具的原聊天模型生成符合人格的通知；工具调用本身不接收预写完成语。旧的组合工具 `canvasforge_generate_image` 已删除。

工具只报告调用结果和当前任务状态，不要求 AI 使用固定等待话术，也不限制 AI 在校验失败后如何修正参数或重新选择工具。同一 conversation 已有任务时返回当前 `generating` 状态，不创建第二个付费请求。

从旧版本升级后，请在 AstrBot 工具管理中确认两个新工具均已启用；旧工具不会继续保留在可调用列表中。

#### 推荐人设规则

以下内容可直接加入聊天 AI 的人设。CanvasForge 的工具说明本身仍是完整的，不配置这段人设也不会改变插件校验：

```text
## CanvasForge 工具规则
- 用户要画图、生成图片、做头像或修改图片时，使用 CanvasForge。
- 从文字直接创作时用 `canvasforge_text_to_image`；要使用当前附图、直接回复图片或聊天参与者头像作为参考时，用 `canvasforge_image_to_image`。
- 图生图的 `avatar_targets` 可按需选择 `sender`、`bot` 或 `mention:N`；参考图用于保持人物身份与整体外貌，需要改变的内容写进提示词。
- `state=generating` 表示任务正在后台处理；后续以同一 `task_id` 的 `completed` 或 `failed` 终态及 CanvasForge 通知为准。
```

### 参考图人物与 QQ 头像

图生图会依次收集当前消息附图、直接回复消息中的图片和 `avatar_targets` 指定的 QQ 头像；相同来源会去重。聊天 AI 可以按需要传入有序的 `avatar_targets`：

- `mention:1`、`mention:2`……：当前消息直接层中按原始顺序出现的有效群友 `@`；
- `sender`：发送当前消息的 QQ 用户；
- `bot`：当前 NapCat 机器人账号。

`avatar_targets` 是可选字段；只使用消息图片时可以省略或传空数组 `[]`。纯文生图工具没有该参数，也不会读取任何消息图片或头像。

例如，“画出 @小明 抱住 @小红”可对应 `["mention:1", "mention:2"]`；“把我和你画成合照”可对应 `["sender", "bot"]`；“你抱住 @小明，@小红在旁边”可对应 `["bot", "mention:1", "mention:2"]`。“我、本人、发送者”使用 `sender`，“你、机器人、助手”使用 `bot`。不要传 QQ 号、昵称或 URL，也不要从历史消息猜测人物。

插件会按实际上传顺序，为 QQ 头像补充输入图编号和昵称映射，帮助图像模型区分人物。运行时只追加一条轻量说明：参考图用于保持人物身份与整体外貌，其余内容按提示词编辑。

群聊支持上述三类选择器；私聊只支持 `sender` 和 `bot`。机器人唤醒用的 `@` 和 `@全体成员` 会被排除，重复 `@` 会按首次出现位置去重；被回复消息中的 `@` 和更深层引用不会成为人物参考。`/canvasforge` 命令不进行 AI 意图判断，因此不支持头像选择。

当前消息附图、直接回复图片和头像共用同一引用图数量与体积限制：默认合计最多 3 张，可在控制台调整为 1–10 张。超过上限、选择器无效或任意参考图获取及校验失败时，整次请求会在调用付费接口前终止。

### 命令

```text
/canvasforge 一只坐在窗边看雨的橘猫，日系动画背景
```

命令提示词会直接发送给图像接口，不经过聊天 AI 润色。`/canvasforge` 保持自动模式：当前消息附图或直接回复中有图片时执行图生图，否则执行文生图。预检成功后，插件先发送固定等待语，再启动后台图像请求；完成或失败终态保存后，同样由原会话中的聊天 AI 生成自然通知。

### 消息图片编辑

发送带图消息或直接回复一条含图片的 QQ 消息，再让 AI 调用 `canvasforge_image_to_image`，也可以使用 `/canvasforge`。CanvasForge 依次读取当前消息附图和这次直接回复消息中的图片：

- 默认最多 3 张，可在管理页调整为 1–10 张；
- 保持各来源中的图片顺序，相同来源会去重；
- 超过数量限制或任意一张图片无效时，整次请求会在调用付费接口前终止；
- 不会递归读取更早一层引用或从历史消息寻找图片。

消息图片中的人物和 QQ 头像都作为身份与整体外貌参考；要修改的内容由提示词决定。消息图片不需要额外人物选择器，非人物图片仍按普通编辑参考处理。

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
当前代码版本为 `v0.1.11`。本版本将两个工具整理为常规文生图/图生图接口，增加当前消息附图支持与按 conversation 隔离的任务状态，并在图片发送或后台失败后可靠写入结构化终态。`v0.1.10` 及更早版本的 Tag 与 Release 保持不变。

## 行为边界

- 每个 conversation 同时最多一个生成任务，不排队；不同 conversation 可以并发，切换或重置会话不会取消旧任务；
- 全局并发上限由 `max_concurrent_generations` 控制，默认 `3`、可调 `1–32`；达到上限时返回 `busy`，不会占用新会话任务位；
- LLM Tool 受理后立即启动后台 Provider，并返回 `accepted=true`、`state=generating`、`finished=false`、`completed=false` 和 `task_id`；不依赖当前 AI 先发送或保存固定等待回复；
- 同一 conversation 再次调用时返回当前任务的 `state=generating` 和 `task_id`，不会创建第二个付费请求；
- 图片发送成功后先提交冷却、释放会话任务位，再把原会话中的同一 `task_id` 改写并核验为 `state=idle`、`finished=true`、`completed=true`、`image_sent=true`，最后生成一次禁用工具的人格通知；
- 参考图、图像接口或图片发送失败时先释放任务位并保存 `finished=true`、`failed=true`、`image_sent=false` 的安全终态，再由原聊天 AI 生成失败通知；失败不产生冷却；
- 成功或失败终态写入任务发起时的原 conversation；原会话已删除或重置时不创建替代会话，也不会污染当前新会话；
- 成功通知生成期间若出现新的用户回合，会跳过迟到的完成语；失败通知仍会送达。通知异常、超时或返回空白时使用固定安全兜底，不改变已经保存的终态；
- “仅允许管理员使用”默认开启，同时限制两个 LLM 工具和 `/canvasforge` 命令；可在生成设置中关闭；
- 每位 QQ 用户默认成功生成后冷却 300 秒，冷却跨 conversation 计算；管理员绕过用户冷却，但不能绕过全局并发上限；
- 当前版本仅实现 Sub2API GPT Images Provider，不实现 Gemini、流式图片、mask、多输出或本地关键词审核。

## 技术基线

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Pages](https://docs.astrbot.app/dev/star/guides/plugin-pages.html)
- [Sub2API](https://github.com/Wei-Shaw/sub2api)

## License

AGPL-3.0
