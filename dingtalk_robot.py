#!/usr/bin/env python
"""
钉钉自定义机器人发送脚本（支持加签、@用户ID、@手机号、@所有人、读文档当消息发送）。

凭据不用写在命令行里：先运行一次 python3 setup_config.py，
之后直接 python3 dingtalk_robot.py --msg "大家好" 就行。
凭据优先级：--access_token/--secret > 环境变量 > config.json（setup_config.py 生成）。
"""

import argparse
import logging
import os
import sys
import time
import hmac
import hashlib
import base64
import urllib.parse
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import dingtalk_config
except ImportError:  # 少了这个文件就退回老用法（纯命令行参数）
    dingtalk_config = None

DEFAULT_MSG = '钉钉，让进步发生'
# 钉钉自定义机器人 text 消息内容上限（字节），可用 --max_bytes 调整；0 表示不限制
DEFAULT_MAX_BYTES = 20000


def setup_logger():
    logger = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter('%(asctime)s %(name)-8s %(levelname)-8s %(message)s [%(filename)s:%(lineno)d]'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def define_options():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--access_token', dest='access_token', default=None,
        help='机器人webhook的access_token（不写则自动读 config.json / 环境变量）'
    )
    parser.add_argument(
        '--secret', dest='secret', default=None,
        help='机器人安全设置的加签secret（不写则自动读 config.json / 环境变量）'
    )
    parser.add_argument(
        '--userid', dest='userid',
        help='待 @ 的钉钉用户ID，多个用逗号分隔'
    )
    parser.add_argument(
        '--at_mobiles', dest='at_mobiles',
        help='待 @ 的手机号，多个用逗号分隔'
    )
    parser.add_argument(
        '--is_at_all', dest='is_at_all', action='store_true',
        help='是否@所有人，指定则为True，不指定为False'
    )
    parser.add_argument(
        '--msg', dest='msg', default=None,
        help='要发送的消息内容（和 --file 同时给出时以 --file 为准）'
    )
    parser.add_argument(
        '-f', '--file', dest='file', default=None,
        help='文档路径，读取该文件内容作为消息发送；传 - 表示从标准输入读取'
    )
    parser.add_argument(
        '--max_bytes', dest='max_bytes', type=int, default=DEFAULT_MAX_BYTES,
        help='消息内容字节上限，超出则截断（默认 %d，传 0 表示不限制）' % DEFAULT_MAX_BYTES
    )
    parser.add_argument(
        '--markdown', dest='markdown', action='store_true',
        help='按 markdown 发送（支持加粗、[标题](链接) 等；默认按纯文本发）'
    )
    parser.add_argument(
        '--markdown_title', dest='markdown_title', default=None,
        help='markdown 消息的通知标题（不传则取正文第一行）'
    )
    parser.add_argument(
        '--strict_length', dest='strict_length', action='store_true',
        help='内容超过 --max_bytes 时直接报错退出，而不是截断'
    )
    return parser.parse_args()


def read_document(path):
    """
    读取文档内容，自动识别常见中文编码。
    返回 (内容, 编码, 来源描述)
    """
    if path == '-':
        data = sys.stdin.buffer.read()
        source = '<stdin>'
    else:
        real_path = os.path.expanduser(path)
        if not os.path.isfile(real_path):
            raise FileNotFoundError(real_path)
        with open(real_path, 'rb') as f:
            data = f.read()
        source = real_path

    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding), encoding, source
        except UnicodeDecodeError:
            continue
    # 兜底：按 utf-8 强制解码，坏字节替换掉，不让脚本崩
    return data.decode('utf-8', errors='replace'), 'utf-8(容错)', source


def truncate_to_bytes(text, max_bytes, suffix='\n……（内容过长，已截断）'):
    """
    按 UTF-8 字节数截断，避免切出半个汉字。
    返回 (文本, 是否被截断)
    """
    if max_bytes <= 0 or len(text.encode('utf-8')) <= max_bytes:
        return text, False
    budget = max(max_bytes - len(suffix.encode('utf-8')), 0)
    truncated = text.encode('utf-8')[:budget].decode('utf-8', errors='ignore')
    return truncated + suffix, True


def resolve_message(options):
    """
    决定最终要发送的文本：--file 优先，其次 --msg，都没有则用默认文案。
    """
    if options.file:
        if options.msg:
            logging.warning('同时指定了 --file 和 --msg，以 --file 内容为准')
        try:
            content, encoding, source = read_document(options.file)
        except FileNotFoundError as e:
            logging.error('找不到文档：%s（请检查路径是否正确）', e)
            sys.exit(2)
        except PermissionError as e:
            logging.error('没有权限读取文档：%s', e)
            sys.exit(2)
        except OSError as e:
            logging.error('读取文档失败：%s', e)
            sys.exit(2)

        content = content.replace('\r\n', '\n').rstrip()
        if not content:
            logging.error('文档内容为空，不发空消息：%s', source)
            sys.exit(2)

        logging.info('已读取文档：%s（编码 %s，%d 字符 / %d 字节）',
                     source, encoding, len(content), len(content.encode('utf-8')))
        return content, source

    return (options.msg if options.msg is not None else DEFAULT_MSG), '--msg'


def send_custom_robot_group_message(access_token, secret, msg, at_user_ids=None, at_mobiles=None,
                                    is_at_all=False, msgtype='text', markdown_title=None):
    """
    发送钉钉自定义机器人群消息。

    msgtype='text'     普通文本
    msgtype='markdown'  markdown（支持 **加粗**、[标题](链接)、- 列表等；
                        钉钉要求 markdown 必须带一个 title，用于通知栏/会话列表显示，
                        没传就取正文第一行）
    """
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f'{timestamp}\n{secret}'
    hmac_code = hmac.new(secret.encode('utf-8'), string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

    url = f'https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}&sign={sign}'

    body = {
        "at": {
            "isAtAll": str(is_at_all).lower(),
            "atUserIds": at_user_ids or [],
            "atMobiles": at_mobiles or []
        },
        "msgtype": msgtype
    }
    if msgtype == 'markdown':
        title = (markdown_title or '').strip()
        if not title:
            for line in msg.splitlines():
                if line.strip():
                    title = line.strip().lstrip('#*> -')
                    break
        body['markdown'] = {"title": title[:50] or '钉钉消息', "text": msg}
    else:
        body['text'] = {"content": msg}

    headers = {'Content-Type': 'application/json'}
    resp = requests.post(url, json=body, headers=headers)
    logging.info("钉钉自定义机器人群消息响应：%s", resp.text)
    return resp.json()


def main():
    setup_logger()
    options = define_options()

    msg, origin = resolve_message(options)
    msg_bytes = len(msg.encode('utf-8'))
    if options.max_bytes > 0 and msg_bytes > options.max_bytes:
        if options.strict_length:
            logging.error('内容共 %d 字节，超过上限 %d 字节（--strict_length 已开启，不截断）',
                          msg_bytes, options.max_bytes)
            sys.exit(2)
        msg, _ = truncate_to_bytes(msg, options.max_bytes)
        logging.warning('内容共 %d 字节，超过上限 %d 字节，已截断后再发送：%s',
                        msg_bytes, options.max_bytes, origin)

    # 处理 @用户ID
    at_user_ids = []
    if options.userid:
        at_user_ids = [u.strip() for u in options.userid.split(',') if u.strip()]
    # 处理 @手机号
    at_mobiles = []
    if options.at_mobiles:
        at_mobiles = [m.strip() for m in options.at_mobiles.split(',') if m.strip()]

    # 凭据：命令行 > 环境变量 > config.json
    token, secret, source = options.access_token, options.secret, '命令行参数'
    if dingtalk_config is not None:
        token, secret, source = dingtalk_config.resolve_credentials(options.access_token, options.secret)
    if not token or not secret:
        logging.error('缺少钉钉凭据：请先运行 python3 setup_config.py 配置一次，'
                      '或用 --access_token/--secret、环境变量传入')
        sys.exit(2)
    logging.info('钉钉凭据来源：%s', source)

    result = send_custom_robot_group_message(
        token,
        secret,
        msg,
        at_user_ids=at_user_ids,
        at_mobiles=at_mobiles,
        is_at_all=options.is_at_all,
        msgtype='markdown' if options.markdown else 'text',
        markdown_title=options.markdown_title
    )

    if isinstance(result, dict) and result.get('errcode') not in (0, None):
        logging.error('发送失败：errcode=%s errmsg=%s', result.get('errcode'), result.get('errmsg'))
        sys.exit(1)
    return result


if __name__ == '__main__':
    main()
