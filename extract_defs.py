# -*- coding: utf-8 -*-
"""Extract id -> CN name dictionaries from Space Haven jar + temp.lib."""
import zipfile, re, json, os, struct, sys

GAME = os.environ.get('SPACE_HAVEN_GAME', 'PATH_TO_YOUR_SPACE_HAVEN_INSTALL')  # 通过环境变量传入游戏安装目录，避免硬编码本机路径
JAR = os.path.join(GAME, 'spacehaven.jar')
TEMPLIB = os.path.join(GAME, 'temp.lib')

z = zipfile.ZipFile(JAR)

# ---- 1. texts: tid -> {cn, en} ----
texts_raw = z.read('library/texts').decode('utf-8', 'replace')
texts = {}
for m in re.finditer(r'<t id="(\d+)"[^>]*>(.*?)</t>', texts_raw, re.S):
    tid = int(m.group(1)); body = m.group(2)
    cn = re.search(r'<CN>(.*?)</CN>', body, re.S)
    en = re.search(r'<EN>(.*?)</EN>', body, re.S)
    texts[tid] = {'cn': cn.group(1).strip() if cn else '',
                  'en': en.group(1).strip() if en else ''}
print('texts', len(texts))

lib = open(TEMPLIB, encoding='utf-8', errors='replace').read()

def name_of(tid):
    t = texts.get(tid)
    return t['cn'] if t and t['cn'] else (t['en'] if t else '')


def en_of(tid):
    t = texts.get(tid)
    return t['en'] if t else ''

# ---- 2. items: products (eid) + items (mid) ----
items = {}
for m in re.finditer(r'<product eid="(\d+)"[^>]*>(.*?)</product>', lib, re.S):
    eid = int(m.group(1)); body = m.group(2)
    nm = re.search(r'<name tid="(\d+)"', body)
    if nm:
        items[eid] = {'cn': name_of(int(nm.group(1))), 'en': en_of(int(nm.group(1))), 'kind': 'raw'}
for m in re.finditer(r'<item mid="(\d+)"[^>]*>(.*?)</item>', lib, re.S):
    mid = int(m.group(1)); body = m.group(2)
    nm = re.search(r'<name tid="(\d+)"', body)
    if nm:
        e = items.get(mid) or {}
        e.update({'cn': name_of(int(nm.group(1))), 'en': en_of(int(nm.group(1))), 'kind': e.get('kind', 'item')})
        items[mid] = e
print('items', len(items))

# ---- 3. traits ----
traits = {}
for m in re.finditer(r'<trait id="(\d+)">(.*?)</trait>', lib, re.S):
    tid = int(m.group(1)); body = m.group(2)
    nm = re.search(r'<name tid="(\d+)"', body)
    de = re.search(r'<desc tid="(\d+)"', body)
    traits[tid] = {'cn': name_of(int(nm.group(1))) if nm else '',
                   'en': en_of(int(nm.group(1))) if nm else '',
                   'desc': name_of(int(de.group(1))) if de else '',
                   'descEn': en_of(int(de.group(1))) if de else ''}
print('traits', len(traits))

# ---- 4. conditions ----
conds = {}
for m in re.finditer(r'<condition id="(\d+)"[^>]*>(.*?)</condition>', lib, re.S):
    cid = int(m.group(1)); body = m.group(2)
    nm = re.search(r'<name tid="(\d+)"', body)
    if nm:
        conds[cid] = name_of(int(nm.group(1)))
if not conds:
    # alternate shape: <cond id=...><name tid=...>
    for m in re.finditer(r'<conds?\s+id="(\d+)"[^>]*>(.*?)</conds?>', lib, re.S):
        cid = int(m.group(1)); body = m.group(2)
        nm = re.search(r'<name tid="(\d+)"', body)
        if nm:
            conds[cid] = name_of(int(nm.group(1)))
print('conditions', len(conds))

# ---- 5. attributes: save uses tid as id ----
attrs = {}
for m in re.finditer(r'<attributes>(.*?)</attributes>', lib, re.S):
    for a in re.finditer(r'<l at="(\d+)"[^>]*>(.*?)</l>', m.group(1), re.S):
        pass
# simpler: scan <l at="N" ...> blocks inside <attributes>
mi = lib.find('<attributes>')
seg = lib[mi: mi+4000]
for a in re.finditer(r'<l at="(\d+)"[^>]*>(.*?)</l>', seg, re.S):
    nm = re.search(r'<name tid="(\d+)"', a.group(2))
    if nm:
        attrs[int(nm.group(1))] = {'cn': name_of(int(nm.group(1))), 'en': en_of(int(nm.group(1))),
                                   'type': (re.search(r'type="(\w+)"', a.group(0)) or [None, ''])[1] if False else (re.search(r'type="(\w+)"', a.group(0)).group(1) if re.search(r'type="(\w+)"', a.group(0)) else '')}
print('attributes', attrs)

# ---- 6. skills: decode Job$SkillClass <clinit> ----
def parse_cp(data):
    _, _, _, cpc = struct.unpack_from('>IHHH', data, 0)
    i = 10; cp = [None]*cpc; idx = 1
    while idx < cpc:
        tag = data[i]; i += 1
        if tag == 1:
            ln = struct.unpack_from('>H', data, i)[0]; i += 2
            cp[idx] = ('Utf8', data[i:i+ln].decode('utf-8', 'replace')); i += ln
        elif tag in (7, 8, 16, 19, 20):
            cp[idx] = (tag, struct.unpack_from('>H', data, i)[0]); i += 2
        elif tag == 3:
            cp[idx] = (tag, struct.unpack_from('>i', data, i)[0]); i += 4
        elif tag == 4:
            cp[idx] = (tag, struct.unpack_from('>f', data, i)[0]); i += 4
        elif tag in (9, 10, 11, 12, 17, 18):
            cp[idx] = (tag, struct.unpack_from('>HH', data, i)); i += 4
        elif tag in (5, 6):
            cp[idx] = (tag, struct.unpack_from('>q', data, i)[0]); i += 8; idx += 1
        elif tag == 15:
            cp[idx] = (tag, data[i], struct.unpack_from('>H', data, i+1)[0]); i += 3
        else:
            raise Exception('bad tag %d' % tag)
        idx += 1
    return cp, i

def ustr(cp, i):
    t = cp[i]
    if t is None: return None
    if t[0] == 'Utf8': return t[1]
    if t[0] in (7, 8): return ustr(cp, t[1])
    return None

def clinit_code(data):
    cp, i = parse_cp(data)
    a, t, s, ic = struct.unpack_from('>HHHH', data, i); i += 8 + ic*2
    fc = struct.unpack_from('>H', data, i)[0]; i += 2
    for _ in range(fc):
        i += 6
        n = struct.unpack_from('>H', data, i)[0]; i += 2
        for _ in range(n):
            i += 2; ln = struct.unpack_from('>I', data, i)[0]; i += 4 + ln
    mc = struct.unpack_from('>H', data, i)[0]; i += 2
    for _ in range(mc):
        _, ni, _ = struct.unpack_from('>HHH', data, i); i += 6
        is_clinit = ustr(cp, ni) == '<clinit>'
        n = struct.unpack_from('>H', data, i)[0]; i += 2
        for _ in range(n):
            an, ln = struct.unpack_from('>HI', data, i); i += 6
            body = data[i:i+ln]; i += ln
            if is_clinit and ustr(cp, an) == 'Code':
                _, _, clen = struct.unpack_from('>III', body, 0)
                return cp, body[12:12+clen]
    return cp, None

L1 = set([0x00, 0x01, 0x02] + list(range(0x03, 0x0e)) + list(range(0x1a, 0x36)) +
         list(range(0x3b, 0x60)) + list(range(0x60, 0x84)) + [0x85, 0x86, 0x87, 0x88,
         0x89, 0x8a, 0x8b, 0x8c, 0x8d, 0x8e, 0x8f, 0x90, 0x91, 0x92, 0x93, 0x94,
         0x95, 0x96, 0x97, 0x98, 0x9a, 0xac, 0xad, 0xae, 0xaf, 0xb0, 0xb1, 0xbe,
         0xbf, 0xc2, 0xc3, 0xc6, 0xc7, 0x59, 0x5a, 0x5b, 0x5c, 0x5d, 0x5e, 0x5f])
L2 = {0x10: 2, 0x11: 3, 0x15: 2, 0x16: 2, 0x17: 2, 0x18: 2, 0x19: 2, 0x36: 2,
      0x37: 2, 0x38: 2, 0x39: 2, 0x3a: 2, 0x99: 3, 0x9b: 3, 0x9c: 3, 0x9d: 3,
      0x9e: 3, 0x9f: 3, 0xa0: 3, 0xa1: 3, 0xa2: 3, 0xa3: 3, 0xa4: 3, 0xa5: 3,
      0xa6: 3, 0xa7: 3, 0xa8: 3, 0xc8: 5, 0xc9: 5}
L3 = {0x12: 2, 0x13: 3, 0x14: 3, 0x84: 3, 0xbb: 3, 0xb2: 3, 0xb3: 3, 0xb4: 3,
      0xb5: 3, 0xb6: 3, 0xb7: 3, 0xb8: 3, 0xb9: 5, 0xbc: 2, 0xbd: 3, 0xc0: 3,
      0xc1: 3, 0xc5: 4}

def oplen(op):
    if op == 0x12: return 2
    if op in L3: return L3[op]
    if op in L2: return L2[op]
    if op in L1: return 1
    return None

cp, code = clinit_code(z.read('fi/bugbyte/spacehaven/ai/Job$SkillClass.class'))
def cst(i):
    e = cp[i]
    if e is None: return None
    if e[0] == 'Utf8': return e[1]
    if e[0] == 8: return ustr(cp, e[1])
    if e[0] in (3, 4): return e[1]
    return None

skills = {}
k = 0; pushes = []
while k < len(code):
    op = code[k]; L = oplen(op)
    if L is None: break
    if op == 0x12: pushes.append(cst(code[k+1]))
    elif op == 0x13: pushes.append(cst(struct.unpack_from('>H', code, k+1)[0]))
    elif 0x03 <= op <= 0x08: pushes.append(op-3)
    elif op == 0x01: pushes.append(None)
    elif 0x0b <= op <= 0x0d: pushes.append(float(op-0x0b))
    elif op == 0x10: pushes.append(struct.unpack_from('>b', code, k+1)[0])
    elif op == 0x11: pushes.append(struct.unpack_from('>h', code, k+1)[0])
    elif op == 0xb7:
        # name, ordinal, show, textId, tooltip, saveNR, sort, learnFactor
        if len(pushes) >= 6:
            name, textid, savenr = pushes[0], pushes[3], pushes[5]
            if isinstance(name, str) and isinstance(savenr, int):
                cn = name_of(int(textid)) if isinstance(textid, str) and textid.isdigit() else ''
                skills[savenr] = {'cn': cn or name, 'en': (en_of(int(textid)) if isinstance(textid, str) and textid.isdigit() else '') or name, 'key': name, 'tid': textid}
        pushes = []
    k += L
print('skills', {k: v['cn'] for k, v in skills.items()})

out = {
    'game': GAME,
    'textsCount': len(texts),
    'items': {str(k): v for k, v in sorted(items.items())},
    'traits': {str(k): v for k, v in sorted(traits.items())},
    'conditions': {str(k): v for k, v in sorted(conds.items())},
    'attributes': {str(k): v for k, v in sorted(attrs.items())},
    'skills': {str(k): v for k, v in sorted(skills.items())},
}
with open('defs.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print('wrote defs.json', os.path.getsize('defs.json'))
