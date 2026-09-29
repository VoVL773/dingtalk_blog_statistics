#!/usr/bin/env python
"""
自动统计「本周谁更新了博客、各写了几篇」-> 发到钉钉群。

名单直接写在下面 PEOPLE 里（不用再准备 Excel），直接运行即可：

    python3 blog_reader.py --dry_run     # 先预览，不发
    python3 blog_reader.py               # 正式发送
    python3 blog_reader.py --days 7      # 改统计窗口（默认最近 7 天）

统计方式：逐个抓博客（优先订阅源，其次 sitemap，再其次首页/文章页），
把页面、标题、URL 里能拿到的日期都解析出来，落在最近 N 天内的文章算「本周产出」，
按人统计篇数并列出链接；一篇都没有 = 未完成；抓不到日期 = 无法判断（单独列出原因）。

输出大致长这样：
    @10000000 @10000000
    本周博客更新统计（09-19 ~ 09-26）：共 12 篇，10/15 人完成
    已完成（10 人）：
    1) 王** 2 篇
    09-24 《标题A》
    https://...
    09-22 《标题B》
    https://...
    2) 邓**1 篇
    ...
    未完成（5 人）：张三、李四
    无法判断（1 人）：王五（博客打不开（超时））

本脚本只看「谁更新了博客」：不抓正文、不做推荐、不记历史。
凭据：先运行一次 python3 setup_config.py，之后自动读取（命令行 > 环境变量 > config.json > 内置模板）。
"""

import argparse
import concurrent.futures
import email.utils
import logging
import os
import re
import sys
import time
import zipfile
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree as ET
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from dingtalk_robot import send_custom_robot_group_message
except ImportError:  # 两个文件被拆开时会走到这里
    send_custom_robot_group_message = None

try:
    import dingtalk_config
except ImportError:
    dingtalk_config = None

# ===========================================================================
# 名单：由「博客链接.xlsx」整理而来，写死在代码里，之后不用再带表格。
# 增删改人直接改这个列表：name 姓名 / mobile 手机号（可留空）/ blog 博客地址。
# ===========================================================================
PEOPLE = [
    {"name": '王**', "mobile": '10000000', "blog": 'https://kingdream-cn.github.io/'},
    {"name": '邓**', "mobile": '10000000', "blog": 'https://cisyam555.github.io'},
    {"name": '郑**', "mobile": '10000000', "blog": 'https://cherish-hs.github.io'},
    {"name": '靳**', "mobile": '10000000', "blog": 'https://CGmoke.github.io/'},
    {"name": '班**', "mobile": '10000000', "blog": 'https://elac1.github.io'},
    {"name": '田**', "mobile": '10000000', "blog": 'https://vovl773.github.io/'},
    {"name": '龚**', "mobile": '10000000', "blog": 'https://gong18598575562.github.io/My-Blog.github.io/'},
    {"name": '闫**', "mobile": '10000000', "blog": 'https://wbxxmz.github.io/my-boke/'},
    {"name": '单**', "mobile": '10000000', "blog": 'https://kkka20.github.io'},
    {"name": '冉**', "mobile": '10000000', "blog": 'https://ranmengjia.github.io/astro-navfolio/'},
    {"name": '骆**', "mobile": '10000000', "blog": 'https://yubin0813.github.io'},
    {"name": '桑**', "mobile": '10000000', "blog": 'https://asang712.github.io/'},
    {"name": '曾**', "mobile": '10000000', "blog": 'https://zeng417.github.io'},
    {"name": '安**', "mobile": '10000000', "blog": 'https://ajxing123.github.io/'},
    {"name": '武**', "mobile": '10000000', "blog": 'https://wsb-666.github.io/my-blog/'},
]

DEFAULT_DAYS = 7
DEFAULT_HEADER = '本周博客更新统计（{start} ~ {end}）：共 {posts} 篇，{done}/{total} 人完成'
DEFAULT_DONE_PREFIX = '已完成（{n} 人）：'
DEFAULT_UNDONE_PREFIX = '未完成（{n} 人）：'
DEFAULT_UNKNOWN_PREFIX = '无法判断（{n} 人）：'
DEFAULT_TIMEOUT = 10
DEFAULT_WORKERS = 8
DEFAULT_MAX_POSTS = 5       # 每个人最多列出几篇（篇数统计不受影响）
MAX_TITLE_LEN = 60
MAX_WEEK_POSTS = 30         # 单人本周最多收集多少篇，防止页面异常导致刷屏
MAX_FEED_TRIES = 3
MAX_POST_PROBES = 2
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'
)
COMMON_FEED_PATHS = ('feed', 'feed.xml', 'rss.xml', 'atom.xml', 'index.xml', 'rss', 'feed/atom')
FEED_TYPE_HINTS = ('rss', 'atom', 'feed')

XLSX_HEADER_RULES = (
    ('output', ('本周博客产出链接', '本周博客产出', '本周产出', '产出', 'output')),
    ('name', ('姓名', '名字', '人名', '同学', 'name')),
    ('mobile', ('手机号', '手机', '电话', '联系方式', 'mobile', 'phone')),
    ('blog', ('博客链接', '博客地址', '博客网址', '网址', '链接', 'blog', 'url')),
)
BLANK_VALUES = ('', '-', '—', '/', '无', '空', 'none', 'null', 'n/a')
# 这些目录明显不是文章（标签页/分类页/分页/归档等），统计时要排除掉
EXCLUDE_SEGMENTS = {'tag', 'tags', 'category', 'categories', 'archive', 'archives', 'page',
                    'author', 'authors', 'search', 'about', 'feed', 'rss', 'atom', 'sitemap',
                    'index', '404', 'links', 'link', 'comment', 'comments', 'shuoshuo', 'memo'}
# 只有「最后一段」是这些名字时才算列表页（/posts/ 是列表，/posts/xxx/ 是文章）
EXCLUDE_LAST_SEGMENTS = {'posts', 'post', 'blog', 'blogs', 'articles', 'article', 'list', 'all'}
PHONE_RE = re.compile(r'^\+?\d{6,20}$')


def setup_logger():
    logger = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter('%(asctime)s %(name)-8s %(levelname)-8s %(message)s [%(filename)s:%(lineno)d]'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def define_options():
    parser = argparse.ArgumentParser(description='统计本周谁更新了博客、各几篇，并发到钉钉群（名单内置在脚本里）')
    parser.add_argument('-f', '--file', dest='file', default=None,
                        help='可选：改用一份 Excel 表格里的名单（不写就用脚本内置的 PEOPLE）')
    parser.add_argument('--days', dest='days', type=int, default=DEFAULT_DAYS,
                        help='统计窗口天数，默认 %d 天' % DEFAULT_DAYS)
    parser.add_argument('--header', dest='header', default=DEFAULT_HEADER,
                        help='消息标题行，可用 {start}/{end}/{posts}/{done}/{total}；传空字符串则不显示')
    parser.add_argument('--done_prefix', dest='done_prefix', default=DEFAULT_DONE_PREFIX,
                        help='已完成那一段的标题（可用 {n}/{posts}）')
    parser.add_argument('--undone_prefix', dest='undone_prefix', default=DEFAULT_UNDONE_PREFIX,
                        help='未完成那一段的标题（可用 {n}）')
    parser.add_argument('--unknown_prefix', dest='unknown_prefix', default=DEFAULT_UNKNOWN_PREFIX,
                        help='无法判断那一段的标题（可用 {n}）')
    parser.add_argument('--max_posts', dest='max_posts', type=int, default=DEFAULT_MAX_POSTS,
                        help='每个人最多列出几篇的链接（默认 %d，篇数统计不受影响）' % DEFAULT_MAX_POSTS)
    parser.add_argument('--dry_run', '--dry-run', dest='dry_run', action='store_true',
                        help='只在终端打印将要发送的内容，不真的发到钉钉')
    parser.add_argument('--text', dest='text', action='store_true',
                        help='按纯文本发送（默认发 markdown：标题做成可点链接、网址不显示）')
    parser.add_argument('--bold', dest='bold', action='store_true',
                        help='给标签加 ** 加粗（默认不加：部分钉钉客户端会把 ** 原样显示出来）')
    parser.add_argument('--timeout', dest='timeout', type=float, default=DEFAULT_TIMEOUT,
                        help='单个请求的超时秒数（默认 %d）' % DEFAULT_TIMEOUT)
    parser.add_argument('--workers', dest='workers', type=int, default=DEFAULT_WORKERS,
                        help='并发检查的人数（默认 %d）' % DEFAULT_WORKERS)
    parser.add_argument('--retries', dest='retries', type=int, default=1,
                        help='请求失败后的重试次数（默认 1）')
    parser.add_argument('--access_token', dest='access_token', default=None,
                        help='覆盖钉钉 access_token（默认读 config.json / 环境变量）')
    parser.add_argument('--secret', dest='secret', default=None, help='覆盖钉钉 secret')
    parser.add_argument('--userid', dest='userid', help='额外 @ 的钉钉用户ID，多个用逗号分隔')
    parser.add_argument('--at_mobiles', dest='at_mobiles', help='额外 @ 的手机号，多个用逗号分隔')
    parser.add_argument('--at_unknown', dest='at_unknown', action='store_true',
                        help='把「无法判断」的人也一起 @（默认不 @，只在消息里列出）')
    parser.add_argument('--no_inline_at', dest='no_inline_at', action='store_true',
                        help='不在消息里写 @手机号（钉钉客户端就不会渲染成 @姓名 提醒）')
    parser.add_argument('--is_at_all', dest='is_at_all', action='store_true', help='是否@所有人')
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 名单：默认用脚本内置的 PEOPLE，也可以用 -f 临时读一份 Excel
# ---------------------------------------------------------------------------

XLSX_NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
XLSX_RID_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def _xlsx_cell_text(cell, shared):
    cell_type = cell.get('t')
    if cell_type == 's':
        node = cell.find(XLSX_NS + 'v')
        if node is None or node.text is None:
            return ''
        try:
            return shared[int(node.text)]
        except (ValueError, IndexError):
            return ''
    if cell_type == 'inlineStr':
        node = cell.find(XLSX_NS + 'is')
        return ''.join(t.text or '' for t in node.iter(XLSX_NS + 't')) if node is not None else ''
    node = cell.find(XLSX_NS + 'v')
    return node.text if node is not None and node.text else ''


def _xlsx_column_index(ref):
    letters = re.match(r'[A-Z]+', (ref or 'A').upper()).group()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def _xlsx_sheet_path(zf):
    try:
        workbook = ET.fromstring(zf.read('xl/workbook.xml'))
        rels = ET.fromstring(zf.read('xl/_rels/workbook.xml.rels'))
        rid_to_target = {r.get('Id'): r.get('Target') for r in rels}
        for sheet in workbook.iter(XLSX_NS + 'sheet'):
            target = rid_to_target.get(sheet.get(XLSX_RID_NS + 'id'))
            if target:
                return target.lstrip('/') if target.startswith('/') else 'xl/' + target.lstrip('/')
    except Exception:
        pass
    return 'xl/worksheets/sheet1.xml'


def read_xlsx_rows(path):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        logging.error('找不到 Excel 文件：%s', path)
        sys.exit(2)
    try:
        import openpyxl
    except ImportError:
        openpyxl = None

    if openpyxl is not None:
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
        try:
            return [['' if c is None else str(c).strip() for c in row]
                    for row in workbook.worksheets[0].iter_rows(values_only=True)]
        finally:
            workbook.close()

    try:
        with zipfile.ZipFile(path) as zf:
            shared = []
            if 'xl/sharedStrings.xml' in zf.namelist():
                root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
                shared = [''.join(t.text or '' for t in si.iter(XLSX_NS + 't'))
                          for si in root.iter(XLSX_NS + 'si')]
            root = ET.fromstring(zf.read(_xlsx_sheet_path(zf)))
    except (zipfile.BadZipFile, KeyError) as e:
        logging.error('这个文件不是有效的 xlsx（或已损坏）：%s（%s）', path, e)
        sys.exit(2)

    rows = []
    for row in root.iter(XLSX_NS + 'row'):
        cells = {}
        for cell in row.iter(XLSX_NS + 'c'):
            value = _xlsx_cell_text(cell, shared).strip()
            if value:
                cells[_xlsx_column_index(cell.get('r'))] = value
        rows.append([cells.get(i, '') for i in range(max(cells) + 1)] if cells else [])
    return rows


def map_xlsx_columns(header_row):
    columns = {}
    for index, raw in enumerate(header_row):
        header = str(raw).strip().lower().replace(' ', '')
        if not header:
            continue
        for field, aliases in XLSX_HEADER_RULES:
            if field in columns:
                continue
            if any(alias.lower() in header for alias in aliases):
                columns[field] = index
                break
    return columns


def _xlsx_cell(row, columns, field):
    index = columns.get(field)
    if index is None or index >= len(row):
        return ''
    return str(row[index]).strip()


def is_blank(value):
    return value.strip().lower() in BLANK_VALUES


def _clean_mobile_cell(value):
    value = value.strip()
    if not value:
        return ''
    if re.fullmatch(r'\d+\.0+', value):
        value = value.split('.')[0]
    if re.search(r'[eE]', value):
        try:
            value = str(int(float(value)))
        except ValueError:
            return value
    return value


def normalize_mobile(value):
    value = value.strip().lstrip('+')
    if value.startswith('86') and len(value) == 13:
        value = value[2:]
    return value


def _normalize_blog(url):
    url = (url or '').strip()
    if not url:
        return ''
    if not url.lower().startswith(('http://', 'https://')):
        url = 'https://' + url.lstrip('/')
    return url


def people_from_xlsx(path):
    rows = read_xlsx_rows(path)
    if not rows:
        logging.error('Excel 里没有任何内容：%s', path)
        sys.exit(2)
    columns = map_xlsx_columns(rows[0])
    if 'name' not in columns or 'blog' not in columns:
        logging.error('Excel 第一行必须是有「姓名」和「博客链接」列的表头，识别到的是：%s', rows[0])
        sys.exit(2)

    people = []
    for row in rows[1:]:
        name = _xlsx_cell(row, columns, 'name')
        if not name or name.startswith('#'):
            continue
        mobile = normalize_mobile(_clean_mobile_cell(_xlsx_cell(row, columns, 'mobile')))
        people.append({'name': name,
                       'mobile': mobile if PHONE_RE.match(mobile) else '',
                       'blog': _normalize_blog(_xlsx_cell(row, columns, 'blog'))})
    return people


def load_people(path=None):
    """返回 [{'name','mobile','blog'}]：默认用脚本内置 PEOPLE，给了 -f 就用表格。"""
    if path:
        logging.info('-f 指定了表格，这次用 %s 里的名单', path)
        source = people_from_xlsx(path)
    else:
        logging.info('使用脚本内置名单（PEOPLE，共 %d 人）', len(PEOPLE))
        source = PEOPLE

    people, seen = [], set()
    for entry in source:
        name = str(entry.get('name', '')).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        blog = _normalize_blog(entry.get('blog', ''))
        mobile = normalize_mobile(str(entry.get('mobile') or ''))
        person = {'name': name, 'mobile': mobile if PHONE_RE.match(mobile) else '', 'blog': blog}
        if not blog or is_blank(blog):
            person['error'] = '脚本名单里没填博客链接'
        people.append(person)
    return people


# ---------------------------------------------------------------------------
# 日期解析
# ---------------------------------------------------------------------------

MONTHS = {name: index + 1 for index, name in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))}


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _to_local_date(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.date()


def parse_date(text):
    """从任意文本里尽力抽一个日期，失败返回 None。"""
    if not text:
        return None
    text = str(text).strip()
    if not text:
        return None

    # 数字型：2026-09-24 / 2026/09/24 / 2026.09.24 / 2026年9月24日
    match = re.search(r'(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?', text)
    if match:
        found = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if found:
            return found

    # RFC822（RSS 的 pubDate）等标准格式
    try:
        found = _to_local_date(email.utils.parsedate_to_datetime(text))
        if found:
            return found
    except (TypeError, ValueError, IndexError):
        pass

    # 英文月份名：Sep 24, 2026 / 24 Sep 2026
    match = re.search(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b', text)
    if match:
        month = MONTHS.get(match.group(1)[:3].lower())
        if month:
            found = _safe_date(int(match.group(3)), month, int(match.group(2)))
            if found:
                return found
    match = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})\b', text)
    if match:
        month = MONTHS.get(match.group(2)[:3].lower())
        if month:
            found = _safe_date(int(match.group(3)), month, int(match.group(1)))
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def fetch(url, timeout, retries=1):
    """抓一个 URL。返回 (响应文本, 错误说明)；错误说明为 None 表示成功。"""
    error = None
    attempt = 0
    while True:
        try:
            resp = requests.get(
                url, timeout=timeout, allow_redirects=True,
                headers={'User-Agent': USER_AGENT,
                         'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                         'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'})
            if resp.status_code >= 500 and attempt < retries:
                error = 'HTTP %d' % resp.status_code
                attempt += 1
                time.sleep(1.0 + attempt)
                continue
            if resp.status_code >= 400:
                return None, 'HTTP %d' % resp.status_code
            if not resp.encoding or resp.encoding.lower() in ('iso-8859-1', 'ascii'):
                resp.encoding = resp.apparent_encoding or 'utf-8'
            return resp.text, None
        except requests.exceptions.Timeout:
            error = '超时'
        except requests.exceptions.SSLError:
            error = '证书错误'
        except requests.exceptions.RequestException as e:
            error = '连接失败(%s)' % e.__class__.__name__
        if attempt >= retries:
            return None, error
        attempt += 1
        time.sleep(1.0 + attempt)


# ---------------------------------------------------------------------------
# 订阅源 / sitemap / 页面解析
# ---------------------------------------------------------------------------

def _attr(tag_text, name):
    match = re.search(r'\b%s\s*=\s*["\']([^"\']*)["\']' % name, tag_text, re.I)
    return match.group(1).strip() if match else None


def find_feed_links(html, base_url):
    feeds = []
    for tag in re.findall(r'<link\b[^>]*>', html, re.I):
        rel = (_attr(tag, 'rel') or '').lower()
        type_ = (_attr(tag, 'type') or '').lower()
        href = _attr(tag, 'href')
        if href and 'alternate' in rel and any(hint in type_ for hint in FEED_TYPE_HINTS):
            url = urljoin(base_url, href)
            if url not in feeds:
                feeds.append(url)
    return feeds


def _tag_name(element):
    return element.tag.split('}')[-1].lower()


def _host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith('www.') else host


def same_site(url, base_url):
    """判断是不是同一个站点（有些主题的订阅源里还留着 example.com 演示文章）。"""
    a, b = _host(url), _host(base_url)
    if not a or not b:
        return False
    return a == b or a.endswith('.' + b) or b.endswith('.' + a)


def parse_feed(text, limit=100):
    """解析 RSS / Atom，返回 [{'title','link','date'}]。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    entries = []
    for element in root.iter():
        if _tag_name(element) not in ('item', 'entry'):
            continue
        title, link, published, updated = None, None, None, None
        for child in element:
            name = _tag_name(child)
            text_value = (child.text or '').strip()
            if name == 'title' and text_value:
                title = ' '.join(text_value.split())
            elif name == 'link':
                rel = (child.get('rel') or 'alternate').lower()
                href = child.get('href') or text_value
                if href and (link is None or rel == 'alternate'):
                    link = href
            elif name in ('pubdate', 'published', 'issued', 'date', 'created') and text_value:
                published = published or text_value
            elif name in ('updated', 'modified') and text_value:
                updated = updated or text_value
        when = parse_date(published) or parse_date(updated) or parse_date(title)
        if when:
            entries.append({'title': title, 'link': link, 'date': when})
        if len(entries) >= limit:
            break
    return entries


def parse_sitemap(text, limit=2000):
    """解析 sitemap，返回 (子 sitemap 列表, [{'loc','date'}])。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], []
    children, entries = [], []
    for element in root.iter():
        name = _tag_name(element)
        if name == 'sitemap':
            loc = None
            for child in element:
                if _tag_name(child) == 'loc' and child.text:
                    loc = child.text.strip()
            if loc:
                children.append(loc)
        elif name == 'url':
            loc, lastmod = None, None
            for child in element:
                child_name = _tag_name(child)
                if child_name == 'loc' and child.text:
                    loc = child.text.strip()
                elif child_name in ('lastmod', 'pubdate') and child.text:
                    lastmod = child.text.strip()
            when = parse_date(loc) or parse_date(lastmod)
            if loc and when:
                entries.append({'loc': loc, 'date': when})
                if len(entries) >= limit:
                    break
    return children, entries


class _PageScanner(HTMLParser):
    """收集页面里的 <time datetime>、meta 时间、以及「链接 + 链接文字」。"""

    DATE_META = ('article:published_time', 'article:modified_time', 'og:updated_time',
                 'datepublished', 'datemodified', 'pubdate', 'publishdate', 'date')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.times = []
        self.metas = []
        self.anchors = []
        self.events = []          # 按出现顺序：('anchor', href, 文字) / ('time', 时间)
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        data = {k.lower(): (v or '') for k, v in attrs}
        if tag == 'time' and data.get('datetime'):
            self.times.append(data['datetime'])
            self.events.append(('time', data['datetime']))
        elif tag == 'meta':
            key = (data.get('property') or data.get('name') or '').lower()
            if key in self.DATE_META and data.get('content'):
                self.metas.append(data['content'])
        elif tag == 'a' and data.get('href'):
            self._href = data['href']
            self._buf = []

    def handle_endtag(self, tag):
        if tag == 'a' and self._href:
            text = ' '.join(''.join(self._buf).split())
            self.anchors.append((self._href, text))
            self.events.append(('anchor', self._href, text))
            self._href, self._buf = None, []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)


def scan_html(html):
    scanner = _PageScanner()
    try:
        scanner.feed(html)
    except Exception:
        pass
    return scanner


def same_site_post_url(url, base_url):
    """
    站点挂在子路径下时（GitHub Pages 项目站），页面里有时会写成根相对链接，
    例如博客在 /astro-navfolio/ 却写 href="/blog/xxx/"（实测 404）。
    这里把这些链接补回站点前缀，例如 /blog/xxx -> /astro-navfolio/blog/xxx。
    """
    base = urlparse(base_url)
    parts = urlparse(url)
    if parts.netloc != base.netloc:
        return url
    base_path = base.path.strip('/')
    if not base_path or parts.path.strip('/').startswith(base_path):
        return url
    fixed = urljoin(base_url, parts.path.lstrip('/'))
    if parts.query:
        fixed += '?' + parts.query
    if parts.fragment:
        fixed += '#' + parts.fragment
    return fixed


def page_signals(html, base_url):
    """返回 (页面上的日期列表, [(链接, 日期, 标题)])"""
    scanner = scan_html(html)
    dates = [d for d in (parse_date(v) for v in scanner.metas + scanner.times) if d]

    candidates = []
    for href, text in scanner.anchors:
        url = urljoin(base_url, href)
        if not url.lower().startswith(('http://', 'https://')):
            continue
        when = parse_date(text) or parse_date(url)
        if when:
            title = re.sub(r'20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?', '', text).strip()
            candidates.append({'url': same_site_post_url(url, base_url), 'date': when, 'title': title or None})

    # 链接文字里没有日期、但旁边有 <time datetime> 的（Astro / Hugo 等主题很常见）：
    # 把每个 <time> 配给最近的「本站的文章链接」——必须同时满足同站 + 像文章，
    # 否则页脚/导航里的外链（比如 GitHub 链接）会在距离并列时被误配走
    anchor_events = []
    for anchor_index, event in enumerate(scanner.events):
        if event[0] != 'anchor':
            continue
        url = same_site_post_url(urljoin(base_url, event[1]), base_url)
        if same_site(url, base_url) and looks_like_post(url, base_url):
            anchor_events.append((anchor_index, url, event[2]))

    known = {c['url'] for c in candidates}
    for index, event in enumerate(scanner.events):
        if event[0] != 'time':
            continue
        when = parse_date(event[1])
        if not when:
            continue
        best, best_gap = None, 9
        for anchor_index, url, text in anchor_events:
            gap = abs(anchor_index - index)
            if gap <= 8 and gap < best_gap:
                best, best_gap = (url, text), gap
        if not best:
            continue
        url, text = best
        if url in known:
            continue
        known.add(url)
        candidates.append({'url': url, 'date': when, 'title': text or None})
    return dates, candidates


SCRIPT_DATE_RE = re.compile(r'["\']?date["\']?\s*[:=]\s*["\'`]?\s*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})', re.I)
SCRIPT_TITLE_RE = re.compile(r'["\']?title["\']?\s*[:=]\s*["\'`]([^"\'`\n]{2,80})')
SLUG_MAP_RE = re.compile(r'["\']([^"\']*?([^"\'/]+)\.md)["\']\s*:\s*([A-Za-z_$][\w$]*)')


def scan_script_dates(text, limit=200):
    """
    从 JS / JSON 里挖「date: 2026-09-19」「"date":"2026-09-19"」这类日期，
    顺便带出最近的标题，以及（Vite 里形如 e0=`--- title: .. date: .. `）内容变量名。
    """
    results = []
    for match in SCRIPT_DATE_RE.finditer(text):
        when = parse_date(match.group(1))
        if not when:
            continue
        window = text[max(0, match.start() - 500):match.start() + 100]
        titles = SCRIPT_TITLE_RE.findall(window)
        var = None
        tick = text.rfind('`', max(0, match.start() - 3000), match.start())
        if tick != -1:
            var_match = re.search(r'([A-Za-z_$][\w$]*)\s*=\s*$', text[max(0, tick - 40):tick])
            if var_match:
                var = var_match.group(1)
        results.append({'date': when, 'title': titles[-1].strip() if titles else None, 'var': var})
        if len(results) >= limit:
            break
    return results


FRONT_MATTER_RE = re.compile(r'---\s*\n(.*?)\n---', re.S)


def scan_front_matter(text, limit=200):
    """找被打包进 JS 的 Markdown 前置信息（Vite 模板常把整篇 md 内联成模板字符串）。"""
    results = []
    for match in FRONT_MATTER_RE.finditer(text):
        block = match.group(1)
        date_match = re.search(r'^\s*date\s*:\s*["\']?(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})', block, re.M)
        if not date_match:
            continue
        when = parse_date(date_match.group(1))
        if not when:
            continue
        title_match = re.search(r'^\s*title\s*:\s*["\']?(.+?)["\']?\s*$', block, re.M)
        tick = text.rfind('`', max(0, match.start() - 3000), match.start())
        var = None
        if tick != -1:
            var_match = re.search(r'([A-Za-z_$][\w$]*)\s*=\s*$', text[max(0, tick - 40):tick])
            if var_match:
                var = var_match.group(1)
        results.append({'date': when, 'title': title_match.group(1).strip() if title_match else None,
                        'var': var})
        if len(results) >= limit:
            break
    return results


def spa_slug_by_var(text):
    """Vite 打包常见：{"../../posts/xxx.md": e0, ...} -> {e0: 'xxx'}"""
    mapping = {}
    for match in SLUG_MAP_RE.finditer(text):
        mapping.setdefault(match.group(3), match.group(2))
    return mapping


def spa_post_link(base_url, slug, bundle_text):
    if not slug:
        return None
    if '#/posts/:slug' in bundle_text or 'posts/:slug' in bundle_text:
        return urljoin(base_url, '#/posts/%s' % slug)
    return urljoin(base_url, 'posts/%s/' % slug)


def script_sources(html, base_url, limit=2):
    urls = []
    for match in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
        url = urljoin(base_url, match.group(1))
        if url not in urls and same_site(url, base_url):
            urls.append(url)
    return urls[:limit]


def looks_like_post(url, base_url):
    """粗略判断一个链接是不是「文章」而不是标签页/分类页/首页。"""
    if not url or url.rstrip('/') == (base_url or '').rstrip('/'):
        return False
    parts = urlparse(url)
    path = parts.path.strip('/')
    if parts.fragment and '/' in parts.fragment:
        path = parts.fragment.lstrip('#!').strip('/')  # hash 路由：#/posts/xxx
    if not path:
        return False
    segments = [seg for seg in path.split('/') if seg]
    if not segments:
        return False
    if any(seg.lower() in EXCLUDE_SEGMENTS for seg in segments):
        return False
    last = segments[-1].lower()
    if last in EXCLUDE_LAST_SEGMENTS:
        return False
    if '.' in last and not last.endswith(('.html', '.htm')):
        return False
    return True


def dedupe_key(url, title=None, when=None):
    """同一篇文章常常有两种写法（URL 编码/未编码、大小写、末尾斜杠），统一成一个键。"""
    if url:
        return unquote(url).strip().rstrip('/').lower()
    return 'no-url:%s:%s' % (title or '', when or '')


def person_note(blog_url):
    return urlparse(blog_url).netloc + urlparse(blog_url).path.rstrip('/')


def collect_week_posts(blog_url, cutoff, timeout, retries=1):
    """
    收集一个博客在 cutoff（含）之后发布的文章。
    返回 {'posts': [...], 'newest': 最新一篇日期或 None, 'error': ...}
    """
    tried = []
    posts = {}
    newest = None

    def note_newest(when):
        nonlocal newest
        if when is not None and (newest is None or when > newest):
            newest = when

    def add(url, when, title, source):
        if when is None:
            return
        note_newest(when)
        if when < cutoff:
            return
        if url and not same_site(url, blog_url):
            return
        if url and not looks_like_post(url, blog_url):
            return  # 标签页 / 分类页 / 首页这类不算文章
        key = dedupe_key(url, title, when)
        if key not in posts or (title and not posts[key].get('title')):
            posts[key] = {'url': url or blog_url, 'title': title, 'date': when, 'source': source}

    def add_feed(url, source):
        text, error = fetch(url, timeout, retries)
        if error:
            tried.append('%s(%s)' % (urlparse(url).path or url, error))
            return
        entries = parse_feed(text)
        if not entries:
            return
        before = len(posts)
        for entry in entries:
            link = urljoin(url, entry['link']) if entry.get('link') else None
            add(link, entry['date'], entry['title'], source)
        if entries and len(posts) == before:
            tried.append('%s(订阅源条目都不像本站文章，已忽略)' % (urlparse(url).path or url))

    def add_sitemap(url, source):
        text, error = fetch(url, timeout, retries)
        if error:
            return
        children, entries = parse_sitemap(text)
        for child in children[:2]:
            child_text, child_error = fetch(urljoin(url, child), timeout, retries)
            if not child_error:
                entries.extend(parse_sitemap(child_text)[1])
        for entry in entries:
            add(entry['loc'], entry['date'], None, source)

    # 1) 首页 + 订阅源
    html, error = fetch(blog_url, timeout, retries)
    if html is None:
        return {'posts': [], 'newest': None, 'error': '博客打不开（%s）' % error}

    root = blog_url if blog_url.endswith('/') else blog_url + '/'
    feed_urls = find_feed_links(html, blog_url)
    for path in COMMON_FEED_PATHS:
        candidate = urljoin(root, path)
        if candidate not in feed_urls:
            feed_urls.append(candidate)
    for feed_url in feed_urls[:MAX_FEED_TRIES]:
        add_feed(feed_url, '订阅源')
        if len(posts) >= MAX_WEEK_POSTS:
            break

    # 2) 首页里带日期的文章链接
    if len(posts) < MAX_WEEK_POSTS:
        dates, candidates = page_signals(html, blog_url)
        for candidate in candidates:
            add(candidate['url'], candidate['date'], candidate['title'], '首页')
        # 首页有日期但链接文字里没有：只用来判断「最新一篇」，不硬凑成文章（避免把导航链接算成文章）
        for when in dates:
            note_newest(when)

    # 3) 还是没有：退到 sitemap（它的 lastmod 常是整站构建时间，所以放在后面）
    if not posts and newest is None:
        for name in ('sitemap.xml', 'sitemap_index.xml', 'sitemap-index.xml'):
            add_sitemap(urljoin(root, name), 'sitemap')
            if len(posts) >= MAX_WEEK_POSTS:
                break

    # 4) 还是啥都没拿到：翻两页看看（可能是「文章列表页」，翻进去才看得到文章链接和日期）
    if not posts and newest is None:
        probes = []
        for href, _ in scan_html(html).anchors:
            url = urljoin(blog_url, href)
            if not url.lower().startswith(('http://', 'https://')):
                continue
            if not same_site(url, blog_url) or url.rstrip('/') == blog_url.rstrip('/'):
                continue
            path = urlparse(url).path.strip('/')
            if not path or any(seg.lower() in EXCLUDE_SEGMENTS for seg in path.split('/')):
                continue
            if url not in probes:
                probes.append(url)
        for url in probes[:MAX_POST_PROBES]:
            page_html, page_error = fetch(url, timeout, retries)
            if page_error:
                continue
            page_dates, page_candidates = page_signals(page_html, url)
            for candidate in page_candidates:
                add(candidate['url'], candidate['date'], candidate['title'], '文章页')
            # 只有「翻的这页本身就是文章」时才用它自己的日期；列表页不硬凑（避免把导航链接算成文章）
            if not page_candidates and page_dates and looks_like_post(url, blog_url):
                add(url, max(page_dates), None, '文章页')

    if posts or newest is not None:
        ordered = sorted(posts.values(), key=lambda p: p['date'], reverse=True)
        return {'posts': ordered[:MAX_WEEK_POSTS], 'newest': newest, 'error': None}

    # 5) 最后兜底：前端渲染的博客（Vite/React/Vue SPA）——日期常常写在 JS 里
    if not posts and newest is None:
        sources = []
        inline = '\n'.join(re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S | re.I))
        if inline.strip():
            sources.append(('页面内联脚本', inline))
        for script_url in script_sources(html, blog_url):
            script_text, script_error = fetch(script_url, timeout, retries)
            if script_text and len(script_text) < 8 * 1024 * 1024:
                sources.append(('脚本文件 %s' % urlparse(script_url).path.rsplit('/', 1)[-1], script_text))
        for label, text in sources:
            entries = scan_front_matter(text) + scan_script_dates(text)
            if not entries:
                continue
            slug_by_var = spa_slug_by_var(text)
            for entry in entries:
                link = None
                if entry.get('var') and entry['var'] in slug_by_var:
                    link = spa_post_link(blog_url, slug_by_var[entry['var']], text)
                if not link and not entry.get('title'):
                    continue  # 没链接又没标题的，多半不是文章，别硬算
                add(link, entry['date'], entry['title'], label)
            if posts:
                break
        if posts:
            logging.info('  （%s 的数据来自前端脚本）', person_note(blog_url))

    if posts or newest is not None:
        ordered = sorted(posts.values(), key=lambda p: p['date'], reverse=True)
        return {'posts': ordered[:MAX_WEEK_POSTS], 'newest': newest, 'error': None}

    detail = '；'.join(tried[:2]) if tried else '首页里没找到文章日期'
    return {'posts': [], 'newest': None, 'error': '没找到日期（%s）' % detail}


def check_person(person, days, timeout, retries, order=0):
    """给一个人算出本周产出：done（有文章）/ undone（没有）/ unknown（判断不了）"""
    result = dict(person)
    result['order'] = order
    result['posts'] = []
    if result.get('error'):
        result['status'] = 'unknown'
        return result

    today = date.today()
    cutoff = today - timedelta(days=days)
    found = collect_week_posts(person['blog'], cutoff, timeout, retries)
    result['posts'] = found['posts']
    result['newest'] = found['newest']

    if found['error']:
        result['status'] = 'unknown'
        result['error'] = found['error']
    else:
        result['status'] = 'done' if found['posts'] else 'undone'
    return result


def clip(text, limit=MAX_TITLE_LEN):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def format_title(title):
    title = clip(title)
    if not title:
        return ''
    if title.startswith('《') and title.endswith('》'):
        return title
    return '《%s》' % title


# ---------------------------------------------------------------------------
# 拼消息
# ---------------------------------------------------------------------------

def build_message(options, results, days):
    """
    拼消息：统计信息在最上面（只有姓名 + 篇数），文章链接放在最下面。
    默认输出 markdown；--text 时输出纯文本。

    钉钉的 markdown 渲染只认「列表项」和「空行分段」：
    单独一行 \n 会被吃掉（上一版就是这么挤成一坨的），所以
    - 统计信息 → 每条前面加 "- "，各自成行
    - 人名小标题 / 文章链接标题 → 用空行隔开，各自成段
    """
    today = date.today()
    start = today - timedelta(days=days)

    done = [r for r in results if r['status'] == 'done']
    undone = [r for r in results if r['status'] == 'undone']
    unknown = [r for r in results if r['status'] == 'unknown']
    total_posts = sum(len(r['posts']) for r in done)
    ranked = sorted(done, key=lambda r: (-len(r['posts']), r['order']))

    prefixes = {name: options.__dict__[name].rstrip('：:') for name in
                ('done_prefix', 'undone_prefix', 'unknown_prefix')}

    def strong(text):
        return '**%s**' % text if options.bold else text

    blocks = []

    # @ 未完成的人（钉钉要求正文里出现 @手机号 才会渲染成 @姓名 并强提醒）
    if not options.no_inline_at:
        mobiles = []
        for person in undone + (unknown if options.at_unknown else []):
            if person.get('mobile') and person['mobile'] not in mobiles:
                mobiles.append(person['mobile'])
        if mobiles:
            blocks.append(' '.join('@' + m for m in mobiles))

    # ---------- 上面：统计信息（每条一个列表项，保证换行）----------
    if options.header:
        blocks.append(options.header.format(start=start.strftime('%m-%d'), end=today.strftime('%m-%d'),
                                            posts=total_posts, done=len(done), total=len(results)))
    stats = []
    if ranked:
        # 已完成：按篇数分行，「2篇：张三 李四」这样
        stats.append('- %s' % strong(prefixes['done_prefix'].format(n=len(ranked), posts=total_posts)))
        for count in sorted({len(p['posts']) for p in ranked}, reverse=True):
            group = [p['name'] for p in ranked if len(p['posts']) == count]
            stats.append('- %d篇：%s' % (count, ' '.join(group)))
    if undone:
        stats.append('- %s：%s' % (strong(prefixes['undone_prefix'].format(n=len(undone))),
                                       '、'.join(p['name'] for p in undone)))
    if unknown:
        stats.append('- %s：%s' % (strong(prefixes['unknown_prefix'].format(n=len(unknown))),
                                       '、'.join('%s（%s）' % (p['name'], p.get('error') or '未知原因')
                                                 for p in unknown)))
    if stats:
        blocks.append('\n'.join(stats))

    # ---------- 下面：文章链接（人名单独成段，文章各自一个列表项）----------
    if ranked:
        link_blocks = [strong('文章链接')]
        for person in ranked:
            shown = person['posts'][:max(0, options.max_posts)]
            items = []
            for post in shown:
                when = post['date'].strftime('%m-%d')
                title = format_title(post.get('title')) or '（无标题）'
                url = post.get('url') or ''
                same_as_home = url.rstrip('/') == person['blog'].rstrip('/')
                if options.text or not url or same_as_home:
                    items.append('- %s %s' % (when, title))
                    if url and not same_as_home and options.text:
                        items.append('- %s' % url)
                else:
                    items.append('- [%s %s](%s)' % (when, title, url))
            if len(person['posts']) > options.max_posts >= 0:
                items.append('- …另有 %d 篇没列出' % (len(person['posts']) - options.max_posts))
            link_blocks.append(strong('【%s %d 篇】' % (person['name'], len(person['posts']))))
            link_blocks.append('\n'.join(items))
        blocks.append('\n\n'.join(link_blocks))

    if not blocks:
        blocks.append('本周没有人更新博客')
    return '\n\n'.join(blocks), done, undone, unknown, total_posts


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------

def load_credentials(options):
    if dingtalk_config is not None:
        return dingtalk_config.resolve_credentials(options.access_token, options.secret)
    token = options.access_token or os.environ.get('DINGTALK_ACCESS_TOKEN')
    secret = options.secret or os.environ.get('DINGTALK_SECRET')
    return token, secret, ('命令行参数' if token and secret else None)


def main():
    setup_logger()
    options = define_options()

    if options.file and options.file.lower().endswith('.xls'):
        logging.error('旧版 .xls 格式读不了，请在 Excel 里「另存为」成 .xlsx 再来：%s', options.file)
        sys.exit(2)

    people = load_people(options.file)
    if not people:
        logging.error('名单是空的：检查脚本里的 PEOPLE 列表，或用 -f 指定表格')
        sys.exit(2)

    today = date.today()
    cutoff = today - timedelta(days=options.days)
    logging.info('统计窗口：%s ~ %s（最近 %d 天）', cutoff.isoformat(), today.isoformat(), options.days)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, options.workers)) as pool:
        futures = [pool.submit(check_person, p, options.days, options.timeout, options.retries, index)
                   for index, p in enumerate(people)]
        results = [f.result() for f in futures]

    for person in results:
        if person['status'] == 'done':
            logging.info('✔ %s：本周 %d 篇', person['name'], len(person['posts']))
            for post in person['posts']:
                logging.info('      %s %s %s', post['date'], post['url'], post.get('title') or '')
        elif person['status'] == 'undone':
            logging.info('✘ %s：本周 0 篇（最新一篇 %s）', person['name'],
                         person.get('newest') or '未检测到')
        else:
            logging.warning('？ %s：%s', person['name'], person.get('error'))

    message, done, undone, unknown, total_posts = build_message(options, results, options.days)
    logging.info('汇总：本周共 %d 篇，完成 %d 人 / 未完成 %d 人 / 无法判断 %d 人',
                 total_posts, len(done), len(undone), len(unknown))

    if options.dry_run:
        logging.info('--dry_run 已开启，以下内容不会发送：')
        print('-' * 60)
        print(message)
        print('-' * 60)
        return

    if send_custom_robot_group_message is None:
        logging.error('找不到 dingtalk_robot.py，请把它和本脚本放在同一目录')
        sys.exit(2)

    token, secret, source = load_credentials(options)
    if not token or not secret:
        logging.error('缺少钉钉凭据：请先运行一次 python3 setup_config.py，'
                      '或用 --access_token/--secret、环境变量传入')
        sys.exit(2)
    logging.info('钉钉凭据来源：%s', source)

    at_user_ids = [u.strip() for u in options.userid.split(',') if u.strip()] if options.userid else []
    at_mobiles = [m.strip() for m in options.at_mobiles.split(',') if m.strip()] if options.at_mobiles else []
    for person in undone + (unknown if options.at_unknown else []):
        if person.get('mobile') and person['mobile'] not in at_mobiles:
            at_mobiles.append(person['mobile'])

    result = send_custom_robot_group_message(
        token, secret, message,
        at_user_ids=at_user_ids, at_mobiles=at_mobiles, is_at_all=options.is_at_all,
        msgtype='text' if options.text else 'markdown',
        markdown_title='本周博客更新统计：%d 篇，%d/%d 人完成' % (total_posts, len(done), len(results))
    )
    if isinstance(result, dict) and result.get('errcode') not in (0, None):
        logging.error('发送失败：errcode=%s errmsg=%s', result.get('errcode'), result.get('errmsg'))
        sys.exit(1)
    logging.info('已发送到钉钉群：本周共 %d 篇，完成 %d 人 / 未完成 %d 人 / 无法判断 %d 人',
                 total_posts, len(done), len(undone), len(unknown))



if __name__ == '__main__':
    main()
