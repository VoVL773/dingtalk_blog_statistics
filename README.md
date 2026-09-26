# dingtalk工具 —— 本周博客更新统计机器人

一句话：**抓每个人的博客，自动数出本周（最近 7 天）发了几篇、都是哪几篇，然后把「谁完成了、谁没完成」发到钉钉群。**

- 名单**写死在脚本里**（`blog_reader.py` 的 `PEOPLE`），不用准备任何表格
- 直接 `python3 blog_reader.py` 就能跑；先看内容加 `--dry_run`
- 输出是**钉钉 markdown 消息**：统计在最上面、文章链接在最下面、标题可点、不显示长网址

---

## 目录

1. [第一次使用（每人做一遍）](#一第一次使用每人做一遍)
2. [日常使用](#二日常使用)
3. [它是怎么判断的](#三它是怎么判断的)
4. [输出长什么样](#四输出长什么样)
5. [参数速查](#五参数速查)
6. [改名单 / 加人删人](#六改名单--加人删人)
7. [常见问题排查](#七常见问题排查)
8. [文件说明](#八文件说明)

---

## 一、第一次使用（每人做一遍）

```bash
# 1. 装依赖（只用到一个第三方库）
pip install requests

# 2. 交互配置钉钉机器人凭据，生成 config.json（权限 600）
python3 setup_config.py
```

向导依次问三件事：

| 提示 | 怎么答 |
|---|---|
| `access_token` | 粘贴后回车（输入时终端不回显） |
| `secret` | 粘贴后回车（`SEC` 开头，输入时不回显） |
| 要不要现在发一条测试消息到群里？ | 输入 `y` 发送，其他键跳过 |

**token / secret 从哪来**：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义，
「安全设置」里勾选 **加签**，保存后就能看到 `access_token` 和 `SEC...` 开头的 `secret`。

配好之后所有脚本自动读取，命令行里不用再写凭据：

```bash
python3 setup_config.py --show      # 查看当前生效的是哪份配置（密钥打码）
python3 setup_config.py --force     # 重新配置（覆盖旧的）
python3 setup_config.py --clear -y  # 清空配置：config.json 内容置空（文件保留）
python3 setup_config.py --delete -y # 删除配置：直接删掉 config.json
python3 setup_config.py --clear     # 不带 -y 会先列出要处理的文件、问一次
```

> 清空 = 把内容写成 `{}`，脚本等价于 `echo '{}' > config.json`；
> 删除 = `rm config.json`。`--clear` / `--delete` 默认会把**所有位置**的 config.json
> （脚本目录、当前目录、`~/.config/dingtalk-robot/`、`DINGTALK_CONFIG` 指定的）都处理掉；
> 只想动其中一个就配合 `--path 文件路径` 或 `--home`。
> 清空后脚本会明确报「缺少钉钉凭据」，不会用旧的发消息；重新配置跑 `python3 setup_config.py` 即可。

---

## 二、日常使用

```bash
cd dingtalk工具

python3 blog_reader.py --dry_run    # ① 先预览：看谁完成、谁没完成、各几篇（不发消息）
python3 blog_reader.py              # ② 确认无误，正式发送
```

就这两步，没有表格要维护。常用变化：

```bash
python3 blog_reader.py --days 10 --dry_run    # 统计最近 10 天（默认 7）
python3 blog_reader.py --max_posts 10         # 每人最多列 10 篇（默认 5）
python3 blog_reader.py --text                 # 改成纯文本发送（会显示完整网址）
```

> 想定时自动发？写成 crontab，每周一 9:00：
> `0 9 * * 1 cd /home/你/dingtalk工具 && python3 blog_reader.py >> run.log 2>&1`

---

## 三、它是怎么判断的

对名单里的每个人，脚本去抓他的**博客首页**，按下面的顺序找文章和日期：

| 顺序 | 来源 | 说明 |
|---|---|---|
| 1 | **订阅源** | 自动找 `feed` / `rss.xml` / `atom.xml` / `index.xml` 等，标题和发布时间最准 |
| 2 | **首页 / 列表页** | ① 链接文字或 URL 里带日期（`2026/09/20/xxx`）；② 链接旁边带 `<time datetime="...">`（Astro、Hugo 等主题）——按出现顺序配对 |
| 3 | **sitemap** | 前两种都没结果时才用（sitemap 的 `lastmod` 常是整站构建时间，容易误判，所以放后面） |
| 4 | **文章页** | 还没有就翻两页，读里面的 `article:published_time` / `<time datetime>` |
| 5 | **JS / JSON 兜底** | 纯前端渲染的博客：扫描页面内联脚本和它的 JS 打包文件，从 `date: 2026-09-19`、`"date":"..."` 或内联的 Markdown 前置信息里取日期+标题；能对上 slug 的还会拼出文章链接（含 `#/posts/xxx` 这种 hash 路由） |

日期能识别的写法：`2026-09-24`、`2026/09/24`、`2026.09.24`、`2026年9月24日`、
RSS 的 `Wed, 24 Sep 2026 10:00:00 GMT`、`Sep 24, 2026`、`24 Sep 2026`。

判定规则：

- **发布时间在最近 N 天（默认 7 天）内**的文章 → 计入本周产出，篇数 +1
- 某人有 ≥1 篇 → **已完成**；0 篇 → **未完成**（消息里点名，有手机号就 @）
- 抓不到日期（打不开 / 没有订阅源 / 页面上没有日期）→ **无法判断**，单独列出原因，**默认不 @**

会自动排除**标签页、分类页、归档页、分页**（`/tags/xxx/`、`/page/2/`、`/posts/` 这类），
同一篇文章的两种写法（URL 编码 / 未编码、大小写、末尾斜杠）只算一篇。

> 实测覆盖：Hexo / Hugo / Jekyll / Astro / VitePress 等静态站靠订阅源或首页即可；
> Astro 把日期放在 `<time>` 里的、以及把文章内联进 JS 的 React/Vue SPA（`#/posts/xxx`）也能读到。
> 仍可能「无法判断」的情况：站点在当前网络打不开（Vercel 域名常超时）、页面完全没有任何日期信息。

---

## 四、输出长什么样

默认发**钉钉 markdown 消息**：统计信息在最上面，文章链接在最下面，标题做成可点链接、不显示长网址。

```markdown
@10000000 @10000000 @10000000

本周博客更新统计（09-19 ~ 09-26）：共 13 篇，11/15 人完成

- 已完成（12 人）
- 2篇：邓** 靳**
- 1篇：王** 郑** 班** 田** 龚** 闫** 骆** 桑** 曾** 武**
- 未完成（3 人）：单**、冉**、安**
- 无法判断（1 人）：桑**（博客打不开（超时））

文章链接

【邓** 2 篇】

- [09-24 《技术 📅 大模型"捡数据"，机器人"造数据"：具身智能的数据困局》](https://cisyam555.github.io/posts/embodied-data-bottleneck/)
- [09-19 《技术 📅 ROS 2 话题通信的 2 个静默陷阱》](https://cisyam555.github.io/posts/ros2-topic-silent-failures/)

【靳** 2 篇】

- [09-30 《Label Studio 数据标注完整教程》](https://cgmoke.github.io/coding/Label%20Studio%20…)
```

要点：

- 第一行 `@手机号`：钉钉要求正文里出现 `@手机号`，客户端才会渲染成 `@姓名` 并强提醒
- 统计区只写**姓名 + 篇数**，且**按篇数分行**：`2篇：邓** 靳**` / `1篇：王** 郑** …`（篇数多的在前，同一行里按名单顺序，用空格隔开）
- 链接区按同一顺序分组：`【姓名 N 篇】` + 每篇 `- [日期 《标题》](网址)`
- 每人最多列 5 篇（`--max_posts`），超出显示「…另有 N 篇没列出」，**篇数统计不受影响**

> 排版注意（踩过的两个坑）：
> 1. 钉钉的 markdown **只认「列表项 `- `」和「空行分段」**，单独一个换行会被吃掉，全挤成一坨。
>    所以统计信息都写成列表项，`【姓名 N 篇】` 和链接之间用空行分段。
> 2. `**加粗**` 在部分钉钉客户端里会**原样显示星号**，所以默认不加粗，人名用 `【】` 区分；
>    如果你的客户端能正常渲染加粗，加 `--bold` 就会重新套上 `**`。

---

## 五、参数速查

```bash
python3 blog_reader.py                  # 直接发（名单用脚本内置的 PEOPLE）
python3 blog_reader.py --dry_run        # 只预览，不发
python3 blog_reader.py --days 10         # 统计窗口改成 10 天（默认 7）
python3 blog_reader.py --max_posts 10    # 每人最多列 10 篇
python3 blog_reader.py --text            # 纯文本发送（会显示完整网址）
python3 blog_reader.py --bold            # 标签加 ** 加粗（客户端支持时才用）
python3 blog_reader.py --at_unknown      # 「无法判断」的人也一起 @
python3 blog_reader.py --no_inline_at    # 不在消息里写 @手机号（就不会有 @ 提醒）
python3 blog_reader.py --is_at_all       # 顺便 @所有人
python3 blog_reader.py --at_mobiles 10000000   # 额外 @ 别人
python3 blog_reader.py --timeout 10 --workers 8 --retries 1   # 网络参数
python3 blog_reader.py -f 新名单.xlsx     # 可选：临时改用一份 Excel 名单（表头要有「姓名」「博客链接」）
```

改文案（都支持占位符）：

```bash
python3 blog_reader.py --header '本周更新（{start} ~ {end}）：{posts} 篇，{done}/{total} 人完成'
python3 blog_reader.py --done_prefix '写了的人（{n} 人）'
python3 blog_reader.py --undone_prefix '没写的（{n} 人）'
python3 blog_reader.py --unknown_prefix '抓不到的（{n} 人）'
```

---

## 六、改名单 / 加人删人

名单就在 `blog_reader.py` 顶部的 `PEOPLE` 里：

```python
PEOPLE = [
    {"name": "王**", "mobile": "10000000", "blog": "https://kingdream-cn.github.io/"},
    {"name": "邓**", "mobile": "10000000", "blog": "https://cisyam555.github.io"},
    ...
]
```

- `name` 姓名（必填）、`mobile` 手机号（可留空，留空就不 @）、`blog` 博客首页地址
- 加人：照抄一行改掉三个值；删人：删掉那一行
- 手机号必须是**本人钉钉绑定**的号码，否则 @ 不会提醒（钉钉只认手机号 / userid，不认中文名）
- 改完存盘直接跑，`--dry_run` 看一眼对不对

---

## 七、常见问题排查

| 现象 | 原因 / 解决 |
|---|---|
| 某人的篇数明显偏多 | 他的博客首页/订阅源把标签页也列进去了。看终端明细里有没有 `/tags/`、`/categories/` 之类的链接，反馈给我再加排除规则 |
| 某人明明写了却显示「未完成」 | 终端会显示他「最新一篇 X 月 X 日」。若确实在窗口内却没算上，多半是日期藏在图片/JS 里：把站点发我，我加一条解析规则；也可以先用 `--days 10` 放宽窗口 |
| 某人显示「无法判断」 | 看终端原因：`博客打不开（超时）` 是网络问题（Vercel 域名常超时，可加大 `--timeout`）；`没找到日期` 是页面上确实没有日期信息 |
| 手机号没生效 / 没被 @ | `PEOPLE` 里的 `mobile` 必须是他钉钉绑定的号码；留空则只写名字 |
| `缺少钉钉凭据：请先运行一次 python3 setup_config.py` | 还没配置。跑一次向导，或临时 `--access_token/--secret` 传一次 |
| `errcode 310000` / `300001` / `300005` | secret 不对 / 安全设置没勾「加签」/ access_token 不存在 |
| 想清空 / 换掉配置 | `python3 setup_config.py --clear -y`（清空）或 `--delete -y`（删掉）或 `--force`（直接用新凭据覆盖），详见[第一节](#一第一次使用每人做一遍) |
| 消息挤成一坨、或出现 `**` 星号 | 见[第四节](#四输出长什么样)的「排版注意」：只用列表项和空行分段；加粗要客户端支持才加 `--bold` |
| 跑得有点慢 | 每人要抓 1~6 个页面，15 人约 40~60 秒。可加大 `--workers 12`、减小 `--timeout 6` |
| `ModuleNotFoundError: requests` | `pip install requests` |
| 终端提示没装 openpyxl | 不是错误：内置解析器照样能读 xlsx（只有用 `-f` 时才需要）；想更快 `pip install openpyxl` |

---

## 八、文件说明

```
dingtalk工具/
├── blog_reader.py      # 主程序：内置名单 + 自动统计 + 发钉钉
├── dingtalk_robot.py   # 底层发送（加签 / @ / markdown / 读文档当消息）
├── setup_config.py     # 配置向导（第一次、换人时跑一次）
├── dingtalk_config.py  # 凭据读取与保存的公共模块
├── config.json         # 配置向导生成（600 权限）
└── README.md           # 本说明
```

单发一条消息（一般用不到）：

```bash
python3 dingtalk_robot.py --msg "大家好"                    # 纯文本
python3 dingtalk_robot.py --msg "**大家好**" --markdown      # markdown
python3 dingtalk_robot.py -f 通知.md --at_mobiles 10000000  # 把文档内容当消息发
```

依赖：Python 3.6+ 与 `requests`。

**分发提醒**：给别人这份目录之前先清掉自己的密钥 —— `python3 setup_config.py --delete -y`
（等价于 `rm config.json`），让对方自己跑一次 `python3 setup_config.py`。
没配凭据时脚本会明确报错，不会误用你的机器人。
