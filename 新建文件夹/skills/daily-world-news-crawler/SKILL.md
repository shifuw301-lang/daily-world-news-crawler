---
name: daily-world-news-crawler
description: 抓取当日全球新闻并生成 Markdown 日报。运行内置脚本从 12 个公开 RSS 源（BBC、The Guardian、Al Jazeera、NPR、DW、France 24、Sky News、CBC、Anadolu、CNA、ABC Australia、Euronews）采集当天发布的新闻，去重后输出包含标题、来源、简要摘要、链接的 Markdown 文件。当用户要求"抓取今天的新闻""全球新闻日报""新闻汇总/简报/列表""每天自动收集新闻"，或需要为内容创作准备当日国际新闻素材时使用。仅访问公开可访问的 RSS 页面，内置 robots.txt 校验、请求限流与重试。
---

# Daily World News Crawler

从公开 RSS 源抓取当日全球新闻，输出一份可直接阅读的 Markdown 日报。

## 快速开始

在项目根目录执行一条命令即可：

```bash
python skills/daily-world-news-crawler/scripts/crawl_news.py
```

默认输出 `output/world-news-YYYY-MM-DD.md`。只用了 Python 标准库，无需安装任何依赖。

跑通再交付：命令正常结束时最后一行会打印 `✅ 已写入 <路径>（成功 N/M 个源，收录 K 条）`。把这条统计、Markdown 路径和几条头条标题一起反馈给用户。

## 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--out PATH` | `output/world-news-YYYY-MM-DD.md` | 输出文件路径（固定文件名，会覆盖） |
| `--outdir DIR` | `output` | 只指定输出目录，文件名仍按日期生成，定时任务用这个 |
| `--since` | `today` | 时间窗口：`today` / `24h` / `48h` / `7d` |
| `--limit N` | `10` | 每个源最多收录条数 |
| `--summary-limit N` | `140` | 摘要截断长度（字符） |
| `--sources a,b` | 全部 | 只抓指定源，key 见 `--list-sources` |
| `--delay S` | `2.0` | 同一站点两次请求的最小间隔（秒），低于 0.5 会被抬到 0.5 |
| `--max-per-minute N` | `15` | 全局每分钟请求上限 |
| `--timeout S` | `15` | 单次请求超时 |
| `--retries N` | `2` | 单个源最大尝试次数（仅 429/5xx 重试） |
| `--min-items N` | `8` | 当日条目不足此数时自动放宽到 24 小时并在文件里注明 |
| `--stdout` | 关 | 只打印 Markdown、不写文件，用于预览或排错 |
| `--proxy URL` | 自动 | 强制指定代理，如 `http://127.0.0.1:7890` |
| `--no-proxy` | 关 | 强制直连 |
| `--no-lock` | 关 | 跳过运行锁 |
| `--quiet` | 关 | 只输出最终摘要 |
| `--list-sources` | — | 列出内置源的 key、名称、地区、地址 |

常用组合：

```bash
# 只抓 BBC 与 Al Jazeera，先看效果
python skills/daily-world-news-crawler/scripts/crawl_news.py --sources bbc,aljazeera --stdout --limit 5

# 覆盖近两天，每个源 8 条，写到指定文件
python skills/daily-world-news-crawler/scripts/crawl_news.py --since 48h --limit 8 --out output/world-news.md
```

## 脚本做了什么

1. 读取 `DEFAULT_SOURCES`（12 个公开 RSS/Atom 源），逐个获取 `robots.txt` 并校验目标地址是否允许抓取。
2. 按 `--delay` 对同一站点串行请求并叠加 0~20% 抖动，同时受 `--max-per-minute` 全局窗口约束；robots.txt 里的 `Crawl-delay` 更长时以它为准。
3. 每个源只请求一次 feed；遇 429/5xx 按 `Retry-After` 或指数退避重试，其余错误直接记为失败。
4. 兼容 RSS 2.0、RSS 1.0（RDF）、Atom 三种结构，抽取标题、链接、摘要、发布时间，HTML 标签清洗为纯文本。
5. 按时间窗口过滤，跨源按链接与标题去重，每个源截取最新 N 条。
6. 生成 Markdown：顶部是抓取时间、时间范围、源数量与条目数；正文按来源分组；末尾列出抓取失败的源与原因。

## 输出结构

```markdown
# 全球新闻日报 · 2026-11-08

> 抓取时间：2026-11-08 19:26:38（UTC+08:00）
> 时间范围：2026-11-08 00:00 起（当日）
> 新闻源：12 个｜抓取成功 12｜收录 102 条（跨源去重后）

## BBC News（英国）

1. **What we know so far after Krakow gun attack**
   - 摘要：Six people were killed in a random shooting in Krakow's main square…
   - 来源：BBC News（英国）
   - 发布：2026-11-08 19:22
   - 链接：<https://www.bbc.co.uk/news/articles/…>
```

## 定时运行

脚本本身是单次执行，用系统计划任务实现"每天一次"：

- Windows（任务计划程序）：

```powershell
schtasks /Create /TN "DailyWorldNews" /TR "python C:\path\to\skills\daily-world-news-crawler\scripts\crawl_news.py --outdir C:\path\to\output" /SC DAILY /ST 08:30 /F
```

- Linux / macOS（crontab）：

```bash
30 8 * * * cd /path/to/project && /usr/bin/python3 skills/daily-world-news-crawler/scripts/crawl_news.py >> output/crawl.log 2>&1
```

脚本用运行锁（输出目录下 `.crawl.lock`）避免定时任务与手动运行撞车；锁超过 60 分钟视为残留并自动接管。

## 网络与代理

`urllib` 默认只认 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。脚本额外识别 Windows 系统代理设置（注册表 `Internet Settings`），国内网络下通常无需手动传参。若定时任务的运行账户读不到代理：

- 显式传 `--proxy http://127.0.0.1:7890`；
- 或在任务环境里设置 `HTTPS_PROXY`。

配置了本地代理但代理软件没启动时，脚本会先探测端口（0.8 秒），提示"代理未在监听，已改为直连"后继续跑，不会逐个源死等超时。全部源失败时终端末尾会给出排查提示。

先跑 `--stdout --sources bbc` 单源验证网络，再跑全量。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 全部源 `URLError: timed out` / `远程主机强迫关闭` | 网络或代理问题，先按上一节确认代理 |
| 某个源 `robots.txt 不允许抓取` | 该站点禁止非搜索引擎抓取，属正常跳过，不要绕过 |
| 某个源 `解析不到任何新闻条目` | feed 结构变化或返回了错误页，用 `--stdout` 单独验证；长期失效就换源 |
| 输出条数明显偏少 | 当日新闻本就少，脚本会自动放宽到 24 小时；也可用 `--since 48h` |
| `另一个抓取任务正在运行` | 等上一轮结束，或确认无残留后加 `--no-lock` |

## 增删新闻源

编辑 `scripts/crawl_news.py` 顶部的 `DEFAULT_SOURCES`，每个源是一个 `key / name / region / url` 字典。新增前先确认三点：地址公开可访问（无需登录、无付费墙）、是 RSS 或 Atom、`robots.txt` 允许抓取。改完用 `--sources <key> --stdout` 单独验证。

不要加入需要登录、付费墙或 robots.txt 禁止的站点；也不要改成抓取文章正文——摘要只取 feed 自带字段，避免给源站带来额外负载。

## 适用范围

支持：公开 RSS/Atom 新闻源、当日或近几日的新闻列表、Markdown 本地落盘、每日定时执行。

不支持：需要登录的站点、付费墙内容、图片与正文全文下载、推送通知、图形界面、云端自动部署。部分源不提供 description，此时摘要显示为"（该源未提供摘要）"。
