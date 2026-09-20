# 修改配置

日常只改个人 `config.json`。结构见 [config.schema.json](../schemas/config.schema.json)，原文交换格式见 [articles.schema.json](../schemas/articles.schema.json)。`reader.py validate` 还检查日期顺序、栏目 ID 和账号重名等运行条件。

## 公众号

### 直接读取指定文章（支持批量）

```json
{
  "accounts": [],
  "article_urls": ["https://mp.weixin.qq.com/s/文章一", "https://mp.weixin.qq.com/s/文章二"]
}
```

以上是个人配置里的来源部分，其余字段由 `init` 自动补齐。只给链接时不需要提供公众号名，也不调用采集端登录：程序从可读的文章页识别标题和公众号，只读取指定文章，不扩展到整号。重复链接去重，同一推送的不同 `idx` 保留为不同文章；某篇失败不阻止其余文章。

可在 `urls.txt` 中每行放一条链接，用 `reader.py init --urls-file urls.txt --out config.json` 导入。空行和以 `#` 开头的注释会被忽略。支持 `--urls`、`--urls-file`、`--accounts` 混合输入。至少提供一个来源。

### 按公众号名称采集

最简单是 `"accounts": ["公众号一", "公众号二"]`。无法仅凭名称定位时，账号项可改为：

```json
{"name": "公众号名称", "id": "MP_WXS_数字标识"}
```

或提供该公众号某篇真实文章的 URL：

```json
{"name": "公众号名称", "article_url": "https://mp.weixin.qq.com/s/真实文章路径"}
```

账号对象内的 `article_url` 用于辅助定位该公众号，属于按账号采集；若只想读这篇文章，应使用顶层 `article_urls`。也可设置 `rss_url`，RSS 提供文章链接列表。不要同时填写几个相互矛盾的入口。

`collector.base_url` 为接收者的采集服务。`credentials_file` 指向仅含接收者账号信息的私有文件，或用 `token_env` 指定环境变量名称。路径相对于配置文件，不依赖发送者的 Windows 用户目录。

`directory_file` 可指向本地 CSV（`name,bizid`）或账号 JSON 列表。默认公共目录只协助找标识，所有名称仍须当前接口核验。设 `directory_url: ""` 关闭公共目录网络访问；不会把本机私人数据上传到目录。`max_pages` 是读取采集端已有文章库的页数上限，不是已抓取多少页历史的承诺。

## 范围

- `since` / `until`：`YYYY-MM-DD`，含首尾两天；不限制填 `null`。日期未知的记录按 `unknown_date` 处理。
- `max_articles_per_account`：每号最多保留几篇，1–1000；达到上限不代表抓全。
- `include_keywords`：为空则不作关键词限制；有值时标题或正文含任意一个即可。
- `exclude_keywords`：命中任意一个则排除，优先于包含词。
- `unknown_date`：`include` 保留未知日期并标注，`exclude` 排除。严格日期任务可用 `exclude`，但需接受可能无法获得文章。

这些是确定性预筛选；复杂语义条件写在 `output.instructions`，交给 AI 在读完原文后判断。想保留所有原文时，让关键词列表为空。

## 整理与输出

例：

```json
{
  "title": "本周产品观察",
  "instructions": "关注新产品的实际变化和用户反馈，少写口号。区分作者观点与已发生的事。",
  "sections": [
    {"id": "changes", "title": "产品变化", "instructions": "写具体变化、影响的人群和原文证据。"},
    {"id": "feedback", "title": "用户反馈", "instructions": "合并相似反馈，保留分歧；没有明确反馈就留空。"}
  ],
  "formats": ["html", "markdown", "pdf"]
}
```

把这个对象填入 `output`。栏目 `id` 自定但不能重复；栏目顺序即报告顺序。导出器只渲染结构，不替 AI 编内容。要新增不同的复杂排版或新的字段类型，再同时修改报告 Schema 与渲染器。

原文 `content_hash` 延续原项目算法：对正文用 Python `json.dumps(text, ensure_ascii=False, sort_keys=True).encode('utf-8')` 再取 SHA-256。不要自己改哈希或把清洗后文本配上旧哈希；重新 import 即可更新。
