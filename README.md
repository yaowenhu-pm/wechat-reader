# WeChat Reader

把公众号名称或文章链接交给 AI，读取可访问的正文，再按自己的要求整理成文档。

## 功能

- 按公众号名称定位账号，或直接输入一篇、多篇文章链接。
- 批量读取、去重，保存正文、标题、公众号和原文链接；失败项单独记录。
- 修改 JSON 配置，自定日期、关键词、关注要求与报告栏目。
- AI 阅读后输出 Markdown、HTML、PDF；每条整理保留来源。

## 使用

下载仓库，交给能运行本地 Python 的 AI，并告诉它：

> 读取 SKILL.md。我关注这些公众号／文章链接：……。请批量读取，按这些栏目整理：……，输出 PDF。需要登录时让我扫码，未读到的明确说明。

也可把文章链接写入 `urls.txt`，每行一条，在仓库目录运行：

```bash
python scripts/reader.py init --urls-file urls.txt --out config.json
python scripts/reader.py collect --config config.json --out run
```

正文在 `run/articles.json` 与 `run/originals/`。让 AI 按 `config.json` 中的规则整理；完整命令见[使用指南](references/usage.md)，配置字段见[配置说明](references/configuration.md)。

## 环境与边界

Python 3.8+；PDF 另装 `reportlab`。按名称采集需[安装采集端](references/collector-setup.md)并本人登录；直接链接模式不强制安装采集端。

**不保证任意公众号、全部历史文章都可读取。** 登录失效、验证码、删除及付费限制可能导致失败；图片内文字、音视频不在已验证范围。脚本负责采集与导出，AI 负责整理；没有内置定时无人值守分析。

仓库仅含代码、说明和合成示例，不含账号凭据或真实文章。
