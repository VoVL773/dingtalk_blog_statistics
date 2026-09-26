#!/usr/bin/env python
"""
钉钉机器人凭据的统一读取 / 保存（供 dingtalk_robot.py、blog_reader.py、setup_config.py 共用）。

取值优先级（从高到低）：
    1. 命令行参数              --access_token / --secret
    2. 环境变量                DINGTALK_ACCESS_TOKEN / DINGTALK_SECRET
    3. 配置文件 config.json    （先看 DINGTALK_CONFIG，再看脚本目录、当前目录、~/.config/dingtalk-robot/）

config.json 由 setup_config.py 生成，格式：
    {"access_token": "...", "secret": "SEC..."}
"""

import json
import os

CONFIG_FILENAME = 'config.json'
ENV_CONFIG = 'DINGTALK_CONFIG'
ENV_TOKEN = 'DINGTALK_ACCESS_TOKEN'
ENV_SECRET = 'DINGTALK_SECRET'

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_CONFIG = os.path.join(os.path.expanduser('~'), '.config', 'dingtalk-robot', CONFIG_FILENAME)


def config_candidates():
    """可能放 config.json 的位置，按优先级排好序。"""
    paths = []
    env_path = os.environ.get(ENV_CONFIG)
    if env_path:
        paths.append(os.path.expanduser(env_path))
    paths.append(os.path.join(SCRIPT_DIR, CONFIG_FILENAME))
    cwd_path = os.path.join(os.getcwd(), CONFIG_FILENAME)
    paths.append(cwd_path)
    paths.append(HOME_CONFIG)

    seen, ordered = set(), []
    for path in paths:
        real = os.path.abspath(path)
        if real not in seen:
            seen.add(real)
            ordered.append(real)
    return ordered


def find_config():
    """返回第一个存在的 config.json 路径，没有就返回 None。"""
    for path in config_candidates():
        if os.path.isfile(path):
            return path
    return None


def load_config(path=None):
    """
    读取配置。返回 ({'access_token':..., 'secret':...}, 路径)
    文件不存在 / 格式不对 / 缺字段时返回 ({}, 路径或 None)
    """
    path = path or find_config()
    if not path or not os.path.isfile(path):
        return {}, path
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print('配置文件读不了（%s）：%s' % (path, e))
        return {}, path

    token = str(data.get('access_token') or '').strip()
    secret = str(data.get('secret') or '').strip()
    if token and secret:
        return {'access_token': token, 'secret': secret}, path
    return {}, path


def save_config(access_token, secret, path=None):
    """把凭据写进 config.json，权限 600（只有自己能读）。返回写入路径。"""
    path = path or os.path.join(SCRIPT_DIR, CONFIG_FILENAME)
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'access_token': access_token.strip(), 'secret': secret.strip()},
                  f, ensure_ascii=False, indent=2)
        f.write('\n')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def resolve_credentials(cli_token=None, cli_secret=None):
    """
    按优先级凑出 (token, secret, 来源说明)。
    凑不齐时来源说明为 None。
    """
    token = (cli_token or '').strip()
    secret = (cli_secret or '').strip()
    if token and secret:
        return token, secret, '命令行参数'

    token = token or os.environ.get(ENV_TOKEN, '').strip()
    secret = secret or os.environ.get(ENV_SECRET, '').strip()
    if token and secret:
        return token, secret, '环境变量'

    config, path = load_config()
    if config:
        token = token or config['access_token']
        secret = secret or config['secret']
        if token and secret:
            return token, secret, '配置文件 %s' % path

    return token, secret, None


def mask(value, head=6, tail=4):
    """打码显示，避免把密钥整串打到终端或日志里。"""
    value = str(value or '')
    if len(value) <= head + tail:
        return '*' * len(value)
    return '%s...%s（共 %d 位）' % (value[:head], value[-tail:], len(value))
