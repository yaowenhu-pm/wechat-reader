# WeChat Reader

把公众号名称或文章链接交给 AI，取得可访问的原文，再按自己的要求整理。

## 交付什么

- 原文 Markdown 与随附的本地图片，保留标题、公众号和来源链接。
- 单独的 AI 整理 PDF，栏目与关注要求由你指定；只要原文时不额外总结。
- 采集与导出状态，明确哪些成功、哪些失败。没有原始 HTML 时仅交付纯文本，并注明图片未验证。

## 使用

下载仓库，交给能运行本地 Python 的 AI：

> 读取 SKILL.md。获取这些公众号／文章链接：……，给我原文 Markdown 和图片，再按这些栏目整理成 PDF：……。需要登录时让我扫码，交付前检查文件能否打开。

也可把链接每行一条写入 `urls.txt`，在仓库目录运行：

```bash
python -m pip install -r requirements-export.txt
python scripts/reader.py init --urls-file urls.txt --out config.json
python scripts/reader.py collect --config config.json --out run
python scripts/export_originals.py --articles run/articles.json --out run/source-delivery
```

原文交付在 `run/source-delivery/`，请连同图片目录一起保存。AI 阅读与 PDF 导出步骤见[使用指南](references/usage.md)，可改字段见[配置说明](references/configuration.md)。

## 环境与边界

Python 3.8+；PDF 另装 `reportlab`。按名称采集需[准备采集端](references/collector-setup.md)并本人登录；链接模式不强制安装。

**不保证任意公众号或全部历史都能读取。** 验证码、删除、登录及付费限制可能导致失败；图片下载不等于识别图片文字。脚本负责采集和导出，AI 负责整理，无内置定时分析。

分享包只含干净代码、说明与合成示例，不包含凭据或真实文章。
