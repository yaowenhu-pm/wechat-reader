# 公众号原文工具包

把这整个目录交给能运行 Python 的 AI 助手，并让它读取 `SKILL.md`。你提供公众号名单和整理要求，它负责修改配置、调用采集、读原文并生成报告。

可直接复制这句话：

> 请读取这个工具包的 SKILL.md。我关注的公众号是：……。先核对账号，获取可访问的原文，保存原文和来源链接。然后按我的要求整理：……。报告分为：……，最终给我 PDF。遇到登录需要我扫码；账号同名或找不到时告诉我，不要猜。

## 可以改什么

- `accounts`：任意公众号名称列表，不限制在内置行业名单。
- `selection`：日期、每号篇数、包含或排除的关键词。
- `output.instructions`：你希望 AI 怎么读、怎么整理。
- `output.sections`：最终交付有哪些栏目，每栏怎么写。
- `output.formats`：HTML、Markdown、PDF。

Schema 是配置的格式约定；日常修改 `config.json` 即可，不必改 Schema 定义。也可以继续改 Skill 和脚本适配自己的流程。

## 运行环境

原文工具和 HTML/Markdown 导出使用 Python 3.8+。采集端另用 Python 3.13 的独立环境，安装方法见 [采集端准备](collector-setup.md)。中文 PDF 需要 ReportLab，可在你自己的虚拟环境中运行：

```text
python -m venv .venv
# Windows 使用 .venv/Scripts/python；macOS/Linux 使用 .venv/bin/python
.venv/Scripts/python -m pip install reportlab
```

上面的 AI 助手需要具备本地命令和文件访问能力；只支持聊天或附件阅读的产品不能直接运行采集。

## 第一次使用：按公众号名称

在自己的工作目录执行，将 `SKILL` 换成工具包目录的绝对路径；后续所有相对命令路径也以该工作目录为准：

```text
python SKILL/scripts/reader.py init --accounts "动脉网" "36氪" --out config.json
python SKILL/scripts/reader.py validate --config config.json
```

准备采集端并扫码后，设置 `collector.credentials_file`（相对 `config.json` 所在目录）或配置的 Token 环境变量。

需要登录时先运行以下命令，打开返回的图片地址，由自己用微信扫码：

```text
python SKILL/scripts/reader.py qr --config config.json
python SKILL/scripts/reader.py status --config config.json
```

已有有效登录时不必重新申请二维码。`status` 表示配置和扫码状态，仍需实际抓取验证。

```text
python SKILL/scripts/reader.py resolve --config config.json --out run
python SKILL/scripts/reader.py collect --config config.json --out run
python SKILL/scripts/reader.py prepare --config config.json --articles run/articles.json --out run
```

让 AI 阅读完整 `reading-pack.json`，按配置填写 `editorial.json`，再执行：

```text
python SKILL/scripts/render_report.py --config config.json --articles run/articles.json --report run/editorial.json --out run/delivery
```

## 直接读取一篇或批量文章链接

只输入链接时，不需要提供公众号名称或安装采集端。把真实链接每行一条写到自己的 `urls.txt`，可以跨不同公众号：

```text
python SKILL/scripts/reader.py init --urls-file urls.txt --out links-config.json
python SKILL/scripts/reader.py collect --config links-config.json --out links-run
python SKILL/scripts/reader.py prepare --config links-config.json --articles links-run/articles.json --out links-run
```

也可以用 `init --urls "链接一" "链接二" --out links-config.json`。单篇和批量用同一个入口；脚本只读取明确给出的文章，不扩展到整个账号。少量失败不会中断其他链接，检查 `collection-status.json` 中的逐链接结果。重复链接自动去重；同一推送中的不同文章不会合并。

然后让 AI 完整阅读 `links-run/reading-pack.json`，按照配置整理为 `links-run/editorial.json`，再用上述 `render_report.py` 命令（换成对应路径）导出。批量原文抓取本身不会调用 AI 或自动产生语义总结。

文章可能要求验证、已删除或正文不可访问；直接链接入口也不能保证所有链接都成功。不以标题或摘要补齐正文。

## 输出

| 文件 | 用途 |
| --- | --- |
| `articles.json` | 标准化原文，含公众号、链接、正文、哈希等；可交给别的整理程序 |
| 逐篇 Markdown 文件 | 可直接阅读、保留和复用的正文 |
| `collection-status.json` | 抓取结果、未成功账号和覆盖范围 |
| `reading-pack.json` | 原文与个人整理规则，给 AI 阅读 |
| `editorial.json` | AI 整理后的结构化内容；修改它可重导出 |
| `delivery/report.html`、`report.md`、`report.pdf` | 按配置选择生成的最终交付 |

采集命令退出码 `2` 表示有失败项或覆盖提醒，不代表全部原文都未取得；查看 `collection-status.json` 和实际的 `articles.json` 再决定。退出码 `0` 也只表示本次所选入口读取成功，不证明完整公众号历史。

报告导出会核对来源 ID、正文哈希和引文是否真实出现在正文中。这个校验能阻止凭空造引文，但不能证明文章报道本身为真，仍需要 AI 正确判断。

## 不登录也能验证格式

`examples/demo-*` 都是合成内容，不能作为真实新闻或抓取成功证明。直接执行：

```text
python SKILL/scripts/render_report.py --config SKILL/examples/demo-config.json --articles SKILL/examples/demo-articles.json --report SKILL/examples/demo-report.json --out demo-output
```

默认生成 HTML 和 Markdown；加 `--pdf` 可验证中文 PDF。没有 ReportLab 时按上面的虚拟环境方式安装，再使用该环境的 Python 执行。

示例已经是规范的原文结构，也可直接执行 `reader.py prepare --config SKILL/examples/demo-config.json --articles SKILL/examples/demo-articles.json --out demo-reading`，让 AI 改成自己的栏目后重新整理。示例链接是 `example.com` 占位地址，不能拿它们执行真实抓取或 `import`；`import` 只接受带微信原文链接的正文记录。

## 实际覆盖边界

名称输入不等于所有账号均可抓。工具会核验身份；公众号后台搜索可能需要另一种登录，公开目录也可能缺号或名称过时，可用该号的一篇文章链接辅助定位。目录只用于找标识，不把目录当作全文来源。

当前适配的是 WeRSS 微信读书采集接口及其本地文章库。接口可能只返回最新文章，或限制历史和分页，工具会记录不完整状态；没有稳定日期证据的文章不会伪装为某日期发布。`unknown_date: include` 会保留并标注未知日期，`exclude` 会排除，不能因此声称日期范围已完整覆盖。

原文指接口实际返回的正文文本；图片内文字、音频、视频、付费部分不在已验证能力内。遇到验证码或登录失效先处理登录，工具不绕过平台限制。采集不调用模型，不会把全文自动发给外部模型服务；你让哪一个 AI 助手读文件，由你的使用环境决定。

仅本地交付，未自带定时调度或对外消息发送。接收者自己登录；分享 ZIP 内不含发送者的登录信息和真实文章。
