#!/usr/bin/env python
"""
本地发送记录 —— 记住「谁提交过哪些文章链接」，用来识别「拿旧文章当本周产出」的情况。

blog_reader.py 的用法：
    - 统计窗口内检测到的文章，如果链接以前就记录过（记录时间早于本次窗口），
      就当作**不是本周新写的**：从本周产出里剔除，消息里也不显示；一个人被剔完就按「未完成」处理。
    - 只有真正发送成功后才写记录，所以重复跑、dry_run 都不会污染历史。

记录文件默认叫「发送记录.json」，放在本脚本同目录（已在 .gitignore 里，不会提交）。
格式（人可读）：

    {
      "version": 1,
      "updated": "2026-09-26 18:00:00",
      "records": [
        {
          "time": "2026-09-26 18:00:00",
          "week": "2026-W39",
          "outputs": { "张三": ["https://a.github.io/posts/x/"] }
        }
      ]
    }

命令行管理（一般不用）：
    python3 send_history.py                # 概览：文件在哪、几条记录、最近 3 次
    python3 send_history.py --list         # 列出全部记录
    python3 send_history.py --person 张三   # 看某人历史上记过哪些文章
    python3 send_history.py --clear -y     # 清空记录（文件保留）
    python3 send_history.py --delete -y    # 删除记录文件
"""

import json
import os
import sys
from datetime import datetime
from urllib.parse import unquote, urlsplit, urlunsplit

HISTORY_FILENAME = '发送记录.json'
MAX_RECORDS = 100  # 只保留最近这么多条，避免文件无限变大
TIME_FORMAT = '%Y-%m-%d %H:%M:%S'

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def default_history_path():
    """默认就是「脚本所在目录 / 发送记录.json」，换位置请用 --history / --path。"""
    return os.path.join(SCRIPT_DIR, HISTORY_FILENAME)


def normalize_link(url):
    """
    比对前先归一化，免得同一篇文章因为写法不同被当成两篇：
    统一小写协议与域名、去掉路径末尾的 /、解开 %XX 转义；查询串和 #锚点保留。
    """
    url = str(url or '').strip()
    if not url:
        return ''
    url = unquote(url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.rstrip('/').lower()
    if not parts.netloc:
        return url.rstrip('/').lower()
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path.rstrip('/'), parts.query, parts.fragment))


def as_urls(value):
    """记录里的值可能是单个链接，也可能是链接列表，统一成列表。"""
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v or '').strip()]
    text = str(value or '').strip()
    return [text] if text else []


def parse_time(text):
    for fmt in (TIME_FORMAT, '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(text).strip(), fmt)
        except (TypeError, ValueError):
            continue
    return None


def current_week():
    iso = datetime.now().isocalendar()
    return '%d-W%02d' % (iso[0], iso[1])


def load_history(path=None):
    """返回 (记录字典, 实际路径)。文件不存在或坏了都当作空记录，不抛异常。"""
    path = path or default_history_path()
    empty = {'version': 1, 'records': []}
    if not os.path.isfile(path):
        return empty, path
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print('发送记录读不了（%s）：%s —— 本次当作没有历史' % (path, e))
        return empty, path
    if not isinstance(data, dict) or not isinstance(data.get('records'), list):
        print('发送记录格式不对（%s）—— 本次当作没有历史' % path)
        return empty, path
    return data, path


def write_history(history, path):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
        f.write('\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def old_urls(history, since):
    """
    取出「since 之前」记录过的链接，返回 {姓名: {归一化后的链接}}。
    since 传本次统计窗口的起点：窗口内刚记过的（比如本周重复跑了一次）不算旧文，避免误判。
    """
    result = {}
    for record in history.get('records', []):
        when = parse_time(record.get('time'))
        if when is not None and since is not None and when >= since:
            continue  # 这条记录在窗口内，不算「旧文」
        for name, value in (record.get('outputs') or {}).items():
            bucket = result.setdefault(str(name).strip(), set())
            for url in as_urls(value):
                normalized = normalize_link(url)
                if normalized:
                    bucket.add(normalized)
    return result


def save_record(outputs, path=None):
    """把这次发送的内容追加进记录。outputs 形如 {姓名: [链接]}；为空则不写。"""
    path = path or default_history_path()
    cleaned = {}
    for name, value in (outputs or {}).items():
        urls = [str(u).strip() for u in as_urls(value) if str(u).strip()]
        if urls:
            cleaned[str(name).strip()] = urls
    if not cleaned:
        return path, False

    history, path = load_history(path)
    history.setdefault('records', []).append({
        'time': datetime.now().strftime(TIME_FORMAT),
        'week': current_week(),
        'outputs': cleaned,
    })
    if len(history['records']) > MAX_RECORDS:
        history['records'] = history['records'][-MAX_RECORDS:]
    history['version'] = 1
    history['updated'] = datetime.now().strftime(TIME_FORMAT)
    write_history(history, path)
    return path, True


def clear_records(path=None):
    """保留文件，只把记录清空。返回 (路径, 原有条数)。"""
    path = path or default_history_path()
    history, path = load_history(path)
    count = len(history.get('records', []))
    history['records'] = []
    history['version'] = 1
    history['updated'] = datetime.now().strftime(TIME_FORMAT)
    write_history(history, path)
    return path, count


def delete_history(path=None):
    path = path or default_history_path()
    if os.path.isfile(path):
        os.remove(path)
        return path, True
    return path, False


def person_history(name, path=None):
    path = path or default_history_path()
    history, path = load_history(path)
    rows = []
    for record in history.get('records', []):
        when = record.get('time') or record.get('week') or '未知时间'
        for url in as_urls((record.get('outputs') or {}).get(name)):
            rows.append({'time': when, 'link': url})
    return path, rows


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description='发送记录管理：查看 / 清空 / 删除（默认用同目录的 %s）'
                                                 % HISTORY_FILENAME)
    parser.add_argument('--path', dest='path', default=None, help='记录文件路径')
    parser.add_argument('--list', dest='list_all', action='store_true', help='列出所有记录')
    parser.add_argument('--person', dest='person', default=None, help='只看某人历史上记过的文章')
    parser.add_argument('--clear', dest='clear', action='store_true', help='清空记录（保留文件）')
    parser.add_argument('--delete', dest='delete', action='store_true', help='删除记录文件')
    parser.add_argument('-y', '--yes', dest='yes', action='store_true', help='不再二次确认')
    options = parser.parse_args()

    path = options.path or default_history_path()

    if options.delete:
        target, done = delete_history(path)
        print(('已删除记录文件：%s' % target) if done else ('记录文件本来就不存在：%s' % target))
        return

    if options.clear:
        if not os.path.isfile(path):
            print('记录文件本来就不存在，无需清空：%s' % path)
            return
        history, path = load_history(path)
        count = len(history.get('records', []))
        if count and not options.yes:
            answer = input('要清空 %s 里的 %d 条记录吗？输入 y 确认：' % (path, count)).strip().lower()
            if answer != 'y':
                print('已取消，记录未改动。')
                return
        path, count = clear_records(path)
        print('已清空 %d 条记录，文件保留：%s' % (count, path))
        return

    if options.person:
        path, rows = person_history(options.person, path)
        print('记录文件：%s' % path)
        if not rows:
            print('「%s」没有历史记录' % options.person)
        for row in rows:
            print('  %s  %s' % (row['time'], row['link']))
        return

    history, path = load_history(path)
    records = history.get('records', [])
    print('发送记录文件：%s %s' % (path, '（存在）' if os.path.isfile(path) else '（还没有，第一次真发送后生成）'))
    print('已有记录 %d 条' % len(records))
    shown = records if options.list_all else records[-3:]
    for record in shown:
        outputs = record.get('outputs') or {}
        print('  %s（%s）：%d 人' % (record.get('time'), record.get('week'), len(outputs)))
        if options.list_all:
            for name, value in outputs.items():
                for url in as_urls(value):
                    print('      %s -> %s' % (name, url))
    if not options.list_all and len(records) > 3:
        print('  ...（只看最近 3 条，全部用 --list）')


if __name__ == '__main__':
    _cli()
