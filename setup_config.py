#!/usr/bin/env python
"""
第一次使用时运行这个脚本，按提示输入钉钉机器人的 access_token 和 secret，
它会写入 config.json（权限 600），之后 dingtalk_robot.py / blog_reader.py 自动读取，
不用再改代码、也不用每次在命令行里写凭据 —— 多人使用就是每人跑一遍这个脚本。

    python3 setup_config.py              # 交互输入，写到脚本目录的 config.json
    python3 setup_config.py --show       # 只看当前用的是哪份配置（密钥打码）
    python3 setup_config.py --clear      # 清空配置（内容置空，文件保留，会二次确认）
    python3 setup_config.py --delete     # 直接删掉 config.json（-y 免确认）
    python3 setup_config.py --home       # 写到 ~/.config/dingtalk-robot/config.json
    python3 setup_config.py --token xx --secret SECyy --no-test   # 非交互（批量部署用）
    python3 setup_config.py --force      # 已有配置时直接覆盖，不询问

access_token / secret 从哪来：钉钉群 -> 群设置 -> 智能群助手 -> 添加机器人 -> 自定义
（安全设置里勾「加签」，就能拿到 SEC 开头的 secret）
"""

import argparse
import getpass
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dingtalk_config as config  # noqa: E402

try:
    from dingtalk_robot import send_custom_robot_group_message
except ImportError:
    send_custom_robot_group_message = None

TEST_MESSAGE = '【配置成功】钉钉机器人已就绪，之后用 blog_reader.py 发博客推荐即可。'


def define_options():
    parser = argparse.ArgumentParser(description='钉钉机器人凭据配置向导')
    parser.add_argument('--token', dest='token', default=None,
                        help='access_token，给了就不交互询问')
    parser.add_argument('--secret', dest='secret', default=None,
                        help='加签 secret，给了就不交互询问')
    parser.add_argument('--home', dest='home', action='store_true',
                        help='写到 ~/.config/dingtalk-robot/config.json（而不是脚本目录）')
    parser.add_argument('--path', dest='path', default=None,
                        help='自定义配置文件的完整路径')
    parser.add_argument('--show', dest='show', action='store_true',
                        help='只显示当前生效的配置来源，然后退出')
    parser.add_argument('--force', dest='force', action='store_true',
                        help='已有配置时直接覆盖，不询问')
    parser.add_argument('--no-test', dest='no_test', action='store_true',
                        help='不发送测试消息')
    parser.add_argument('--clear', dest='clear', action='store_true',
                        help='清空配置：把 config.json 的内容置空（文件保留）')
    parser.add_argument('--delete', dest='delete', action='store_true',
                        help='删除配置：直接删掉 config.json')
    parser.add_argument('-y', '--yes', dest='yes', action='store_true',
                        help='清理/删除时不再二次确认')
    return parser.parse_args()


def looks_like_token(value):
    return bool(re.fullmatch(r'[0-9a-fA-F]{32,128}', value))


def looks_like_secret(value):
    return value.startswith('SEC') and len(value) >= 20


def ask_secret(label):
    """交互输入；输入内容不回显。没有终端（管道）时会自动退化成普通读取。"""
    while True:
        try:
            value = getpass.getpass(label).strip()
        except (EOFError, KeyboardInterrupt):
            print('\n已取消，配置未修改。')
            sys.exit(1)
        if value:
            return value
        print('  不能为空，请重新输入。')


def show_current():
    config_path = config.find_config()
    if not config_path:
        print('当前没有 config.json —— 直接运行 python3 setup_config.py 走一遍配置向导即可。')
    else:
        data, _ = config.load_config(config_path)
        print('当前使用的配置文件：%s' % config_path)
        if data:
            print('  access_token : %s' % config.mask(data['access_token']))
            print('  secret       : %s' % config.mask(data['secret']))
        else:
            print('  （文件存在，但内容不完整或格式不对）')
    token, secret, source = config.resolve_credentials()
    print('实际生效来源：%s' % (source or '没找到任何凭据'))
    for name, path in zip(('脚本目录', '当前目录', '用户目录'),
                          (os.path.join(config.SCRIPT_DIR, config.CONFIG_FILENAME),
                           os.path.join(os.getcwd(), config.CONFIG_FILENAME),
                           config.HOME_CONFIG)):
        print('  %s：%s %s' % (name, path, '✓存在' if os.path.isfile(path) else '（无）'))


def existing_configs(target=None):
    """当前实际存在的 config.json（--path/--home 指定时只看那一个）。"""
    if target:
        return [target] if os.path.isfile(target) else []
    found, seen = [], set()
    for path in config.config_candidates():
        real = os.path.abspath(path)
        if real not in seen and os.path.isfile(real):
            seen.add(real)
            found.append(real)
    return found


def do_clear(paths, delete=False, yes=False):
    """清空（内容置空）或删除配置。paths 为空表示没找到。"""
    if not paths:
        print('没有找到任何 config.json，本来就没配置，不用清。')
        return
    print('将要%s以下配置文件：' % ('删除' if delete else '清空'))
    for path in paths:
        print('  %s' % path)
    if not yes:
        answer = input('确认吗？输入 y 继续，其他任意键取消：').strip().lower()
        if answer != 'y':
            print('已取消，配置未改动。')
            return
    for path in paths:
        try:
            if delete:
                os.remove(path)
                print('已删除：%s' % path)
            else:
                config.save_config('', '', path)  # 写成一个空的 {} 并把内容清掉
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('{}\n')
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                print('已清空：%s' % path)
        except OSError as e:
            print('处理失败：%s（%s）' % (path, e))
    print('\n现在没有任何可用凭据了：再发消息会提示「缺少钉钉凭据」。')
    print('要重新配置就运行：python3 setup_config.py')


def send_test_message(token, secret):
    if send_custom_robot_group_message is None:
        print('  找不到 dingtalk_robot.py，跳过测试消息。')
        return False
    print('  正在发送测试消息...')
    try:
        result = send_custom_robot_group_message(token, secret, TEST_MESSAGE)
    except Exception as e:
        print('  测试消息发送失败：%s' % e)
        return False
    if isinstance(result, dict) and result.get('errcode') == 0:
        print('  ✅ 测试消息已发出，去群里看一眼吧。')
        return True
    errcode = (result or {}).get('errcode') if isinstance(result, dict) else '?'
    errmsg = (result or {}).get('errmsg') if isinstance(result, dict) else result
    print('  ❌ 发送失败：errcode=%s errmsg=%s' % (errcode, errmsg))
    print('     310000 = secret 不对；300001 = 安全设置没匹配上（检查是否选了「加签」）；'
          '300005 = access_token 不存在')
    return False


def main():
    options = define_options()

    if options.show:
        show_current()
        return

    target = options.path or (config.HOME_CONFIG if options.home
                              else os.path.join(config.SCRIPT_DIR, config.CONFIG_FILENAME))
    target = os.path.abspath(os.path.expanduser(target))

    if options.clear or options.delete:
        # 指定了 --path/--home 就只动那一个，否则把所有位置的 config.json 都处理掉
        paths = existing_configs(target if (options.path or options.home) else None)
        do_clear(paths, delete=options.delete, yes=options.yes)
        return

    print('=' * 64)
    print('钉钉机器人凭据配置向导')
    print('access_token / secret 位置：钉钉群 -> 群设置 -> 智能群助手 -> 自定义机器人（安全设置选「加签」）')
    print('=' * 64)

    if os.path.isfile(target) and not options.force:
        old, _ = config.load_config(target)
        print('已经存在配置文件：%s' % target)
        if old:
            print('  现有 access_token：%s' % config.mask(old['access_token']))
            print('  现有 secret      ：%s' % config.mask(old['secret']))
        answer = input('要覆盖它吗？输入 y 覆盖，其他任意键取消：').strip().lower()
        if answer != 'y':
            print('已取消，配置未修改。')
            return

    token = options.token or ask_secret('access_token（粘贴后回车，输入时不显示）: ')
    secret = options.secret or ask_secret('secret（SEC 开头，粘贴后回车，输入时不显示）: ')

    if not looks_like_token(token):
        print('⚠️  access_token 一般是 64 位十六进制字符，你输入的是 %d 位，确认没粘错就继续。' % len(token))
    if not looks_like_secret(secret):
        print('⚠️  secret 一般以 SEC 开头，你输入的是 %s，确认没粘错就继续。' % config.mask(secret))

    path = config.save_config(token, secret, target)
    print('\n✅ 已写入配置文件：%s' % path)
    print('   文件权限已设为 600（只有你自己能读）。')

    if not options.no_test:
        answer = input('要不要现在发一条测试消息到群里？输入 y 发送，其他任意键跳过：').strip().lower()
        if answer == 'y':
            send_test_message(token, secret)

    print('\n接下来这样用（凭据会自动读取，不用再写 token/secret）：')
    print('    python3 blog_reader.py -f 博客链接.xlsx            # 按表格点名 + 发手写的推荐链接')
    print('    python3 blog_reader.py -f 博客链接.xlsx --dry-run  # 先预览不发送')
    print('    python3 setup_config.py --show                     # 查看当前配置来源')
    print('\n换人使用：每人复制这份目录后，各自运行一次 python3 setup_config.py 即可。')
    print('注意：config.json 和你自己的密钥一样重要，别上传到网盘 / Git / 聊天工具。')


if __name__ == '__main__':
    main()
