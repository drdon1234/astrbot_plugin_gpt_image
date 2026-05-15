# 精细生图 AstrBot 插件

这是一个独立 AstrBot 插件项目，可以放到 `AstrBot/data/plugins/astrbot_plugin_gpt_image` 使用。它提供：

- OpenAI-compatible 图片接口调用，默认模型 `gpt-image-2`。
- 用户命令只支持一个可选参数：末尾分辨率。
- 群聊和私聊独立配额：按“每多少分钟最多多少张图”限制。
- 管理员、白名单、黑名单权限控制。
- 回复/引用含图消息时，自动提取引用消息里的全部图片作为参考图。
- 参考图和生成图只写入 `tempfile` 创建的临时目录，发送或请求结束后删除，插件终止时兜底清理。
- 使用 AstrBot 的 `logger` 接口，不直接调用 Python 标准日志模块。

## 安装

```bash
cd AstrBot/data/plugins
git clone <this-repo-url> astrbot_plugin_gpt_image
```

或者直接把本目录复制到 `AstrBot/data/plugins/astrbot_plugin_gpt_image`。

AstrBot 会根据 `requirements.txt` 安装依赖；如果手动安装：

```bash
pip install -r requirements.txt
```

## 配置

在 AstrBot WebUI 的插件配置中填写：

- `api.api_key`：OpenAI-compatible API Key。也可用环境变量 `OPENAI_API_KEY`。
- `api.base_url`：兼容 `/v1` 的接口地址。
- `defaults.size_preset`：默认尺寸选项，下拉包含 `auto`、16:9 / 9:16 / 1:1 的 1K、2K、4K 常用尺寸和 `custom`。
- `defaults.custom_size`：自定义分辨率，仅当 `defaults.size_preset = custom` 时生效。
- `defaults.quality` / `defaults.count`：默认质量和张数。
- `quota.group.window_minutes` / `quota.group.max_images`：群聊窗口和张数。
- `quota.private.window_minutes` / `quota.private.max_images`：私聊窗口和张数。
- `permissions.admin_id`：管理员 ID，优先于所有白名单和黑名单。
- `permissions.whitelist.*` / `permissions.blacklist.*`：用户/群聊白名单和黑名单。

插件固定使用 `gpt-image-2`、`opaque` 背景和 `png` 输出格式。参考图数量、参考图大小、生成张数上限和尺寸规则按官方限制内置，不作为配置项；图片下载超时固定为 30 秒。

`max_images = 0` 表示该作用域不限额。群聊配额按群号计数，私聊配额按用户号计数。

权限优先级固定为：管理员 > 个人白名单 > 个人黑名单 > 群组白名单 > 群组黑名单。白名单启用但没有命中时，普通用户会被拒绝；白名单未启用时，未命中黑名单的普通用户默认放行。

## 使用

```text
/gimg 一只玻璃材质的白色机械鸟，产品摄影风格
/gimg 赛博茶馆室内设计 1536x1024
/gimg 赛博茶馆室内设计 auto
/生图 把参考图中的杯子改成红色 1536*1024
/生图 一个银白色机械鸟停在玻璃树枝上 1536×1024
/gimg_status
```

分辨率必须放在提示词最后，用空格分隔。不支持 `尺寸 1536x1024`、`质量 高`、`张数 2` 这类聊天参数；这些值由插件默认配置控制。

如果用户只发送 `/生图`、使用命令行参数写法，或分辨率超出限制，插件会发送固定参数提示文本。

当用户回复/引用一条包含图片的消息再执行 `/gimg`，插件会尽力提取被引用消息里的全部图片作为参考图。没有引用图时默认纯文本生图。跨平台的引用消息结构不完全一致，OneBot v11 会尝试通过 `get_msg` 拉取被引用消息；其他平台如果事件链或原始消息已包含引用内容，也会自动识别。

## 参数约束

尺寸支持 `auto` 或 `宽x高`。插件内置 `gpt-image-2` 官方尺寸校验：

- 单边最大 3840。
- 宽高都需要是 16 的倍数。
- 宽高比不超过 3:1。
- 总像素在 655360 到 8294400 之间。

默认尺寸下拉包含：

- `auto`
- 16:9：`1280x720`、`2560x1440`、`3840x2160`
- 9:16：`720x1280`、`1440x2560`、`2160x3840`
- 1:1：`1024x1024`、`2048x2048`、`2880x2880`
- `custom`：读取 `defaults.custom_size`

单次最多生成 10 张，最多使用 16 张参考图，单张参考图最大 50MB。

## 数据与临时文件

插件只把配额账本作为持久数据放在：

```text
AstrBot/data/plugin_data/astrbot_plugin_gpt_image/
```

其中 `usage.json` 用于配额记账。

引用图下载、base64 参考图、本地参考图副本、生成图下载或解码结果都会写入 Python `tempfile` 创建的插件临时目录。生成图在 AstrBot 发送后立即删除；插件终止时会拒绝新请求、取消进行中的生图任务，并兜底删除临时目录。

## 本地检查

不包含测试目录；可以用下面命令做基础语法和配置检查：

```bash
python -m compileall .
python -c "import json; json.load(open('_conf_schema.json', encoding='utf-8')); print('json ok')"
```

## 许可证

本项目基于 GNU Affero General Public License v3.0 or later 发布，详见 `LICENSE`。
