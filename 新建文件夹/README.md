# daily-world-news-crawler

自动抓取多家公开新闻源的当日全球新闻，汇总成一份带日期的 Markdown 文档，保存在本地。

## 1. 项目解决什么问题

想了解当天国际上发生了什么，通常要挨个打开 BBC、卫报、半岛电视台等好几个网站，来回切换、手动翻页，费时又容易漏。

这个项目用一条命令把多家公开新闻源的**当日新闻**一起抓下来，提取标题、来源、摘要和原文链接，汇总写入一个同一天的 Markdown 文件。你不用再逐个刷网站，每天打开一个文件就能看完当天要闻；文件是纯文本，便于长期归档、检索，也方便再拿去做笔记、二创或喂给其他工具处理。

## 2. 主要功能

- **抓取公开新闻站点新闻**：内置 12 个公开 RSS/Atom 源（BBC、The Guardian、Al Jazeera、NPR、Deutsche Welle、France 24、Sky News、CBC、Anadolu Agency、CNA、ABC News (Australia)、Euronews），每条新闻提取标题、新闻来源、简要摘要、原文链接。
- **自带请求延时，礼貌爬虫**：同一站点两次请求默认间隔 2 秒并叠加随机抖动，全局最多每分钟 15 次请求；抓取前先读网站的 `robots.txt`，不被允许的地址直接跳过，站内声明了 `Crawl-delay` 时按它执行。不会高频暴力访问网站。
- **自动生成带当日日期的 Markdown 文件**：默认输出 `output/world-news-YYYY-MM-DD.md`，同一天重复运行会覆盖同名文件；跨源重复新闻自动去重，某个源抓取失败会在文末列出原因。
- **支持 Windows 定时任务**：用 `schtasks` 建一条每日任务，到点自动运行抓取，无需每天手动敲命令。

## 3. 安装方法

### ① 安装依赖

**本项目的抓取脚本只用 Python 标准库（`urllib`、`xml.etree`、`html` 等），不需要 `requests` 或任何第三方库，无需 `pip install`。**

先确认本机 Python 版本（需要 3.9 或以上）：

```bash
python --version
```

> 说明：常见爬虫教程里的 `pip install requests` 对本项目不是必需步骤。脚本没有 `import requests`，装了也用不上；若你后续自行改写成 requests 版本，再安装即可：
>
> ```bash
> pip install requests
> ```

### ② 下载本项目全部文件到本地

保持目录结构不变即可，脚本不依赖任何外部配置文件：

```
daily-world-news-crawler/
├── README.md
├── skills/daily-world-news-crawler/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   └── scripts/crawl_news.py      ← 爬虫脚本
└── output/                        ← 抓取结果存放目录（自动创建）
```

## 4. 使用方法

### 手动运行

进入 `scripts` 文件夹，在终端执行：

```bash
cd skills/daily-world-news-crawler/scripts
python crawl_news.py
```

也可以直接在项目根目录执行（效果相同，只是输出位置不同）：

```bash
python skills/daily-world-news-crawler/scripts/crawl_news.py
```

输出文件默认写到**当前目录下的 `output/`**，即：

- 在 `scripts` 目录执行 → `skills/daily-world-news-crawler/scripts/output/world-news-YYYY-MM-DD.md`
- 在项目根目录执行 → `output/world-news-YYYY-MM-DD.md`

想固定输出位置就加 `--out`：

```bash
python crawl_news.py --out D:\news\world-news.md
```

常用参数（可选，不填就用默认值）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--since` | `today` | 时间窗口：`today` / `24h` / `48h` / `7d` |
| `--limit N` | `10` | 每个新闻源最多收录条数 |
| `--sources a,b` | 全部 | 只抓指定源，如 `--sources bbc,cna` |
| `--out PATH` | `output/world-news-YYYY-MM-DD.md` | 输出文件路径（固定文件名，会覆盖） |
| `--outdir DIR` | `output` | 只指定输出目录，文件名仍按当日日期自动生成（推荐定时任务用） |
| `--stdout` | 关 | 只在终端打印、不写文件，用于预览排错 |
| `--list-sources` | — | 列出全部新闻源 |
| `--proxy URL` | 自动识别 | 指定代理，如 `http://127.0.0.1:7890` |

全量抓取一轮约 1 分钟（12 个源，每个源 1 次请求 + 1 次 robots 检查，中间有限流间隔），属正常现象，不是卡住。

> **网络前提（重要）**：这 12 个新闻源都在境外服务器。如果你的网络无法直接访问境外站点（例如国内网络），需要先启动本地代理软件。脚本会自动识别 Windows 系统代理，通常不用额外设置；也可以用参数显式指定：
>
> ```bash
> python crawl_news.py --proxy http://127.0.0.1:7890
> ```
>
> 代理软件没开、端口配置却还在时，脚本会提示"代理未在监听，已改为直连"并继续尝试，不会一直卡住。全部源失败时，终端最后会给出排查提示。

### Windows 自动定时运行

用 `schtasks` 创建每天固定时间执行的任务（命令在 PowerShell 或 CMD 中执行均可）：

```powershell
schtasks /Create /TN "DailyWorldNews" /TR "python C:\path\to\skills\daily-world-news-crawler\scripts\crawl_news.py --outdir C:\path\to\output" /SC DAILY /ST 08:30 /F
```

⚠ **使用前必须把路径替换成你本机的真实路径**：

- 把 `C:\path\to\skills\daily-world-news-crawler\scripts\crawl_news.py` 换成你电脑上 `crawl_news.py` 的实际位置；
- 把 `C:\path\to\output` 换成你希望保存日报的目录（写成 `--outdir` 让它按日期生成 `world-news-YYYY-MM-DD.md`，每天一个文件，不会覆盖；目录不存在会自动创建）；
- `python` 建议写成完整路径（如 `C:\Python313\python.exe`），否则任务运行时可能找不到解释器；
- 路径里带空格时，整段 `/TR` 值要用转义双引号包起来。

参数含义：`/TN` 任务名，`/TR` 要执行的命令，`/SC DAILY` 每天执行，`/ST 08:30` 执行时间（24 小时制），`/F` 已存在同名任务时强制覆盖。

查看、立即测试、删除：

```powershell
schtasks /Query /TN "DailyWorldNews"
schtasks /Run   /TN "DailyWorldNews"
schtasks /Delete /TN "DailyWorldNews" /F
```

⚠ **电脑开机才会执行**：这是本机计划任务，不是云服务，任务默认以当前用户身份、在你登录后才运行。关机、休眠或错过设定时间时不会补跑，需要开机后手动 `schtasks /Run` 补一次；笔记本长期合盖休眠的话，建议把时间设在常用机时段。

## 5. 输入输出示例

**输入**：执行爬虫脚本

```bash
python crawl_news.py
```

终端会逐个源打印进度，最后给出统计和文件路径：

```
开始抓取 12 个新闻源（间隔 2.0s，每分钟上限 15 次）
[1/12] BBC News … OK，25 条
[2/12] The Guardian … OK，45 条
[3/12] Al Jazeera English … OK，25 条
...
[12/12] Euronews … OK，50 条
✅ 已写入 output/world-news-2026-11-08.md（成功 12/12 个源，收录 102 条）
```

**输出**：生成 `world-news-2026-11-08.md`，文档内含多条新闻，每条都有标题、来源、摘要、新闻链接。以下节选其中四条（编号与文件内一致）：

```markdown
# 全球新闻日报 · 2026-11-08

> 抓取时间：2026-11-08 19:26:38（UTC+08:00）
> 时间范围：2026-11-08 00:00 起（当日）
> 新闻源：12 个｜抓取成功 12｜收录 102 条（跨源去重后）
> 生成工具：DailyWorldNewsCrawler 1.0

## BBC News（英国）

1. **What we know so far after Krakow gun attack**
   - 摘要：Six people were killed in a random shooting in Krakow's main square, and three bodies were later found at the home of the attacker.
   - 来源：BBC News（英国）
   - 发布：2026-11-08 19:22
   - 链接：<https://www.bbc.co.uk/news/articles/cmd7q15drwgeo?at_medium=RSS&at_campaign=rss>

2. **How the deadly shooting in Krakow unfolded**
   - 摘要：Video authenticated by BBC Verify shows the moment gunshots rang out in Krakow's main market square.
   - 来源：BBC News（英国）
   - 发布：2026-11-08 17:29
   - 链接：<https://www.bbc.co.uk/news/videos/cq74p2zpwyjmo?at_medium=RSS&at_campaign=rss>

3. **Houthis say at least 33 killed in Saudi air strike**
   - 摘要：Saudi Arabia, which leads a coalition fighting the Houthis in Yemen, has not commented.
   - 来源：BBC News（英国）
   - 发布：2026-11-08 12:03
   - 链接：<https://www.bbc.co.uk/news/articles/c65yn1v99ypmo?at_medium=RSS&at_campaign=rss>

## CNA（新加坡）

4. **Youth Olympic Games: How Singapore has fared so far and what’s ahead in coming days**
   - 摘要：Wushu exponent Ng Zhe Nin struck gold for Singapore, while table tennis player Loy Ming Ying secured silver at the Games.
   - 来源：CNA（新加坡）
   - 发布：2026-11-08 18:54
   - 链接：<https://www.channelnewsasia.com/singapore/youth-olympic-games-team-singapore-medals-6534116>
```

若某个源抓取失败，文件末尾会多出一段，说明是哪个源、什么原因：

```markdown
## 抓取异常

- Sky News（英国）：HTTP 503 Service Unavailable
```

## 6. 合规提示

- **仅抓取公开可访问页面**：只读取各站点公开的 RSS/Atom 地址，不涉及登录、付费墙、后台接口或任何非公开内容。
- **遵守网站 robots 协议**：抓取前读取并校验 `robots.txt`，声明禁止的地址一律跳过，不做绕过；站点声明 `Crawl-delay` 时按其要求放慢。
- **禁止高频大量请求**：脚本默认同一站点间隔 2 秒（含随机抖动）、全局每分钟不超过 15 次请求，每个源每轮只请求一次。请保持这些默认值，不要为了"抓得快"把 `--delay` 调到 1 秒以下或并发抓取，避免给源站造成压力。
- **商用需站点授权**：本项目仅供个人学习与日常阅读使用。新闻内容的著作权归各原媒体所有，本工具只摘录标题、简要摘要与原文链接用于索引和阅读；如需用于商业用途、批量转载、二次分发或对外提供服务，必须事先取得相应站点的授权。
- 请勿用于规避访问限制、采集非公开数据或其他违反站点服务条款与当地法律的用途。

## 7. 免责声明

本项目仅用于个人学习研究目的。爬虫访问请遵守目标网站的 `robots.txt` 协议，禁止高频暴力访问网站，商用需要自行获得站点授权。因使用本项目产生的任何后果与法律责任，由使用者自行承担。

## 📄 许可证

本项目采用 **MIT 许可证**（MIT License）开源，完整的协议文本见项目根目录下的 [`LICENSE`](LICENSE) 文件。

在 MIT 协议允许的范围内，你可以自由使用、复制、修改、合并、发布、分发本项目的代码（包括商业使用），条件是保留原版权声明与许可声明；本项目按"原样"提供，不附带任何明示或暗示的担保。

需要注意，MIT 授权的是**代码本身**；抓取到的新闻内容与站点数据，其权利仍归各原媒体所有，商用须按上一节自行取得站点授权。

> 发布前请把 `LICENSE` 文件中的 `[YOUR NAME]` 替换成你的名字（也可以留空），让版权声明生效。
