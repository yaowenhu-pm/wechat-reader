# 使用指南

把工具包交给能读写文件、运行 Python 的 AI，并让它读取 `SKILL.md`。通常交付两部分：原文 Markdown 与本地图片，以及按个人规则整理的独立 PDF。用户只要原文时，完成原文导出与检查即可。

可直接说：

> 读取 SKILL.md。获取这些公众号／文章链接：……，给我原文 Markdown 和图片，再按这些栏目整理成 PDF：……。需要登录时让我扫码，未取得的内容明确说明。交付前检查文字、图片路径和 PDF。

以下命令中的 `SKILL` 替换为工具包绝对目录，在自己的工作目录运行；配置、登录数据与真实文章放在个人目录，不改工具包自带示例。

## 环境

采集 CLI 使用 Python 3.8+。原文图文导出安装 `requirements-export.txt`；整理 PDF 另需 ReportLab。建议使用自己的虚拟环境：

```text
python -m venv .venv
# Windows 后续使用 .venv/Scripts/python；macOS/Linux 使用 .venv/bin/python
.venv/Scripts/python -m pip install -r "SKILL/requirements-export.txt" reportlab
```

下文的 `python` 指已安装依赖的那个解释器。按名称采集所需的服务另使用 Python 3.13，见[采集端准备](collector-setup.md)。只支持聊天或附件阅读的 AI 产品不能直接运行本地采集。

## 先取得正文

**一篇或多篇文章链接：** 将真实链接每行一条写入自己的 `urls.txt`，允许空行与 `#` 注释行，可以跨公众号。

```text
python "SKILL/scripts/reader.py" init --urls-file urls.txt --out config.json
python "SKILL/scripts/reader.py" collect --config config.json --out run
```

也可用 `init --urls "链接一" "链接二" --out config.json`。纯链接模式不要求采集端登录，只读明确提供的文章，不扩展到整个账号。重复链接去重，同一推送中不同文章保留；某篇失败不阻止其余文章。

**按公众号名称：** 创建配置，准备自己的采集服务并填写 `collector.base_url`、`collector.credentials_file` 或 Token 环境变量名。凭据文件路径相对配置文件解析。

```text
python "SKILL/scripts/reader.py" init --accounts "公众号一" "公众号二" --out config.json
python "SKILL/scripts/reader.py" validate --config config.json
python "SKILL/scripts/reader.py" status --config config.json
```

已有有效登录无需重新扫码；需要登录时运行 `reader.py qr --config config.json`，由用户本人打开返回的二维码图片并扫码，再检查 status。之后运行：

```text
python "SKILL/scripts/reader.py" resolve --config config.json --out run
python "SKILL/scripts/reader.py" collect --config config.json --out run
```

同名或无法核验时，提供该号文章链接或准确账号标识继续定位。账号对象内的 `article_url` 是定位账号的线索；只读某篇文章应使用顶层 `article_urls`。采集端缺少正文时，程序会尝试从原文页面补读并核对身份，失败则记录原因。

已有正文可用 `reader.py import --config config.json --input articles.json --out run` 导入，只接受标记为 `content_kind: "fulltext"`、带微信原文链接的记录，不把摘要扩写成原文。

两种入口都需检查 `run/collection-status.json`。退出码 `2` 可能表示部分失败或覆盖提醒，不代表所有正文都未取得；退出码 `0` 也不证明完整公众号历史。失败不能解释成账号没有更新。

## 导出原文 Markdown 与图片

`collect` 保留 `run/articles.json` 和基础文本 `run/originals/<article-id>.md`。交付带图片的原文时，再运行独立出口：

```text
python "SKILL/scripts/export_originals.py" --articles run/articles.json --out run/source-delivery
```

每轮导出使用新的目录，例如 `run/source-delivery-2026-09-20`。若目录内已有原文文件，程序拒绝覆盖；重试时换一个 `--out`，保留上轮交付。可加 `--offline` 只验证转换、不下载图片；存在图片时会保留源链接并记录部分导出。出口返回 `0` 表示本轮完整导出，`2` 表示部分导出或失败，详情以本轮状态与错误信息为准。

输出结构：

```text
run/source-delivery/
  export-status.json
  originals/
    <article-id>/
      article.md
      images/
```

有原始 HTML 时，导出器转换正文并保存图片，Markdown 用相对路径引用本地图。若只有纯文本，则明确记录图片不可验证，不能称为完整图文导出。图片下载失败时保留远程链接，并在 `export-status.json` 标记 `partial`；不能将“有 Markdown 文件”当成图片均已落地。

标题、公众号、作者和发布日期按已取得证据填写。不知道发布日期就注明未知，不把抓取时间或文件生成时间当作发布日期。原文出口保留内容；个人筛选、摘要与判断放到独立整理稿。

交付前实际打开 Markdown，核对正文起止、段落及图片引用；检查每个相对图片路径和文件可读性。有源 HTML 时可比对 `#js_content` 与导出正文，允许排版和空白变化，但不能漏段。连同整个 `originals/<article-id>/` 文件夹交付，移动时保持 `article.md` 与 `images/` 的相对位置。

## AI 阅读后生成独立整理 PDF

在 `config.json` 修改 `output.instructions` 与 `output.sections`，确定关注要求、栏目及写法，`output.formats` 中加入 `pdf`。日期、关键词与每号篇数在 `selection` 中设置，完整字段见[配置说明](configuration.md)。

```text
python "SKILL/scripts/reader.py" prepare --config config.json --articles run/articles.json --out run
```

让 AI 完整阅读 `run/reading-pack.json`，以 `report-template.json` 为结构参考，另存 `run/editorial.json`。每个条目引用对应文章 ID、正文哈希及连续短引文，区分原文事实、作者观点和推断。栏目允许为空，不凑内容，也不把全文复制进整理报告。

```text
python "SKILL/scripts/render_report.py" --config config.json --articles run/articles.json --report run/editorial.json --out run/delivery
```

按配置生成 `report.pdf`、`report.html`、`report.md`。导出器核对来源 ID、正文哈希和引文，但不替代语义核对。打开实际 PDF 并查看渲染结果，检查中文、分页、裁切和原文链接；缺依赖或导出失败就明确说明未生成，不能把其他文件改名为 PDF。交付原文目录与独立报告，简要说明未完成项和覆盖范围。

采集及导出脚本不调用模型；语义整理由当前 AI 会话完成，无需另填模型密钥。仅运行采集命令不会自动生成总结。

## 不登录也能验证整理格式

`examples/demo-*` 是合成内容，仅用于格式验证：

```text
python "SKILL/scripts/render_report.py" --config "SKILL/examples/demo-config.json" --articles "SKILL/examples/demo-articles.json" --report "SKILL/examples/demo-report.json" --out demo-output --pdf
```

也可将示例传给 `reader.py prepare`，改栏目后重新整理。示例的 `example.com` 链接不能用于真实抓取或 `import`，没有原始 HTML 的合成材料也不能证明图文导出成功。

## 覆盖与分享

名称输入不等于所有账号都能抓取。当前账号采集适配 WeRSS 微信读书接口及其本地文章库，历史和分页可能受限；公众号后台搜索可能还需要另一种登录。公开目录仅用于发现账号标识，不能当作全文来源。

未知日期按 `selection.unknown_date` 保留或排除，不能据此宣称某段日期已完整覆盖。遇到验证码、登录失效、删除或付费限制时如实记录，不绕过限制。下载图片不等于识别其中的文字，音视频和付费内容也不在已验证范围。

分享代码时只打包干净代码、说明及合成示例；个人登录状态、凭据、原始 HTML、真实全文与交付目录留在本地。工具没有内置定时无人值守分析或对外发送功能。
