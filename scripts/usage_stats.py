#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多平台 AI 用量统计：一份脚本统计本机所有 AI 编码工具的 token 与花费。

设计思路
    少数平台用精确解析器：WorkBuddy 能拿到服务端返回的真实扣费积分，
    Codex CLI 需要按 last_token_usage 增量累加，两者字段独特、值得专门适配。
    其余平台一律走通用解析器：在日志 JSON 里自动寻找形似 usage 的对象，
    不依赖具体工具名。所以 Claude Code / Gemini CLI / Qwen Code / OpenCode /
    Crush / Goose 等只要日志里有 token 字段，无需改代码即可统计。

用法
    python usage_stats.py                     # 交互菜单（终端里直接运行）
    python usage_stats.py --days 7            # 近 7 日汇总表
    python usage_stats.py --days 30           # 近 30 日
    python usage_stats.py --date today        # 今日各模型分布
    python usage_stats.py --by-source         # 按平台对比
    python usage_stats.py --scan              # 数据源诊断（推荐首次运行）
    python usage_stats.py --json              # 机器可读
    python usage_stats.py --interactive       # 表后循环下钻（回车看今日，x 退出）
    python usage_stats.py --only workbuddy,codex
    python usage_stats.py --full              # 忽略 mtime 预筛，扫描全部历史文件

口径
    积分  取 providerData.rawUsage.credit（服务端返回的单次调用实扣值，
          已按峰谷折扣计算，直接累加，不可再乘 0.5）。仅 WorkBuddy 有积分。
    峰谷  周一至周五 09:00-12:00、14:00-18:00 为原价；其余时段（含周末）5 折。
          判定统一按 --tz 指定的时区（默认 UTC+8）。
    金额  按 deepseek-v4.1-flash 牌价折算（元/百万 token）：
          输入未命中 2.0、输出 8.0、缓存命中 0.04、缓存写入 1.25×输入；
          高峰系数 1.0、非高峰 0.5。这是横向可比用的统一基准，非实际扣费。

依赖
    Python 3.8+ 标准库，无需安装任何包，不联网。
"""

import argparse
import collections
import datetime
import json
import os
import sys
import time
import unicodedata

VERSION = '1.1.0'
WEEK = ['一', '二', '三', '四', '五', '六', '日']

# deepseek-v4.1-flash 牌价（元 / 百万 token），作为统一折算基准
IN_P = 2.0
OUT_P = 8.0
HIT_P = 0.04
WRITE_MULT = 1.25

DEFAULT_TZ = 8.0
CREDIT_YUAN = 60.0 / 7000   # 7000 积分 = 60 元

COLS = (12, 4, 14, 8, 7, 10, 8, 8, 12)
COLS_M = (26, 14, 10, 9, 8)
COLS_S = (18, 30, 8, 9, 12, 18)

# 通用解析器识别的字段（按优先级）
USAGE_IN = ('input_tokens', 'prompt_tokens', 'inputTokens', 'promptTokens')
USAGE_OUT = ('output_tokens', 'completion_tokens', 'outputTokens', 'completionTokens')
USAGE_HIT = ('cache_read_input_tokens', 'prompt_cache_hit_tokens', 'cached_input_tokens',
             'cacheReadTokens', 'promptCacheHitTokens', 'cached_tokens')
USAGE_WRT = ('cache_creation_input_tokens', 'cache_write_input_tokens',
             'cacheWriteTokens', 'prompt_cache_write_tokens')
USAGE_TOT = ('total_tokens', 'totalTokens')
USAGE_ALL = USAGE_IN + USAGE_OUT + USAGE_HIT + USAGE_WRT + USAGE_TOT
# Anthropic 系：input_tokens 不含缓存，需要相加
ANTHROPIC_KEYS = ('cache_read_input_tokens', 'cache_creation_input_tokens')

TS_KEYS = ('timestamp', 'created_at', 'createdAt', 'createTime', 'create_time',
           'time', 'ts', 'date', 'startTime', 'start_time')
ID_KEYS = ('requestId', 'request_id', 'id', 'uuid')


# ---------------------------------------------------------------- 终端样式

class C:
    R = '\033[0m'
    B = '\033[1m'
    D = '\033[2m'
    CY = '\033[36m'
    GR = '\033[32m'
    YE = '\033[33m'
    RD = '\033[31m'


def enable_ansi():
    if os.name == 'nt':
        try:
            os.system('')
        except Exception:
            pass


def has_ansi():
    if os.environ.get('NO_COLOR'):
        return False
    return sys.stdout.isatty()


def c(txt, code):
    return code + txt + C.R if has_ansi() else txt


# ---------------------------------------------------------------- 格式化工具

def dwidth(s):
    w = 0
    for ch in str(s):
        w += 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1
    return w


def pad(s, width, right=True):
    s = str(s)
    gap = width - dwidth(s)
    if gap <= 0:
        return s
    return (' ' * gap + s) if right else (s + ' ' * gap)


def fmt(n):
    if n >= 1e8:
        return "%.2f 亿" % (n / 1e8)
    if n >= 1e4:
        return "{:,.1f} 万".format(n / 1e4)
    if n == 0:
        return "—"
    return "{:,}".format(n)


def pct(a, b):
    return "%.1f%%" % (a / b * 100) if b else "—"


def gnum(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


# ---------------------------------------------------------------- 时间

def parse_ts(v):
    """把各种时间表示统一成 float 时间戳。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if v > 1e12:
            return v / 1000.0
        if v > 1e9:
            return float(v)
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        for f in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ'):
            try:
                return datetime.datetime.strptime(s, f).replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except Exception:
                pass
        for f in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
                  '%Y-%m-%d %H:%M:%S.%f'):
            try:
                return datetime.datetime.strptime(s, f).replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except Exception:
                pass
        try:
            return float(s) / 1000.0
        except Exception:
            return None
    return None


def to_dt(ts, tz):
    """UTC 时间戳 → 指定时区的 naive datetime（避开 utcfromtimestamp 弃用告警）。"""
    utc = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).replace(tzinfo=None)
    return utc + datetime.timedelta(hours=tz)


def is_peak(dt):
    return dt.weekday() < 5 and ((9 <= dt.hour < 12) or (14 <= dt.hour < 18))


def rec_ts(o):
    """从一条记录里找时间戳。"""
    for k in TS_KEYS:
        if k in o:
            t = parse_ts(o.get(k))
            if t:
                return t
    return None


# ---------------------------------------------------------------- 聚合骨架

def blank_seg():
    return {'tok': 0, 'inp': 0, 'out': 0, 'hit': 0, 'wrt': 0, 'cr': 0.0, 'n': 0}


def blank_day():
    return {'tok': 0, 'turns': 0, 'sess': set(), 'cr': 0.0, 'inp': 0, 'hit': 0,
            'models': collections.Counter(), 'mcredit': collections.Counter(),
            'mhit': collections.Counter(), 'minp': collections.Counter(),
            'pk': blank_seg(), 'op': blank_seg()}


def acc(day, d, dt, tok, inp, hit, out, wrt, cr, mdl, sid):
    """把一次调用累加进当日的总账与峰谷分段。"""
    r = day[d]
    seg = r['pk'] if is_peak(dt) else r['op']
    r['tok'] += tok
    r['turns'] += 1
    r['sess'].add(sid)
    r['cr'] += cr
    r['inp'] += inp
    r['hit'] += hit
    seg['tok'] += tok
    seg['n'] += 1
    seg['cr'] += cr
    seg['inp'] += inp
    seg['hit'] += hit
    seg['wrt'] += wrt
    seg['out'] += out
    r['models'][mdl] += tok
    r['mcredit'][mdl] += cr
    r['minp'][mdl] += inp
    r['mhit'][mdl] += hit


# ---------------------------------------------------------------- 文件遍历

def iter_files(root, cutoff=None):
    """列出目录下的 .jsonl / .json。cutoff 为 mtime 下界，用于跳过老旧文件。"""
    out = []
    for dp, _, fns in os.walk(root):
        for fn in fns:
            low = fn.lower()
            if not (low.endswith('.jsonl') or low.endswith('.json')):
                continue
            fp = os.path.join(dp, fn)
            if cutoff is not None:
                try:
                    if os.path.getmtime(fp) < cutoff:
                        continue
                except OSError:
                    continue
            out.append(fp)
    out.sort()
    return out


# ---------------------------------------------------------------- 精确解析器

def collect_workbuddy(root, tz, cutoff=None):
    """WorkBuddy：providerData.rawUsage（含 credit 实扣积分）。"""
    day = collections.defaultdict(blank_day)
    files = iter_files(root, cutoff)
    for fp in files:
        try:
            fh = open(fp, encoding='utf-8', errors='ignore')
        except Exception:
            continue
        sid = os.path.splitext(os.path.basename(fp))[0]
        with fh:
            for line in fh:
                if 'providerData' not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                pd = o.get('providerData')
                if not isinstance(pd, dict):
                    continue
                ru = pd.get('rawUsage') or {}
                u = pd.get('usage') or {}
                tt = ru.get('total_tokens') or u.get('totalTokens')
                if not tt:
                    continue
                ts = parse_ts(o.get('timestamp'))
                if not ts:
                    continue
                cr = ru.get('credit')
                if cr is None:
                    cr = pd.get('credit')
                cr = float(cr) if isinstance(cr, (int, float)) else 0.0
                inp = gnum(ru, 'prompt_tokens', 'inputTokens', 'promptTokens')
                hit = gnum(ru, 'prompt_cache_hit_tokens', 'cacheReadTokens',
                           'promptCacheHitTokens')
                out = gnum(ru, 'completion_tokens', 'outputTokens', 'completionTokens')
                wrt = gnum(ru, 'cache_creation_input_tokens', 'cacheWriteTokens',
                           'prompt_cache_write_tokens')
                mdl = (pd.get('model') or pd.get('requestModelId')
                       or pd.get('requestModelName') or 'unknown')
                dt = to_dt(ts, tz)
                acc(day, dt.strftime('%Y-%m-%d'), dt, int(tt), inp, hit, out, wrt, cr, mdl, sid)
    return day, len(files)


def collect_codex(root, tz, cutoff=None):
    """Codex CLI：token_count 事件按 last_token_usage 增量累加。"""
    day = collections.defaultdict(blank_day)
    if not root or not os.path.isdir(root):
        return day, 0
    files = iter_files(root, cutoff)
    for fp in files:
        try:
            fh = open(fp, encoding='utf-8', errors='ignore')
        except Exception:
            continue
        sid = os.path.splitext(os.path.basename(fp))[0]
        with fh:
            for line in fh:
                if 'token_count' not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                p = o.get('payload') or {}
                if p.get('type') != 'token_count':
                    continue
                lu = (p.get('info') or {}).get('last_token_usage') or {}
                if not lu:
                    continue
                ts = parse_ts(o.get('timestamp'))
                if not ts:
                    continue
                inp = int(lu.get('input_tokens') or 0)
                hit = int(lu.get('cached_input_tokens') or 0)
                out = (int(lu.get('output_tokens') or 0)
                       + int(lu.get('reasoning_output_tokens') or 0))
                wrt = int(lu.get('cache_write_input_tokens') or 0)
                tok = int(lu.get('total_tokens') or 0) or (inp + out)
                mdl = lu.get('model') or 'codex'
                dt = to_dt(ts, tz)
                acc(day, dt.strftime('%Y-%m-%d'), dt, tok, inp, hit, out, wrt, 0.0, mdl, sid)
    return day, len(files)


# ---------------------------------------------------------------- 通用解析器

def _extract_usage(obj):
    """在任意 JSON 结构里找最像 usage 的对象，返回 (usage, model, uid)。"""
    best = None   # (score, depth, usage, model)
    stack = [(obj, 0, None)]
    while stack:
        node, depth, model = stack.pop()
        if depth > 8:
            continue
        if isinstance(node, dict):
            m = node.get('model') or node.get('modelName')
            if isinstance(m, str) and m:
                model = m
            score = 0
            for k in USAGE_ALL:
                if isinstance(node.get(k), (int, float)):
                    score += 1
            has_out = any(k in node for k in USAGE_OUT)
            has_in = any(k in node for k in USAGE_IN)
            has_tot = any(k in node for k in USAGE_TOT)
            # 纯 total 的配置项不算；必须能看出是输入/输出结构
            if score and (has_out or (has_tot and has_in)):
                if best is None or score > best[0] or (score == best[0] and depth < best[1]):
                    best = (score, depth, node, model)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1, model))
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    stack.append((v, depth + 1, model))
    if not best:
        return None, None, None
    usage = best[2]
    uid = None
    msg = obj.get('message') if isinstance(obj, dict) else None
    if isinstance(msg, dict):
        for k in ID_KEYS:
            if isinstance(msg.get(k), str):
                uid = msg[k]
                break
    if not uid and isinstance(obj, dict):
        for k in ID_KEYS:
            if isinstance(obj.get(k), str):
                uid = obj[k]
                break
    return usage, best[3] or 'unknown', uid


def _usage_nums(u):
    """把 usage 字典归一化为 (tok, inp, hit, out, wrt)。"""
    base_in = gnum(u, *USAGE_IN)
    hit = gnum(u, *USAGE_HIT)
    wrt = gnum(u, *USAGE_WRT)
    out = gnum(u, *USAGE_OUT)
    tot = gnum(u, *USAGE_TOT)
    anthropic = any(k in u for k in ANTHROPIC_KEYS)
    if anthropic:
        inp = base_in + hit + wrt
    else:
        inp = base_in
    if not inp and tot:
        inp = max(0, tot - out)
    tok = tot or (inp + out)
    return tok, inp, hit, out, wrt


def collect_generic(root, tz, cutoff=None):
    """通用解析器：不认工具名，只认 JSON 里的 usage 结构。"""
    day = collections.defaultdict(blank_day)
    if not root or not os.path.isdir(root):
        return day, 0
    files = iter_files(root, cutoff)
    seen = set()

    def one(obj, sid, idx):
        if not isinstance(obj, dict):
            return
        uso, mdl, uid = _extract_usage(obj)
        if not uso:
            return
        ts = rec_ts(obj)
        if not ts:
            return
        key = (sid, uid) if uid else (sid, idx)
        if key in seen:
            return
        seen.add(key)
        tok, inp, hit, out, wrt = _usage_nums(uso)
        if tok <= 0:
            return
        dt = to_dt(ts, tz)
        acc(day, dt.strftime('%Y-%m-%d'), dt, tok, inp, hit, out, wrt, 0.0, mdl, sid)

    for fp in files:
        sid = os.path.splitext(os.path.basename(fp))[0]
        if fp.lower().endswith('.json'):
            try:
                with open(fp, encoding='utf-8', errors='ignore') as fh:
                    blob = json.load(fh)
            except Exception:
                continue
            items = blob if isinstance(blob, list) else [blob]
            for i, it in enumerate(items):
                one(it, sid, i)
            continue
        try:
            fh = open(fp, encoding='utf-8', errors='ignore')
        except Exception:
            continue
        with fh:
            for i, line in enumerate(fh):
                if 'token' not in line and 'usage' not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                one(o, sid, i)
    return day, len(files)


# ---------------------------------------------------------------- 数据源登记

SOURCES = [
    {'id': 'workbuddy', 'name': 'WorkBuddy', 'kind': 'workbuddy',
     'path': '~/.workbuddy/projects', 'billing': '积分制 · 真实扣费'},
    {'id': 'codex', 'name': 'Codex CLI', 'kind': 'codex',
     'path': '~/.codex/sessions', 'billing': '订阅制 · 不扣积分'},
    {'id': 'claude', 'name': 'Claude Code', 'kind': 'generic',
     'path': '~/.claude/projects', 'billing': '订阅制'},
    {'id': 'qoderwork', 'name': 'QoderWork', 'kind': 'generic',
     'path': '~/.qoderworkcn/projects', 'billing': '订阅制'},
    {'id': 'qoder', 'name': 'Qoder', 'kind': 'generic',
     'path': '~/.qoder/projects', 'billing': '订阅制'},
    {'id': 'gemini', 'name': 'Gemini CLI', 'kind': 'generic',
     'path': '~/.gemini/tmp', 'billing': '免费额度'},
    {'id': 'qwen', 'name': 'Qwen Code', 'kind': 'generic',
     'path': '~/.qwen/tmp', 'billing': '免费额度'},
    {'id': 'opencode', 'name': 'OpenCode', 'kind': 'generic',
     'path': '~/.local/share/opencode/storage', 'billing': '按量'},
    {'id': 'crush', 'name': 'Crush', 'kind': 'generic',
     'path': '~/.local/share/crush', 'billing': '按量'},
    {'id': 'goose', 'name': 'Goose', 'kind': 'generic',
     'path': '~/.local/share/goose/sessions', 'billing': '按量'},
    {'id': 'aider', 'name': 'Aider', 'kind': 'generic',
     'path': '~/.aider', 'billing': '按量'},
]

PARSERS = {'workbuddy': collect_workbuddy,
           'codex': collect_codex,
           'generic': collect_generic}


def load_config(path=None):
    """读取 ~/.ai-usage-stats.json 里的路径覆盖，缺省返回空。"""
    p = path or os.path.expanduser('~/.ai-usage-stats.json')
    try:
        with open(p, encoding='utf-8') as fh:
            cfg = json.load(fh)
        paths = cfg.get('paths') or {}
        return paths if isinstance(paths, dict) else {}
    except Exception:
        return {}


def detect(paths, skip):
    """探测各平台数据源，补上 root / ok / files。"""
    out = []
    for s in SOURCES:
        if s['id'] in skip:
            continue
        root = os.path.expanduser(paths.get(s['id']) or s['path'])
        s2 = dict(s)
        s2['root'] = root
        s2['ok'] = os.path.isdir(root)
        s2['files'] = len(iter_files(root)) if s2['ok'] else 0
        out.append(s2)
    return out


def load_all(sources, tz, cutoff=None):
    data = {}
    for s in sources:
        if not s['ok']:
            data[s['id']] = collections.defaultdict(blank_day)
            continue
        data[s['id']] = PARSERS[s['kind']](s['root'], tz, cutoff)[0]
    return data


def merge(data, ids=None):
    """合并多个平台的日账（token/命中率相加，积分只来自 WorkBuddy）。"""
    out = collections.defaultdict(blank_day)
    for k, days in data.items():
        if ids and k not in ids:
            continue
        for d, r in days.items():
            o = out[d]
            o['tok'] += r['tok']
            o['turns'] += r['turns']
            o['sess'] |= r['sess']
            o['cr'] += r['cr']
            o['inp'] += r['inp']
            o['hit'] += r['hit']
            o['models'].update(r['models'])
            o['mcredit'].update(r['mcredit'])
            o['mhit'].update(r['mhit'])
            o['minp'].update(r['minp'])
            for seg in ('pk', 'op'):
                for k2 in ('tok', 'inp', 'out', 'hit', 'wrt', 'cr', 'n'):
                    o[seg][k2] += r[seg][k2]
    return out


# ---------------------------------------------------------------- 计费

def seg_cost(s, pf):
    inp, out, hit, wrt = s['inp'], s['out'], s['hit'], s['wrt']
    hit = min(hit, inp)
    wrt = min(wrt, max(0, inp - hit))
    miss = max(0, inp - hit - wrt)
    return (hit * HIT_P + wrt * WRITE_MULT * IN_P + miss * IN_P + out * OUT_P) * pf / 1e6


def day_cost(r):
    return seg_cost(r['pk'], 1.0) + seg_cost(r['op'], 0.5)


# ---------------------------------------------------------------- 报表

def today_in(tz):
    return to_dt(datetime.datetime.now(datetime.timezone.utc).timestamp(), tz).date()


def date_list(days, tz):
    t = today_in(tz)
    return [(t - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)], t


def build_rows(merged, days):
    rows = []
    for d in reversed(days):
        r = merged.get(d)
        if not r or not r['tok']:
            rows.append({'date': d, 'empty': True})
            continue
        rows.append({
            'date': d,
            'empty': False,
            'weekday': WEEK[datetime.datetime.strptime(d, '%Y-%m-%d').weekday()],
            'tokens': r['tok'],
            'turns': r['turns'],
            'sessions': len(r['sess']),
            'credit': r['cr'],
            'op_credit': r['op']['cr'],
            'idle_share': r['op']['tok'] / r['tok'] if r['tok'] else 0.0,
            'hit': r['hit'],
            'inp': r['inp'],
            'hit_rate': (r['hit'] / r['inp']) if r['inp'] else None,
            'cost': day_cost(r),
        })
    return rows


def print_table(rows, title=None):
    print(title or ("近 %d 日 token 用量" % len(rows)))
    print("=" * 83)
    print(pad("日期", COLS[0], False) + pad("周", COLS[1], False) + pad("Token", COLS[2])
          + pad("轮次", COLS[3]) + pad("会话", COLS[4]) + pad("积分", COLS[5])
          + pad("闲时%", COLS[6]) + pad("命中率", COLS[7]) + pad("预估金额", COLS[8]))
    for r in rows:
        if r['empty']:
            print(pad(r['date'], COLS[0], False) + pad("—", COLS[1], False) + pad("—", COLS[2])
                  + pad(0, COLS[3]) + pad(0, COLS[4]) + pad("—", COLS[5])
                  + pad("—", COLS[6]) + pad("—", COLS[7]) + pad("—", COLS[8]))
            continue
        print(pad(r['date'], COLS[0], False) + pad(r['weekday'], COLS[1], False)
              + pad(fmt(r['tokens']), COLS[2]) + pad(r['turns'], COLS[3])
              + pad(r['sessions'], COLS[4]) + pad("%.1f" % r['credit'], COLS[5])
              + pad("%.0f%%" % (r['idle_share'] * 100), COLS[6])
              + pad("%.1f%%" % (r['hit_rate'] * 100) if r['hit_rate'] is not None else "—", COLS[7])
              + pad("¥%.2f" % r['cost'], COLS[8]))
    tot_tok = sum(r.get('tokens', 0) for r in rows)
    tot_cr = sum(r.get('credit', 0.0) for r in rows)
    tot_cost = sum(r.get('cost', 0.0) for r in rows)
    tot_hit = sum(r.get('hit', 0) for r in rows)
    tot_inp = sum(r.get('inp', 0) for r in rows)
    op_cr = sum(r.get('op_credit', 0.0) for r in rows)
    print("-" * 83)
    print(pad("近 %d 天合计" % len(rows), COLS[0] + COLS[1], False) + pad(fmt(tot_tok), COLS[2])
          + pad("", COLS[3]) + pad("", COLS[4]) + pad("%.1f" % tot_cr, COLS[5])
          + pad("", COLS[6]) + pad(pct(tot_hit, tot_inp), COLS[7])
          + pad("¥%.2f" % tot_cost, COLS[8]))
    print("  非高峰实扣 %.1f 分（原价 %.1f 分，因 5 折省下 %.1f 分）"
          % (op_cr, op_cr * 2, op_cr))
    print("  积分折合约 ¥%.2f（按 7000 积分/月 = 60 元）" % (tot_cr * CREDIT_YUAN))


def print_models(merged, d):
    r = merged.get(d)
    if not r or not r['tok']:
        print("\n【%s】无数据" % d)
        return
    total = r['tok']
    print("\n【%s 各模型用量】" % d)
    print(pad("模型", COLS_M[0], False) + pad("Token", COLS_M[1]) + pad("积分", COLS_M[2])
          + pad("命中率", COLS_M[3]) + pad("占比", COLS_M[4]))
    for m, v in r['models'].most_common():
        cr = r['mcredit'].get(m, 0.0)
        print(pad(m, COLS_M[0], False) + pad(fmt(v), COLS_M[1])
              + pad(("%.1f" % cr) if cr else "不扣", COLS_M[2])
              + pad(pct(r['mhit'].get(m, 0), r['minp'].get(m, 0)), COLS_M[3])
              + pad(pct(v, total), COLS_M[4]))
    print(pad("合计", COLS_M[0], False) + pad(fmt(total), COLS_M[1])
          + pad("%.1f" % r['cr'], COLS_M[2]) + pad(pct(r['hit'], r['inp']), COLS_M[3]))
    print("  其中高峰 %.1f 分（原价）/ 非高峰 %.1f 分（已 5 折实扣，原价 %.1f 分）"
          % (r['pk']['cr'], r['op']['cr'], r['op']['cr'] * 2))


def print_by_source(data, sources, days):
    """按平台对比（近 N 日）。未检测到的平台在末尾汇总一行。"""
    print("\n【按平台对比 · 近 %d 日】" % len(days))
    W = (16, 14, 8, 10, 9, 11)
    print(pad("平台", W[0], False) + pad("Token", W[1]) + pad("占比", W[2])
          + pad("积分", W[3]) + pad("命中率", W[4]) + pad("估算金额", W[5]))
    lo, hi = min(days), max(days)
    rows = []
    missing = []
    for s in sources:
        if not s['ok']:
            missing.append(s['name'])
            continue
        dmap = data.get(s['id'], {})
        tok = cr = hit = inp = 0
        cost = 0.0
        for d, r in dmap.items():
            if not (lo <= d <= hi):
                continue
            tok += r['tok']
            cr += r['cr']
            hit += r['hit']
            inp += r['inp']
            cost += day_cost(r)
        rows.append((s, tok, cr, hit, inp, cost))
    total_tok = sum(r[1] for r in rows) or 0
    for s, tok, cr, hit, inp, cost in rows:
        if not tok:
            print(pad(s['name'], W[0], False) + pad("无记录", W[1]) + pad("—", W[2])
                  + pad("—", W[3]) + pad("—", W[4]) + pad("—", W[5]))
            continue
        print(pad(s['name'], W[0], False) + pad(fmt(tok), W[1])
              + pad(pct(tok, total_tok), W[2]) + pad(("%.1f" % cr) if cr else "不扣", W[3])
              + pad(pct(hit, inp), W[4]) + pad("¥%.2f" % cost, W[5]))
    print("-" * sum(W))
    print(pad("合计", W[0], False) + pad(fmt(total_tok), W[1]) + pad("100%", W[2])
          + pad("%.1f" % sum(r[2] for r in rows), W[3])
          + pad(pct(sum(r[3] for r in rows), sum(r[4] for r in rows)), W[4])
          + pad("¥%.2f" % sum(r[5] for r in rows), W[5]))
    if missing:
        print("  未检测到：%s" % "、".join(missing))


def print_scan(sources, data):
    """数据源诊断：路径、文件数、可解析记录数、token。"""
    print("\n【数据源诊断】")
    print(pad("平台", COLS_S[0], False) + pad("路径", COLS_S[1], False) + " "
          + pad("文件", COLS_S[2]) + pad("记录", COLS_S[3])
          + pad("Token", COLS_S[4]) + "  " + pad("状态", COLS_S[5], False))
    for s in sources:
        dmap = data.get(s['id'], {})
        tok = sum(r['tok'] for r in dmap.values())
        rec = sum(r['turns'] for r in dmap.values())
        path = s['root'].replace(os.path.expanduser('~'), '~')
        if dwidth(path) > 32:
            path = path[:14] + '…' + path[-16:]
        if not s['ok']:
            state = "未检测到"
        elif not rec:
            state = "无用量记录"
        else:
            state = "正常"
        print(pad(s['name'], COLS_S[0], False) + pad(path, COLS_S[1], False) + " "
              + pad(s['files'] if s['ok'] else "—", COLS_S[2])
              + pad(rec if s['ok'] else "—", COLS_S[3])
              + pad(fmt(tok), COLS_S[4]) + "  " + pad(state, COLS_S[5], False))
    print("\n  「无用量记录」= 目录存在但日志里没有 token 字段（该工具不记录用量），属正常。")
    print("  新增平台：在该文件 SOURCES 里加一行 id/name/path，kind 用 generic 即可。")


def emit_json(data, days, sources, tz):
    merged = merge(data)
    rows = build_rows(merged, days)
    out = {
        'version': VERSION,
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'tz_offset': tz,
        'sources': [{'id': s['id'], 'name': s['name'], 'detected': s['ok'],
                     'files': s['files'], 'billing': s['billing']} for s in sources],
        'daily': rows,
        'models': [],
    }
    for d in sorted(merged):
        r = merged[d]
        for m, v in r['models'].items():
            out['models'].append({
                'date': d, 'model': m, 'tokens': v,
                'credit': round(r['mcredit'].get(m, 0.0), 2),
                'hit_rate': round(r['mhit'].get(m, 0) / r['minp'][m], 4)
                if r['minp'].get(m) else None,
            })
    print(json.dumps(out, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 菜单

def banner():
    line = "=" * 58
    print()
    print(c("  " + line, C.CY))
    print(c("   ", C.CY) + c("AI Token / 积分用量统计  v%s" % VERSION, C.B))
    print(c("  " + line, C.CY))


def menu(sources, data, tz):
    while True:
        banner()
        print(c("  数据源", C.B))
        for s in sources:
            if s['ok']:
                rec = sum(r['turns'] for r in data.get(s['id'], {}).values())
                if rec:
                    print("   %s %-13s %s" % (c("●", C.GR), s['name'],
                                              c("%d 个文件 · %d 条记录" % (s['files'], rec), C.D)))
                else:
                    print("   %s %-13s %s" % (c("◐", C.YE), s['name'],
                                              c("%d 个文件 · 无用量记录" % s['files'], C.D)))
            else:
                print("   %s %-13s %s" % (c("○", C.D), s['name'], c("未检测到", C.D)))
        print(c("  " + "-" * 56, C.D))
        print("   1. 近 7 日汇总        2. 近 30 日汇总")
        print("   3. 今日模型分布        4. 按平台对比")
        print("   5. 查询指定日期        6. 导出 JSON")
        print("   7. 数据源诊断          0. 退出")
        try:
            ch = input(c("\n  请选择 > ", C.YE)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ch == '0':
            return
        elif ch in ('1', '2'):
            n = 7 if ch == '1' else 30
            days, _ = date_list(n, tz)
            print_table(build_rows(merge(data), days))
        elif ch == '3':
            days, _ = date_list(1, tz)
            print_models(merge(data), days[0])
        elif ch == '4':
            days, _ = date_list(7, tz)
            print_by_source(data, sources, days)
        elif ch == '5':
            q = input("  日期（today / 1023 / 2025-03-13）> ").strip()
            _, t = date_list(1, tz)
            if q.lower() == 'today':
                q = t.strftime('%Y-%m-%d')
            elif len(q) == 4:
                q = '%s-%s-%s' % (t.year, q[:2], q[2:])
            print_models(merge(data), q)
        elif ch == '6':
            days, _ = date_list(7, tz)
            path = os.path.join(os.getcwd(), 'ai-usage-%s.json'
                                % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
            merged = merge(data)
            rec = {'version': VERSION,
                   'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
                   'daily': build_rows(merged, days)}
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=2)
            print("  已导出: %s" % path)
        elif ch == '7':
            print_scan(sources, data)
        else:
            print("  无效选项。")


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(
        description="多平台 AI 用量统计（WorkBuddy / Codex / Claude Code / 通用）")
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--date', help='指定日模型分布：today / 1023 / 2025-03-13')
    ap.add_argument('--by-source', action='store_true', help='按平台对比')
    ap.add_argument('--scan', action='store_true', help='数据源诊断')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--interactive', action='store_true', help='循环下钻（回车看今日，x 退出）')
    ap.add_argument('--menu', action='store_true', help='强制进入交互菜单')
    ap.add_argument('--full', action='store_true', help='不做 mtime 预筛，扫描全部历史')
    ap.add_argument('--only', help='只统计指定平台，逗号分隔')
    ap.add_argument('--exclude', help='排除指定平台，逗号分隔')
    ap.add_argument('--config', help='配置文件路径，默认 ~/.ai-usage-stats.json')
    ap.add_argument('--projects', help='WorkBuddy 日志目录')
    ap.add_argument('--codex', help='Codex CLI 会话目录')
    ap.add_argument('--claude', help='Claude Code 日志目录')
    ap.add_argument('--tz', type=float, default=DEFAULT_TZ, help='峰谷判定时区，默认 8')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    enable_ansi()

    paths = load_config(args.config)
    for k, v in (('workbuddy', args.projects), ('codex', args.codex),
                 ('claude', args.claude)):
        if v:
            paths[k] = v

    skip = set()
    if args.exclude:
        skip.update(x.strip() for x in args.exclude.split(',') if x.strip())
    if args.only:
        want = set(x.strip() for x in args.only.split(',') if x.strip())
        skip.update(s['id'] for s in SOURCES if s['id'] not in want)

    sources = detect(paths, skip)

    # mtime 预筛：多留 3 天冗余，避免跨时区/长会话截断
    cutoff = None
    if not args.full and not args.scan:
        span = args.days + 3
        if args.date and args.date.lower() != 'today' and len(args.date) == 10:
            try:
                d0 = datetime.datetime.strptime(args.date, '%Y-%m-%d').date()
                span = max(span, (today_in(args.tz) - d0).days + 3)
            except Exception:
                pass
        cutoff = time.time() - span * 86400

    data = load_all(sources, args.tz, cutoff)
    days, today = date_list(args.days, args.tz)

    if args.menu:
        menu(sources, data, args.tz)
        return 0

    if args.scan:
        print_scan(sources, data)
        return 0

    if args.date:
        q = args.date.strip()
        if q.lower() == 'today':
            q = days[0]
        elif len(q) == 4:
            q = '%s-%s-%s' % (today.year, q[:2], q[2:])
        print_models(merge(data), q)
        return 0

    if args.by_source:
        print_by_source(data, sources, days)
        return 0

    if args.json:
        emit_json(data, days, sources, args.tz)
        return 0

    if len(sys.argv) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        menu(sources, data, args.tz)
        return 0

    print_table(build_rows(merge(data), days))

    if args.interactive:
        print("\n按回车看模型分布，点 x 退出")
        merged = merge(data)
        while True:
            try:
                q = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() == 'x':
                break
            if not q:
                print_models(merged, days[0])
                continue
            if len(q) == 4:
                q = '%s-%s-%s' % (today.year, q[:2], q[2:])
            print_models(merged, q)
    return 0


if __name__ == '__main__':
    sys.exit(main())
