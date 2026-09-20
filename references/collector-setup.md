# 接收者安装公众号采集服务

只有还没有可用采集服务时才需要安装。Skill 的原文采集 CLI 与服务分开；共享包不包含别人的微信登录、管理员密码、原文、数据库或上游完整源码。

## 已有服务

先确认服务地址和接收者自己的管理员凭据，将它们填入任务配置的 `collector.base_url` 与 `collector.credentials_file`。凭据文件使用 JSON：`{"username":"自己的用户名","password":"自己的密码"}`。不要把凭据写进共享配置、对话回复或交付件。

此处的 `setup_collector.py start` 只管理它自己 `prepare` 出来的服务。对于其他现有服务，直接使用主 CLI；不要把现有安装移动进新 state 或覆盖其配置。

## 新安装

需要 Git、Python **3.13**、访问 GitHub 和 Python 包源的网络。安装辅助脚本本身兼容 Python 3.8+，但固定采集服务使用独立的 Python 3.13 虚拟环境。安装过程需要下载 Python 依赖和 Playwright WebKit，可能持续数分钟。不会安装系统级 Python、Git 或 Linux 系统库。

从解压后的 `wechat-reader` 目录运行，state 选接收者自己的私有目录，放在 Skill 文件夹外。例如 Windows PowerShell：

```powershell
$collectorState = Join-Path $env:LOCALAPPDATA 'wechat-reader-collector'
python scripts/setup_collector.py prepare --state "$collectorState"
python scripts/setup_collector.py start --state "$collectorState" --port 8006
python scripts/setup_collector.py status --state "$collectorState"
```

找不到 Python 3.13 时显式传入**可执行文件路径**（不是一段 shell 命令）：

```powershell
python scripts/setup_collector.py prepare --state "$collectorState" --python 'C:\Path\To\Python313\python.exe'
```

macOS/Linux 使用相同 CLI，例如：

```bash
python3 scripts/setup_collector.py prepare --state "$HOME/.local/share/wechat-reader-collector" --python /path/to/python3.13
python3 scripts/setup_collector.py start --state "$HOME/.local/share/wechat-reader-collector" --port 8006
```

服务默认地址是 `http://127.0.0.1:8006`。把 prepare 返回的 `credentials_file` **绝对路径**填入任务配置 `collector.credentials_file`，把服务地址填入 `collector.base_url`。state 中的 `login.json` 有 `username`、`password`、`secret_key`，主 CLI 只需要前两项。再次 prepare 会保留原凭据；不要手动重置密码后期待既有数据库自动同步。

`start` 在后台运行，Windows 不弹出终端窗口。只有健康检查通过才返回 `running`；`starting_unverified` 表示进程已创建，但服务尚未验证就绪，再运行 status。端口被占用时检查原服务或换端口，脚本不会结束其他程序。status 的 `running` 只确认服务登录可用，**不代表微信读书或公众号后台已经登录**。

安装脚本固定使用 [rachelos/we-mp-rss 的提交 d8feb6a](https://github.com/rachelos/we-mp-rss/tree/d8feb6a42c6773d7374e03c487d3ae3426084af8)，下载源码时保留其 [MIT License](https://github.com/rachelos/we-mp-rss/blob/d8feb6a42c6773d7374e03c487d3ae3426084af8/LICENSE)。这是上游仓库的许可；其依赖各有许可，公众号文章内容本身并不因此获得再分发授权。

## 用户本人登录

采集服务就绪后，按照 Skill 主 CLI 的二维码流程，让**接收者本人**使用微信扫码并确认登录。不能复用分享者的 cookie 或会话。微信读书登录和微信公众号后台登录是两种独立会话：

- 微信读书会话用于读取该会话可访问的公众号文章。
- 仅输入公众号名称时，搜索公众号可能还需要登录微信公众号后台，并且该用户账号具备对应后台访问能力。
- 如果名称搜索不可用，让用户提供一篇该公众号文章链接，或已确认的公众号 ID，再按主 CLI 支持的入口继续。不要将文章链接中的 `__biz` 直接冒充微信读书 `mp_id`。

扫码成功仍需验证目标公众号身份及实际原文。名称重名、账号不可见、登录过期、验证码、文章已删除、正文抓取失败，都应呈现为待处理状态，不能用摘要填充原文。工具支持用户指定目标公众号，但不承诺所有公众号或全部历史文章都一定可读。

## 本地文件与常见问题

state 目录包含：`we-mp-rss/`（固定源码、数据库、微信会话）、`venv/`、`browsers/`、`login.json`、`prepared.json`、`process.json`、`setup.log`、`stdout.log`、`stderr.log`。这些是接收者运行数据，**不要重新压进分享包**。上游运行日志可能包含微信授权信息，也不要直接发布完整日志。

辅助脚本应用已核对的补丁：取消启动时输出全部环境变量；HTTP 只绑定 `127.0.0.1`；使用随机管理员密码和服务签名密钥；GitHub 版本查询设 5 秒超时。启动/安装子进程采用环境变量白名单，不继承 AI 模型密钥、Python 注入路径或代理凭据。它关闭采集定时任务、自动补抓、内置 Redis、级联和自动加入书架。需要采集时由主 CLI 明确发起。

- Python 版本不对：安装 3.13 并传 `--python`。脚本不会修改系统默认 Python。
- 下载或依赖安装失败：查看本地 `setup.log` 中对应错误；修复网络或依赖后重新 prepare。半途失败没有 `prepared.json`，不能算安装成功。若 Git 下载中断造成不完整 checkout，使用新的空 state 重试；脚本不自动删除目录。
- Linux WebKit 提示缺系统库：按本机发行版安装 Playwright 提示的依赖；需要管理员操作时由用户处理。脚本不自动执行 sudo 或包管理器。
- `port_in_use_unverified`：端口上有服务，但未用当前 login.json 验证成功；可能还在启动、凭据不匹配或是其他程序。
- 上游源码被修改：脚本拒绝覆盖关键文件或启动未核对版本。保留原状态，选择新的 state 安装，或人工审查修改。
- 代理：为避免把认证代理地址和模型密钥传入采集服务，脚本不继承代理环境变量；若网络只能通过代理访问，需要接收者在本机自行配置适当的网络通路。

当前已在 Windows 上检查固定版本和启动方式；安装辅助脚本有隔离的单元测试。macOS/Linux 的安装和浏览器系统依赖仍需在接收者机器上验证。不要把本地脚本测试通过表述为所有平台、所有公众号都已完成采集验收。
