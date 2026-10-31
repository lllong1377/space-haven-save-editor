#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Space Haven 存档可视化修改器。

仅依赖标准库。读取/写回游戏存档 XML，写入前自动备份。
设计原则：只做定点替换（offset patch），不重新序列化整个文件，
保证存档其余字节与游戏原始输出完全一致。
"""

import datetime
import sys
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# 资源与可写目录：打包成 exe 后，只读资源（index.html/defs.json）位于 _MEIPASS，
# 可写数据（backups/config.json/server.log）放在用户数据目录（onefile 下 exe 会被解压到
# 临时目录，不能依赖 sys.executable 所在位置）。
if getattr(sys, 'frozen', False):
    _appdata = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or os.path.expanduser('~')
    BASE = os.path.join(_appdata, 'SpaceHavenSaveEditor')
    os.makedirs(BASE, exist_ok=True)
    RES = sys._MEIPASS
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

BACKUP_ROOT = os.path.join(BASE, 'backups')
LOG_PATH = os.path.join(BASE, 'server.log')
CONFIG_PATH = os.path.join(BASE, 'config.json')
DEFS_PATH = os.path.join(RES, 'defs.json')
INDEX_PATH = os.path.join(RES, 'index.html')
PORT_RANGE = range(8766, 8780)


def has_saves(root):
    """目录里是否存在至少一个 <槽位>/save/game 或 <槽位>/autosaveN/game。"""
    try:
        for slot in os.listdir(root):
            sd = os.path.join(root, slot)
            if not os.path.isdir(sd):
                continue
            if os.path.isfile(os.path.join(sd, 'save', 'game')):
                return True
            for sub in os.listdir(sd):
                if sub.startswith('autosave') and os.path.isfile(os.path.join(sd, sub, 'game')):
                    return True
    except OSError:
        pass
    return False


# --------------------------------------------------------------------------
# 双语支持：前端通过 lang 参数选择，M(zh, en) 按当前语言取文案
# （需定义在 pick_initial_root 之前：模块加载期会调用它）
# --------------------------------------------------------------------------
_LANG = 'zh'


def set_lang(v):
    global _LANG
    _LANG = 'en' if str(v or '').strip().lower().startswith('en') else 'zh'


def M(zh, en):
    return en if _LANG == 'en' else zh


def dname(d, fallback=''):
    """按当前语言取 defs 词条名称（优先 en，缺省回落 cn）。"""
    if not isinstance(d, dict):
        return fallback
    if _LANG == 'en' and d.get('en'):
        return d['en']
    return d.get('cn') or d.get('en') or fallback


def ddesc(d):
    if not isinstance(d, dict):
        return ''
    if _LANG == 'en' and d.get('descEn'):
        return d['descEn']
    return d.get('desc') or ''


# 存档目录完全由用户手动指定，不再自动探测（更安全，也避免暴露本机目录布局）。
# 通过界面「存档目录」按钮设置，持久化到 config.json 的 save_root；
# 可选 SPACE_HAVEN_SAVE_ROOT 环境变量供自测覆盖。


def load_config():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def persist_config(cfg):
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def pick_initial_root():
    """优先级：环境变量（自测用）> config.json 中已保存的目录。
    都不存在时返回 None，由用户在界面手动指定存档目录。"""
    env = os.environ.get('SPACE_HAVEN_SAVE_ROOT')
    if env:
        return env
    r = load_config().get('save_root')
    if r and os.path.isdir(r) and has_saves(r):
        return r
    return None


ENV_LOCKED = bool(os.environ.get('SPACE_HAVEN_SAVE_ROOT'))
SAVE_ROOT = pick_initial_root()

with open(DEFS_PATH, encoding='utf-8') as _f:
    DEFS = json.load(_f)


# --------------------------------------------------------------------------
# 带偏移量的极简 XML 解析器
# --------------------------------------------------------------------------
class XErr(Exception):
    pass


class Node(object):
    __slots__ = ('tag', 'start', 'end', 'attrs', 'parent', 'children',
                 'self_closing', 'inner_start', 'inner_end',
                 'close_start', 'close_end')

    def __init__(self, tag, start=-1, end=-1):
        self.tag = tag
        self.start = start
        self.end = end
        self.attrs = {}          # name -> (value, value_start, value_end)
        self.parent = None
        self.children = []
        self.self_closing = False
        self.inner_start = end
        self.inner_end = end
        self.close_start = end
        self.close_end = end

    def get(self, name):
        a = self.attrs.get(name)
        return a[0] if a else None

    def kids(self, tag):
        return [c for c in self.children if c.tag == tag]

    def kid(self, tag):
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


_ATTR_RE = re.compile(r'([A-Za-z_][\w:.\-]*)\s*=\s*"([^"]*)"')


def parse_xml(text):
    """解析为带字节（字符）偏移的节点树。根为虚拟节点。"""
    root = Node('__root__')
    stack = [root]
    i = 0
    n = len(text)
    while i < n:
        j = text.find('<', i)
        if j < 0:
            break
        if text.startswith('<?', j):
            k = text.find('?>', j)
            if k < 0:
                raise XErr(M('XML 声明未闭合', 'Unclosed XML declaration'))
            i = k + 2
            continue
        if text.startswith('<!--', j):
            k = text.find('-->', j)
            if k < 0:
                raise XErr(M('注释未闭合', 'Unclosed comment'))
            i = k + 3
            continue
        # 扫描到 '>'，跳过引号内内容
        k = j + 1
        quote = None
        while k < n:
            ch = text[k]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch == '>':
                break
            k += 1
        if k >= n:
            raise XErr(M('第 %d 字符处标签未闭合', 'Unclosed tag at char %d') % j)
        raw = text[j:k + 1]
        if raw.startswith('</'):
            tag = raw[2:].rstrip('>').strip()
            if len(stack) < 2 or stack[-1].tag != tag:
                got = stack[-1].tag if len(stack) > 1 else '（文件结束）'
                raise XErr(M('标签不匹配：期望 </%s>，实际 </%s>', 'Tag mismatch: expected </%s>, got </%s>') % (got, tag))
            node = stack.pop()
            node.close_start = j
            node.close_end = k + 1
            node.inner_end = j
        else:
            body_end = k - (1 if raw.rstrip().endswith('/>') else 0)
            head = text[j + 1:body_end]
            m = re.match(r'\s*([A-Za-z_][\w:.\-]*)', head)
            if not m:
                raise XErr(M('第 %d 字符处标签名无效', 'Invalid tag name at char %d') % j)
            tag = m.group(1)
            node = Node(tag, j, k + 1)
            node.self_closing = raw.rstrip().endswith('/>')
            node.parent = stack[-1]
            node.parent.children.append(node)
            node.inner_start = node.end
            for am in _ATTR_RE.finditer(head, m.end()):
                name, val = am.group(1), am.group(2)
                # 值起止偏移（引号内）
                vs = j + 1 + am.start(2)
                ve = j + 1 + am.end(2)
                node.attrs[name] = (val, vs, ve)
            if not node.self_closing:
                stack.append(node)
        i = k + 1
    if len(stack) != 1:
        raise XErr(M('存在未闭合标签：</%s>', 'Unclosed tag: </%s>') % stack[-1].tag)
    return root


def iter_nodes(node):
    for c in node.children:
        yield c
        for d in iter_nodes(c):
            yield d


def line_indent(text, pos):
    """pos 所在行的行首缩进。"""
    ls = text.rfind('\n', 0, pos) + 1
    m = re.match(r'[ \t]*', text[ls:])
    return m.group(0)


def patch_attr(node, name, value):
    if name not in node.attrs:
        raise XErr(M('元素 <%s> 没有属性 %s', 'Element <%s> has no attribute %s') % (node.tag, name))
    _, vs, ve = node.attrs[name]
    return (vs, ve, str(value))


# --------------------------------------------------------------------------
# 模型构建
# --------------------------------------------------------------------------
CONTAINER_LABEL_ZH = {
    'storage': '货箱 / 仓库', 'prod': '生产缓冲', 'grow': '种植槽', 'stabil': '稳定装置',
    'wm': '工作机', 'engine': '引擎', 'medical': '医疗设备', 'robo': '机器人坞',
    'research': '研究设备',
}
CONTAINER_LABEL_EN = {
    'storage': 'Cargo hold', 'prod': 'Production buffer', 'grow': 'Farming tray',
    'stabil': 'Stabilizer', 'wm': 'Work machine', 'engine': 'Engine',
    'medical': 'Medical device', 'robo': 'Robot bay', 'research': 'Research device',
}


def container_label(kind):
    return M(CONTAINER_LABEL_ZH.get(kind, '%s 内部' % kind),
             CONTAINER_LABEL_EN.get(kind, '%s interior' % kind))


def build(text, defs):
    """返回 (model_dict, ctx)。ctx 内含节点引用，供操作解析使用。"""
    root = parse_xml(text)
    if not root.children:
        raise XErr(M('存档为空', 'Save file is empty'))
    game = root.children[0]
    if game.tag != 'game':
        raise XErr(M('根元素不是 <game>：%s', 'Root element is not <game>: %s') % game.tag)

    items = defs['items']
    traits = defs['traits']
    attributes = defs['attributes']
    skills_def = defs['skills']

    def item_name(eid):
        e = items.get(str(eid))
        return dname(e) if e else ''

    ctx = {'game': game, 'bank': None, 'ships': [], 'containers': []}
    model = {'ok': True, 'ships': [], 'containers': []}

    # 时钟
    clock = game.kid('clock')
    model['clock'] = {
        'days': clock.get('days') if clock else None,
        'hours': clock.get('hours') if clock else None,
        'minutes': clock.get('minutes') if clock else None,
    }

    # 资金
    bank = game.kid('playerBank')
    ctx['bank'] = bank
    model['bank'] = ({k: v[0] for k, v in bank.attrs.items()} if bank else {})
    model['bankFields'] = sorted(bank.attrs.keys()) if bank else []

    def read_crew(c, si, ci):
        """读取一个 <c> 角色节点（船内或穿梭机内通用）。"""
        pers = c.kid('pers')
        crew = {
            's': si,
            'c': ci,
            'cid': c.get('cid'),
            'name': c.get('name') or '',
            'lname': c.get('lname') or '',
            'gender': (c.get('bb') or 'm'),
            'attrs': [],
            'traits': [],
            'skills': [],
            'status': [],
            'node': c,
            'pers': pers,
        }
        if pers is not None:
            at = pers.kid('attr')
            if at is not None:
                for a in at.kids('a'):
                    aid = a.get('id')
                    crew['attrs'].append({
                        'id': aid,
                        'name': dname(attributes.get(aid)) or (M('未知属性 %s', 'Unknown attribute %s') % aid),
                        'points': int(a.get('points') or 0),
                        'known': aid in attributes,
                    })
            tr = pers.kid('traits')
            if tr is not None:
                ids = []
                for t in tr.kids('t'):
                    tid = t.get('id')
                    ids.append({
                        'id': tid,
                        'name': dname(traits.get(tid)) or (M('未知特质 %s', 'Unknown trait %s') % tid),
                        'desc': ddesc(traits.get(tid)),
                        'known': tid in traits,
                    })
                crew['traits'] = ids
                crew['traitsNode'] = tr
            sk = pers.kid('skills')
            if sk is not None:
                for s in sk.kids('s'):
                    n = s.get('sk')
                    crew['skills'].append({
                        'sk': n,
                        'name': dname(skills_def.get(n)) or (M('技能 %s', 'Skill %s') % n),
                        'level': int(s.get('level') or 0),
                        'mxn': int(s.get('mxn') or 0),
                        'exp': s.get('exp'),
                        'known': n in skills_def,
                    })
                crew['skillsNode'] = sk
            crew['attrNode'] = at
        pe = c.kid('props')
        if pe is not None:
            for p in pe.children:
                if p.tag in ('Health', 'Food', 'Rest', 'Comfort', 'Mood'):
                    crew['status'].append({
                        'key': p.tag,
                        'v': p.get('v'),
                        'ltv': p.get('ltv'),
                    })
        return crew

    # 飞船
    ships_el = game.kid('ships')
    ship_nodes = ships_el.kids('ship') if ships_el else []
    for si, sh in enumerate(ship_nodes):
        settings = sh.kid('settings')
        owner = settings.get('owner') if settings else None
        sinfo = {
            'idx': si,
            'sid': sh.get('sid'),
            'sname': sh.get('sname') or (M('未命名船 #%s', 'Unnamed ship #%s') % (sh.get('sid') or si)),
            'owner': owner,
            'isPlayer': owner == 'Player',
            'crew': [],
        }
        crew_nodes = []
        ch = sh.kid('characters')
        if ch is not None:
            crew_nodes = ch.kids('c')
        for ci, c in enumerate(crew_nodes):
            sinfo['crew'].append(read_crew(c, si, ci))
        ship_ctx = {'node': sh, 'crew': sinfo['crew']}
        ctx['ships'].append(ship_ctx)
        model['ships'].append(sinfo)

    # 穿梭机 / 登陆艇上的角色（刚招募、往返途中的人不会出现在船只 <characters> 里）
    crafts_el = game.kid('crafts')
    for cr in (crafts_el.kids('c') if crafts_el is not None else []):
        cch = cr.kid('characters')
        if cch is None:
            continue
        cnodes = cch.kids('c')
        if not cnodes:
            continue
        home = cr.get('homeSid')
        target = None
        for si2, sinfo2 in enumerate(model['ships']):
            if home is not None and str(sinfo2.get('sid')) == str(home):
                target = si2
                break
        if target is None:
            side = cr.get('side') or 'Player'
            sinfo = {
                'idx': len(model['ships']),
                'sid': home,
                'sname': '穿梭机上（未停靠，sid %s）' % (home or '?'),
                'owner': side,
                'isPlayer': side == 'Player',
                'crew': [],
            }
            ctx['ships'].append({'node': None, 'crew': sinfo['crew']})
            model['ships'].append(sinfo)
            target = len(model['ships']) - 1
        for c in cnodes:
            ci = len(model['ships'][target]['crew'])
            crew = read_crew(c, target, ci)
            crew['craft'] = {
                'id': cr.get('id'),
                'cname': cr.get('cname'),
                'st': cr.get('st'),
                'side': cr.get('side'),
            }
            model['ships'][target]['crew'].append(crew)
            # ctx 与 model 的 crew 是同一个列表，只追加一次

    # 容器（祖先链含 feat 的 <inv>）
    for node in iter_nodes(game):
        if node.tag != 'inv':
            continue
        anc = list(node.ancestors())
        if not any(a.tag == 'feat' for a in anc):
            continue
        feat = next((a for a in anc if a.tag == 'feat'), None)
        kind = node.parent.tag if node.parent is not None else 'storage'
        if kind == 'feat':
            kind = 'storage'
        anchor = None
        for a in anc:
            if a.tag in ('l', 'e') and a.get('id') is not None and a.get('ind') is not None:
                anchor = a
                break
            if a.tag in ('l', 'e') and a.get('x') is not None:
                anchor = a
                break
        ship = next((a for a in anc if a.tag == 'ship'), None)
        if ship is not None:
            owner = ship.kid('settings')
            owner = owner.get('owner') if owner else None
            ship_label = ship.get('sname') or (M('船 #%s', 'Ship #%s') % ship.get('sid'))
            ship_extra = M('（我的船）', ' (my ship)') if owner == 'Player' else ((M('（%s）', ' (%s)') % owner) if owner else '')
            where = ship_label + ship_extra
            in_player_ship = owner == 'Player'
        else:
            sp = next((a for a in anc if a.tag == 'e' and a.get('id')), None)
            if sp is not None:
                where = M('地图物体 #%s', 'Map object #%s') % sp.get('id')
            else:
                where = M('未知位置', 'Unknown location')
            in_player_ship = False

        entries = []
        for s in node.children:
            if s.tag != 's':
                continue
            eid = s.get('elementaryId')
            entries.append({
                'eid': eid,
                'name': item_name(eid),
                'qty': int(s.get('inStorage') or 0),
                'wi': int(s.get('onTheWayIn') or 0),
                'wo': int(s.get('onTheWayOut') or 0),
                'known': str(eid) in items,
            })

        box = {
            'idx': len(ctx['containers']),
            'kind': kind,
            'label': container_label(kind),
            'where': where,
            'isPlayerShip': in_player_ship,
            'eatAllowed': feat.get('eatAllowed') if feat is not None else None,
            'objId': anchor.get('id') if anchor is not None else None,
            'ind': anchor.get('ind') if anchor is not None else None,
            'x': anchor.get('x') if anchor is not None else None,
            'y': anchor.get('y') if anchor is not None else None,
            'items': entries,
            'empty': len(entries) == 0,
        }
        ctx['containers'].append({'inv': node, 'feat': feat})
        model['containers'].append(box)

    model['containerStats'] = {
        'total': len(model['containers']),
        'storage': sum(1 for b in model['containers'] if b['kind'] == 'storage'),
        'items': sum(len(b['items']) for b in model['containers']),
    }
    return model, ctx


# --------------------------------------------------------------------------
# 操作 -> 补丁
# --------------------------------------------------------------------------
def _need_int(v, lo, hi, what):
    try:
        x = int(str(v).strip())
    except Exception:
        raise XErr(M('%s 必须是整数（收到：%r）', '%s must be an integer (got: %r)') % (what, v))
    if x < lo or x > hi:
        raise XErr(M('%s 需在 %d-%d 之间（收到：%d）', '%s must be between %d-%d (got: %d)') % (what, lo, hi, x))
    return x


def _crew_of(ctx, s, c):
    try:
        ship = ctx['ships'][int(s)]
        crew = ship['crew'][int(c)]
    except (IndexError, KeyError, TypeError, ValueError):
        raise XErr(M('找不到目标船员（船=%s 船员=%s）', 'Target crew not found (ship=%s crew=%s)') % (s, c))
    return crew


def op_patches(op, ctx, text, defs):
    t = op.get('t')

    if t == 'raw':
        return [(0, len(text), op.get('text') or '')]

    if t in ('name', 'lname'):
        crew = _crew_of(ctx, op['s'], op['c'])
        val = str(op.get('v') or '')
        if not val.strip():
            raise XErr(M('名字不能为空', 'Name cannot be empty'))
        if len(val) > 40:
            raise XErr(M('名字过长（最多 40 字符）', 'Name too long (max 40 characters)'))
        return [patch_attr(crew['node'], t, val)]

    if t == 'gender':
        crew = _crew_of(ctx, op['s'], op['c'])
        v = op.get('v')
        if v not in ('m', 'f'):
            raise XErr(M('性别只能是 m / f', 'Gender must be m / f'))
        patches = [patch_attr(crew['node'], a, v)
                   for a in ('bb', 'bh') if a in crew['node'].attrs]
        if not patches:
            raise XErr(M('该船员没有性别属性（bb/bh）', 'This crew has no gender attributes (bb/bh)'))
        return patches

    if t == 'attr':
        crew = _crew_of(ctx, op['s'], op['c'])
        aid = str(op.get('id'))
        pts = _need_int(op.get('v'), 1, 999, '属性点数')
        node = crew.get('attrNode')
        if node is None:
            raise XErr(M('该船员没有属性区块', 'This crew has no attribute block'))
        for a in node.kids('a'):
            if a.get('id') == aid:
                return [patch_attr(a, 'points', pts)]
        raise XErr(M('找不到属性 id=%s', 'Attribute id=%s not found') % aid)

    if t == 'traits':
        crew = _crew_of(ctx, op['s'], op['c'])
        node = crew.get('traitsNode')
        if node is None:
            raise XErr(M('该船员没有特质区块', 'This crew has no traits block'))
        ids = op.get('ids') or []
        if not isinstance(ids, list):
            raise XErr(M('特质参数格式错误', 'Malformed traits parameter'))
        clean = []
        for i in ids:
            iv = str(i)
            if iv not in defs['traits']:
                raise XErr(M('未知特质 id=%s（不在游戏数据中）', 'Unknown trait id=%s (not in game data)') % iv)
            clean.append(iv)
        if node.self_closing and not clean:
            raise XErr(M('该船员本就没有特质，无需清空', 'This crew has no traits to clear'))
        head_indent = line_indent(text, node.start)
        child_indent = head_indent + '\t'
        if clean:
            inner = '\n' + '\n'.join(
                '%s<t id="%s"/>' % (child_indent, i) for i in clean) + '\n' + head_indent
        else:
            inner = ''
        if node.self_closing:
            return [(node.start, node.end, '<traits>%s</traits>' % inner)]
        return [(node.inner_start, node.inner_end, inner)]

    if t == 'skill':
        crew = _crew_of(ctx, op['s'], op['c'])
        n = str(op.get('sk'))
        level = _need_int(op.get('level'), 0, 10, '技能等级')
        mxn = _need_int(op.get('mxn'), 0, 10, '技能上限')
        node = crew.get('skillsNode')
        if node is None:
            raise XErr(M('该船员没有技能区块', 'This crew has no skills block'))
        for s in node.kids('s'):
            if s.get('sk') == n:
                return [patch_attr(s, 'level', level), patch_attr(s, 'mxn', mxn)]
        raise XErr(M('找不到技能 sk=%s', 'Skill sk=%s not found') % n)

    if t == 'max':
        # 一键全满：该船员全部属性 points=999、全部技能 level/mxn=10
        crew = _crew_of(ctx, op['s'], op['c'])
        patches = []
        node = crew.get('attrNode')
        if node is not None:
            for a in node.kids('a'):
                if 'points' in a.attrs and a.get('points') != '999':
                    patches.append(patch_attr(a, 'points', 999))
        skn = crew.get('skillsNode')
        if skn is not None:
            for s in skn.kids('s'):
                if 'level' in s.attrs and s.get('level') != '10':
                    patches.append(patch_attr(s, 'level', 10))
                if 'mxn' in s.attrs and s.get('mxn') != '10':
                    patches.append(patch_attr(s, 'mxn', 10))
        if not patches:
            raise XErr(M('该船员的属性与技能已经全是满值，无需修改', 'This crew is already maxed out; nothing to change'))
        return patches

    if t == 'bank':
        bank = ctx['bank']
        if bank is None:
            raise XErr(M('找不到 <playerBank>', '<playerBank> not found'))
        f = op.get('f')
        if f not in bank.attrs:
            raise XErr(M('存档中没有资金字段 %s（现有：%s）', 'No funds field %s in save (have: %s)') % (f, ', '.join(sorted(bank.attrs))))
        val = _need_int(op.get('v'), 0, 2000000000, '数值')
        return [patch_attr(bank, f, val)]

    if t == 'item':
        idx = int(op.get('box'))
        try:
            box = ctx['containers'][idx]
        except (IndexError, TypeError, ValueError):
            raise XErr(M('找不到目标容器 #%s', 'Target container #%s not found') % op.get('box'))
        inv = box['inv']
        eid = str(op.get('eid'))
        if eid not in defs['items']:
            raise XErr(M('未知物品 id=%s（不在游戏数据中，可能损坏存档）', 'Unknown item id=%s (not in game data; would corrupt the save)') % eid)
        mode = op.get('mode')

        target = None
        for s in inv.children:
            if s.tag == 's' and s.get('elementaryId') == eid:
                target = s
                break

        if mode == 'del':
            if target is None:
                raise XErr(M('容器中没有该物品', 'Item not in this container'))
            ls = text.rfind('\n', 0, target.start) + 1
            end = target.end
            nl = text.find('\n', end)
            if nl < 0:
                nl = len(text)
            # 仅当该 <s .../> 独占一行时才连行一起删除，避免误删同行内容
            if text[ls:target.start].strip() or text[end:nl].strip():
                return [(target.start, target.end, '')]
            if end < len(text) and text[end] == '\n':
                end += 1
            return [(ls, end, '')]

        qty = _need_int(op.get('qty'), 0, 1000000, '数量')

        if mode == 'set':
            if target is None:
                raise XErr(M('容器中没有该物品，请用“添加”', 'Item not in this container; use "Add"'))
            return [patch_attr(target, 'inStorage', qty)]

        if mode == 'add':
            line = '<s elementaryId="%s" inStorage="%d" onTheWayIn="0" onTheWayOut="0"/>' % (eid, qty)
            if target is not None:
                return [patch_attr(target, 'inStorage', qty)]
            if inv.self_closing:
                head_indent = line_indent(text, inv.start)
                child_indent = head_indent + '\t'
                new = ('<inv>\n%s%s\n%s</inv>'
                       % (child_indent, line, head_indent))
                return [(inv.start, inv.end, new)]
            if inv.children:
                # 追加到最后一个子元素之后，保持原有缩进结构
                last = inv.children[-1]
                child_indent = line_indent(text, last.start)
                return [(last.end, last.end, '\n' + child_indent + line)]
            head_indent = line_indent(text, inv.start)
            child_indent = head_indent + '\t'
            return [(inv.inner_start, inv.inner_end,
                     '\n%s%s\n%s' % (child_indent, line, head_indent))]

        raise XErr(M('未知操作 %r', 'Unknown op %r') % t)

    raise XErr(M('未知操作类型 %r', 'Unknown op type %r') % t)


def describe(op):
    t = op.get('t')
    if t == 'raw':
        return '整体替换存档 XML（覆盖其他更改）'
    if t in ('name', 'lname'):
        crew = '%s %s' % (op.get('_cn', ''), op.get('_cl', ''))
        return '船员 %s：改%s为「%s」' % (crew.strip(), '名' if t == 'name' else '姓', op.get('v'))
    if t == 'gender':
        return '船员 %s：性别改为%s' % (op.get('_cn', '').strip(), '男' if op.get('v') == 'm' else '女')
    if t == 'attr':
        return '船员 %s：%s %s → %s' % (op.get('_cn', '').strip(), op.get('_an', op.get('id')),
                                       op.get('_ao', '?'), op.get('v'))
    if t == 'traits':
        names = op.get('_tn') or op.get('ids') or []
        return '船员 %s：特质设为 %s' % (op.get('_cn', '').strip(),
                                       '、'.join(names) if names else '（清空）')
    if t == 'skill':
        return '船员 %s：%s 等级 %s → %s，上限 → %s' % (
            op.get('_cn', '').strip(), op.get('_sn', op.get('sk')),
            op.get('_slo', '?'), op.get('level'), op.get('mxn'))
    if t == 'bank':
        return '资金：%s %s → %s' % (op.get('_bf', op.get('f')), op.get('_bo', '?'), op.get('v'))
    if t == 'max':
        return '船员 %s：属性与技能全满（属性 999，技能 10/10）' % (op.get('_cn', '').strip())
    if t == 'item':
        name = op.get('_in') or ('物品 %s' % op.get('eid'))
        box = op.get('_bx') or ('容器 #%s' % op.get('box'))
        if op.get('mode') == 'del':
            return '%s：删除 %s' % (box, name)
        if op.get('mode') == 'add':
            return '%s：添加 %s × %s' % (box, name, op.get('qty'))
        return '%s：%s 数量 %s → %s' % (box, name, op.get('_io', '?'), op.get('qty'))
    return str(op)


def apply_ops(text, ops, defs):
    model, ctx = build(text, defs)
    if not isinstance(ops, list) or not ops:
        raise XErr(M('没有待应用的更改', 'No changes to apply'))
    # 附加上下文标签，便于人类可读描述
    patches = []
    for op in ops:
        try:
            if op.get('t') in ('name', 'lname', 'gender', 'attr', 'traits', 'skill', 'max'):
                crew = _crew_of(ctx, op.get('s'), op.get('c'))
                op['_cn'] = ('%s %s' % (crew['name'], crew['lname'])).strip()
                if op.get('t') == 'attr':
                    aid = str(op.get('id'))
                    op['_an'] = (defs['attributes'].get(aid, {}) or {}).get('cn', aid)
                    for a in crew['attrs']:
                        if a['id'] == aid:
                            op['_ao'] = a['points']
                if op.get('t') == 'traits':
                    op['_tn'] = [x['name'] for x in crew['traits']]
                if op.get('t') == 'skill':
                    for s in crew['skills']:
                        if s['sk'] == str(op.get('sk')):
                            op['_sn'] = s['name']
                            op['_slo'] = s['level']
            elif op.get('t') == 'bank':
                b = model['bank'].get(op.get('f'))
                op['_bo'] = b
                op['_bf'] = op.get('f')
            elif op.get('t') == 'item':
                b = model['containers'][int(op.get('box'))]
                op['_bx'] = '%s · %s' % (b['label'], b['where'])
                for it in b['items']:
                    if it['eid'] == str(op.get('eid')):
                        op['_in'] = it['name']
                        op['_io'] = it['qty']
            patches.extend(op_patches(op, ctx, text, defs))
        except XErr:
            raise
        except Exception as exc:
            raise XErr(M('处理操作失败：%s', 'Failed to process op: %s') % exc)

    # 校验补丁互不重叠，并按偏移倒序套用
    sp = sorted(patches, key=lambda p: (p[0], p[1]))
    for a, b in zip(sp, sp[1:]):
        if a[1] > b[0]:
            raise XErr(M('修改项相互冲突（位置 %d）', 'Changes overlap (at offset %d)') % a[0])
    out = text
    for start, end, new in sorted(patches, key=lambda p: -p[0]):
        out = out[:start] + new + out[end:]

    try:
        ET.fromstring(out)
    except ET.ParseError as e:
        raise XErr(M('修改后的 XML 无法解析，已中止：%s', 'Patched XML failed to parse; aborted: %s') % e)
    if out == text:
        raise XErr(M('内容没有变化', 'Nothing changed'))
    return out


# --------------------------------------------------------------------------
# 存档 / 备份 IO
# --------------------------------------------------------------------------
def list_saves():
    out = []
    if not SAVE_ROOT or not os.path.isdir(SAVE_ROOT):
        return out
    for slot in sorted(os.listdir(SAVE_ROOT)):
        slot_dir = os.path.join(SAVE_ROOT, slot)
        if not os.path.isdir(slot_dir):
            continue
        for sub in sorted(os.listdir(slot_dir)):
            if sub != 'save':
                continue
            g = os.path.join(slot_dir, sub, 'game')
            if os.path.isfile(g):
                st = os.stat(g)
                label = '%s%s' % (slot, M('（正式存档）', ' (main save)'))
                out.append({
                    'path': '%s/%s/game' % (slot, sub),
                    'label': label,
                    'size': st.st_size,
                    'mtime': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                })
    # 自动存档
    for slot in sorted(os.listdir(SAVE_ROOT)):
        slot_dir = os.path.join(SAVE_ROOT, slot)
        if not os.path.isdir(slot_dir):
            continue
        for sub in sorted(os.listdir(slot_dir)):
            if not sub.startswith('autosave'):
                continue
            g = os.path.join(slot_dir, sub, 'game')
            if os.path.isfile(g):
                st = os.stat(g)
                out.append({
                    'path': '%s/%s/game' % (slot, sub),
                    'label': '%s / %s' % (slot, sub),
                    'size': st.st_size,
                    'mtime': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                })
    return out


def resolve_path(rel):
    rel = (rel or '').replace('\\', '/').strip()
    if not rel:
        raise XErr(M('未指定存档', 'No save specified'))
    if '..' in rel.split('/'):
        raise XErr(M('非法路径', 'Illegal path'))
    abspath = os.path.realpath(os.path.join(SAVE_ROOT, *rel.split('/')))
    root = os.path.realpath(SAVE_ROOT)
    if not abspath.startswith(root + os.sep):
        raise XErr(M('路径超出存档目录', 'Path escapes the save folder'))
    if not os.path.isfile(abspath):
        raise XErr(M('存档不存在：%s', 'Save not found: %s') % rel)
    return abspath


def read_save(path):
    with open(path, 'rb') as f:
        data = f.read()
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as e:
        raise XErr(M('存档不是有效的 UTF-8 文本：%s', 'Save is not valid UTF-8 text: %s') % e)
    return text


def sha1_of(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()


def backup_dir_for(path):
    rel = os.path.relpath(path, SAVE_ROOT).replace('\\', '/')
    slot = os.path.dirname(rel).replace('/', '_') or 'default'
    slot = re.sub(r'[^A-Za-z0-9._\-]+', '_', slot)[:80]
    # 用存档根目录名作前缀，避免不同目录的同名槽位备份混在一起
    base = re.sub(r'[^A-Za-z0-9._\-]+', '_',
                  os.path.basename(os.path.normpath(SAVE_ROOT)) or 'root')[:40] or 'root'
    slot = '%s_%s' % (base, slot)
    d = os.path.join(BACKUP_ROOT, slot)
    os.makedirs(d, exist_ok=True)
    return d, rel


def jsonable(o):
    """把 build() 产出的 model 转成可 JSON 序列化的副本（剔除内部 Node 引用）。"""
    if isinstance(o, Node):
        return None
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items() if not isinstance(v, Node)}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    return o


def make_backup(path, tag='backup'):
    text = read_save(path)
    d, rel = backup_dir_for(path)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    name = '%s_%s.bak' % (stamp, tag)
    dest = os.path.join(d, name)
    n = 1
    while os.path.exists(dest):
        n += 1
        name = '%s_%s_%d.bak' % (stamp, tag, n)
        dest = os.path.join(d, name)
    with open(dest, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    return {'name': name, 'dir': d, 'rel': rel,
            'size': len(text.encode('utf-8')),
            'time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


def list_backups(path):
    d, rel = backup_dir_for(path)
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), reverse=True):
            if not n.endswith('.bak'):
                continue
            p = os.path.join(d, n)
            st = os.stat(p)
            out.append({
                'name': n,
                'size': st.st_size,
                'mtime': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            })
    return out


def write_save(path, text):
    tmp = path + '.editor_tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def game_running():
    try:
        kwargs = {'capture_output': True, 'timeout': 6}
        if os.name == 'nt':
            # 窗口化 exe 下，控制台子进程（tasklist.exe）会闪出黑框；隐藏其窗口
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs['startupinfo'] = si
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq spacehaven.exe'], **kwargs)
        return b'spacehaven.exe' in out.stdout
    except Exception:
        return False


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = 'SpaceHavenEditor/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        try:
            line = '%s - %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                (fmt % args) if args else fmt)
        except Exception:
            return
        print(line, flush=True)
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/' or u.path == '/index.html':
            return self._file(INDEX_PATH, 'text/html; charset=utf-8')
        status, j = handle_api('GET', self.path, None)
        self._json(j, status)

    def do_POST(self):
        data = self._body()
        status, j = handle_api('POST', self.path, data)
        self._json(j, status)


def handle_api(method, raw_path, body):
    """统一 API 入口，HTTP 服务端与 pywebview 原生窗口共用。
    返回 (status:int, j:dict)。"""
    global SAVE_ROOT
    u = urlparse(raw_path)
    q = parse_qs(u.query)
    if method == 'GET':
        set_lang((q.get('lang') or [None])[0])
    else:
        set_lang((body or {}).get('lang'))
    try:
        if method == 'GET':
            if u.path == '/api/defs':
                return 200, {'ok': True, 'defs': DEFS}
            if u.path == '/api/saves':
                return 200, {'ok': True, 'saves': list_saves()}
            if u.path == '/api/model':
                path = resolve_path((q.get('p') or [''])[0])
                text = read_save(path)
                model, _ = build(text, DEFS)
                model = jsonable(model)   # 去掉内部 Node 引用，仅保留可序列化数据
                model.update({
                    'ok': True,
                    'rel': os.path.relpath(path, SAVE_ROOT).replace('\\', '/'),
                    'abs': path,
                    'size': len(text.encode('utf-8')),
                    'sha1': sha1_of(text),
                    'mtime': datetime.datetime.fromtimestamp(os.stat(path).st_mtime)
                             .strftime('%Y-%m-%d %H:%M:%S'),
                    'gameRunning': game_running(),
                })
                return 200, model
            if u.path == '/api/raw':
                path = resolve_path((q.get('p') or [''])[0])
                text = read_save(path)
                return 200, {'ok': True, 'text': text, 'sha1': sha1_of(text)}
            if u.path == '/api/config':
                return 200, {
                    'ok': True,
                    'root': SAVE_ROOT,
                    'envLocked': ENV_LOCKED,
                    'candidates': [],
                }
            if u.path == '/api/backups':
                path = resolve_path((q.get('p') or [''])[0])
                return 200, {'ok': True, 'backups': list_backups(path)}
            return 404, {'ok': False, 'error': '404'}
        else:  # POST
            if u.path == '/api/apply':
                path = resolve_path(body.get('p'))
                cur = read_save(path)
                want = body.get('sha1')
                if want and sha1_of(cur) != want:
                    return 409, {
                        'ok': False,
                        'conflict': True,
                        'error': M('存档文件已被外部修改（游戏可能刚刚保存过），请先「重新载入」再重做更改。',
                                   'The save was modified externally (the game may have just saved). Please "Reload" and redo your changes.'),
                    }
                new_text = apply_ops(cur, body.get('ops') or [], DEFS)
                bak = make_backup(path, 'pre')
                try:
                    write_save(path, new_text)
                except PermissionError:
                    return 409, {
                        'ok': False,
                        'error': M('无法写入存档（文件被占用）。请先退出 Space Haven，再重试。',
                                   'Cannot write the save (file in use). Quit Space Haven and retry.'),
                    }
                return 200, {
                    'ok': True,
                    'sha1': sha1_of(new_text),
                    'backup': bak,
                    'size': len(new_text.encode('utf-8')),
                }

            if u.path == '/api/config':
                raw = str(body.get('root') or '').strip().strip('"').strip("'")
                if not raw:
                    return 400, {'ok': False, 'error': M('未填写目录', 'Folder is empty')}
                root = os.path.normpath(os.path.abspath(os.path.expandvars(raw)))
                if not os.path.isdir(root):
                    return 400, {'ok': False, 'error': M('目录不存在：%s', 'Folder does not exist: %s') % root}
                if not has_saves(root):
                    return 400, {'ok': False, 'error':
                        M('该目录下没有找到存档。需要形如 <槽位>/save/game 的目录结构'
                          '（自动存档为 <槽位>/autosave1/game），请选择 savegames 目录本身。',
                          'No saves found in that folder. Expected structure <slot>/save/game '
                          '(autosaves: <slot>/autosave1/game) - select the savegames folder itself.')}
                cfg = load_config()
                cfg['save_root'] = root
                persist_config(cfg)
                applied = True
                note = None
                if ENV_LOCKED:
                    applied = False
                    note = M('当前会话的存档目录被环境变量覆盖，配置已保存，重启后生效',
                             'The save folder is overridden by an environment variable this session; config saved, takes effect after restart')
                else:
                    SAVE_ROOT = root
                return 200, {'ok': True, 'root': SAVE_ROOT, 'applied': applied, 'note': note}

            if u.path == '/api/backup':
                path = resolve_path(body.get('p'))
                return 200, {'ok': True, 'backup': make_backup(path, 'manual')}

            if u.path == '/api/restore':
                path = resolve_path(body.get('p'))
                name = (body.get('name') or '').strip()
                if not name or '/' in name or '\\' in name or '..' in name:
                    return 400, {'ok': False, 'error': M('非法备份名', 'Illegal backup name')}
                d, _ = backup_dir_for(path)
                src = os.path.join(d, name)
                if not os.path.isfile(src):
                    return 404, {'ok': False, 'error': M('备份不存在', 'Backup not found')}
                with open(src, encoding='utf-8', newline='') as f:
                    text = f.read()
                try:
                    ET.fromstring(text)
                except ET.ParseError as e:
                    return 400, {'ok': False, 'error': M('备份内容不是合法 XML：%s', 'Backup is not valid XML: %s') % e}
                make_backup(path, 'before_restore')
                write_save(path, text)
                return 200, {'ok': True, 'sha1': sha1_of(text)}

            return 404, {'ok': False, 'error': '404'}
    except XErr as e:
        return 400, {'ok': False, 'error': str(e)}
    except PermissionError:
        return 409, {'ok': False, 'error':
            M('文件被占用，无法写入。请先退出 Space Haven 再重试。',
              'File in use; cannot write. Quit Space Haven and retry.')}
    except Exception as e:
        return 500, {'ok': False, 'error': '%s: %s' % (type(e).__name__, e)}


class WebviewApi:
    """暴露给前端（pywebview 原生窗口）的本地引擎接口。
    前端以 window.pywebview.api.call(method, path, body) 调用，返回与 HTTP 一致的 JSON。"""

    def call(self, method, path, body=None):
        if body is None:
            body = {}
        _status, j = handle_api(method, path, body)
        return j


def launch_webview():
    """以原生窗口启动（不启动 HTTP 服务）。需要 pywebview。"""
    import webview
    with open(INDEX_PATH, encoding='utf-8') as f:
        html = f.read()
    webview.create_window(
        title=M('Space Haven 存档修改器', 'Space Haven Save Editor'),
        html=html,
        js_api=WebviewApi(),
        width=1200, height=840,
    )
    webview.start()


def pick_port():
    for p in PORT_RANGE:
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), Handler)
            return p, srv
        except OSError:
            continue
    raise SystemExit('8766-8779 端口均被占用')


def log(msg):
    """兼容无控制台（--windowed 打包）的日志：写入 server.log，print 失败则忽略。"""
    line = str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), line))
    except Exception:
        pass


def main():
    argv = sys.argv[1:]
    # 打包成 exe 默认走原生窗口；源码运行默认走本地服务（便于开发/调试）。
    # 可用 --webview / --server 显式切换。
    webview_mode = bool(getattr(sys, 'frozen', False)) or ('--webview' in argv)
    if '--server' in argv:
        webview_mode = False
    if webview_mode:
        try:
            launch_webview()
            return
        except Exception as e:
            msg = '[警告] 原生窗口启动失败：%s；回退到本地服务模式。' % e
            log(msg)
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    '%s\n\n将回退到本地服务模式（会自动打开浏览器）。\n'
                    '若系统缺少 Edge WebView2 运行时，请先安装后再使用原生窗口。' % e,
                    'Space Haven 存档修改器', 0x30)
            except Exception:
                pass
        # 回退到本地服务模式
    port, srv = pick_port()
    url = 'http://127.0.0.1:%d/' % port
    log('=' * 60)
    log('  Space Haven 存档修改器')
    log('  地址: %s' % url)
    log('  存档: %s' % (SAVE_ROOT or '（未设置，请在界面中指定）'))
    log('  按 Ctrl+C 停止')
    log('=' * 60)
    if not os.environ.get('SPACE_HAVEN_NO_BROWSER'):
        threading_timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        threading_timer.daemon = True
        threading_timer.start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        try:
            with open(os.path.join(BASE, 'crash.log'), 'a', encoding='utf-8') as f:
                f.write('CRASH %s\n' % datetime.datetime.now().isoformat())
                f.write(traceback.format_exc())
                f.write('\n')
        except Exception:
            pass
        raise
