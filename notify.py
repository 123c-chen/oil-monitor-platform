"""
油液监测报警 - 企业微信推送脚本（本地独立运行）
用法:
  python notify.py              # 启动定时监测服务（每天 9:00）
  python notify.py --report     # 立即生成并发送周报（含趋势图）
  python notify.py --monthly    # 立即生成并发送月报（含趋势图）

功能：
- 每天 9:00 轮询 MDS 设备数据
- 阈值超标报警（颗粒度、温度、水含量、水活性）
- SQLite 本地数据库存储检测数据，用于趋势分析
- 连续一周指标上升趋势报警
- 每周一自动发送周报 + 趋势图
- 每月1日自动发送月报 + 趋势图
- 通过企业微信 Webhook 推送所有通知
- 自动去重，同一条报警不重复推送
"""

import json
import time
import ssl
import sys
import os
import sqlite3
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timedelta

def log(msg):
    """带刷新的打印"""
    print(msg, flush=True)

# ============================================================
# 配置
# ============================================================
API_BASE = 'https://mds.bodazl.com:8090'
API_USER = 'mds26061103'
API_PASS = '123456'
API_DEVICE = 56857

WEBHOOK_URL = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=c7ba1fb2-236a-4e20-8a69-9b2e0d1447bd'

# 每天 9:00
SCHEDULE_HOURS = [9]

# 报警阈值
THRESHOLDS = {
    'particle':     {'warning': 18,  'critical': 25},
    'temperature':  {'warning': 55,  'critical': 65},
    'waterContent': {'warning': 150, 'critical': 300},
    'waterActivity':{'warning': 0.7, 'critical': 0.85}
}

# 趋势检测
TREND_DAYS = 7
TREND_MIN_POINTS = 5

# 本地文件
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(SCRIPT_DIR, 'monitor_data.db')
CHART_FILE = os.path.join(SCRIPT_DIR, 'trend_chart.png')

# ============================================================
# 状态
# ============================================================
api_token = None
token_expiry = 0
notified_alarms = set()
notified_trends = set()

# SSL
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


# ============================================================
# 本地数据存储（SQLite）
# ============================================================
def init_db():
    """初始化 SQLite 数据库，创建表结构，并迁移旧 JSON 数据"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS monitor_data (
            timestamp TEXT PRIMARY KEY,
            level1 REAL, level2 REAL, level3 REAL, level4 REAL, level5 REAL,
            temperature REAL, waterContent REAL, waterActivity REAL,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    conn.commit()

    # 迁移旧 JSON 数据（仅首次）
    json_file = os.path.join(SCRIPT_DIR, 'monitor_data.json')
    if os.path.exists(json_file):
        try:
            with open(json_file, 'r', encoding='utf-8') as f:
                old = json.load(f).get('records', [])
            migrated = 0
            for r in old:
                try:
                    c.execute('''
                        INSERT OR IGNORE INTO monitor_data
                        (timestamp, level1, level2, level3, level4, level5,
                         temperature, waterContent, waterActivity)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        r.get('timestamp', ''),
                        r.get('level1', 0), r.get('level2', 0),
                        r.get('level3', 0), r.get('level4', 0), r.get('level5', 0),
                        r.get('temperature', 0), r.get('waterContent', 0),
                        r.get('waterActivity', 0)
                    ))
                    if c.rowcount > 0:
                        migrated += 1
                except Exception:
                    pass
            if migrated > 0:
                conn.commit()
                log(f'[DB] 已从 JSON 迁移 {migrated} 条记录')
            # 迁移完成后重命名旧文件，避免重复迁移
            backup = json_file + '.bak'
            os.rename(json_file, backup)
            log(f'[DB] 旧数据文件已备份: {backup}')
        except Exception as e:
            log(f'[DB] JSON 迁移失败: {e}')

    conn.close()
    log(f'[DB] 数据库已初始化: {DB_FILE}')


def load_data():
    """从 SQLite 读取所有记录，返回 list[dict]"""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('''
            SELECT timestamp, level1, level2, level3, level4, level5,
                   temperature, waterContent, waterActivity
            FROM monitor_data
            ORDER BY timestamp ASC
        ''')
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        log(f'[DB] 读取失败: {e}')
        return []


def append_record(record):
    """插入一条记录到 SQLite，自动去重，清理60天前数据，返回所有记录"""
    ts = record.get('timestamp', '')
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('''
            INSERT OR IGNORE INTO monitor_data
            (timestamp, level1, level2, level3, level4, level5,
             temperature, waterContent, waterActivity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            ts,
            record.get('level1', 0), record.get('level2', 0),
            record.get('level3', 0), record.get('level4', 0), record.get('level5', 0),
            record.get('temperature', 0), record.get('waterContent', 0),
            record.get('waterActivity', 0)
        ))
        if c.rowcount > 0:
            conn.commit()
            log(f'[DB] 已保存记录 {ts}')
        else:
            log(f'[DB] 记录已存在: {ts}，跳过')

        # 清理 60 天前的旧数据
        cutoff = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
        c.execute('DELETE FROM monitor_data WHERE timestamp < ?', (cutoff,))
        if c.rowcount > 0:
            conn.commit()
            log(f'[DB] 已清理 {c.rowcount} 条过期记录')

        conn.close()
    except Exception as e:
        log(f'[DB] 写入失败: {e}')

    return load_data()


# ============================================================
# HTTP 请求
# ============================================================
def http_request(method, url, body=None, headers=None):
    if headers is None:
        headers = {}
    headers['Content-Type'] = 'application/json'
    headers['Accept'] = 'application/json'
    if api_token and time.time() * 1000 < token_expiry:
        headers['X-Token'] = api_token
    data = json.dumps(body).encode('utf-8') if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        log(f'[HTTP] {e.code}: {e.read().decode("utf-8", errors="replace")}')
        return None
    except Exception as e:
        log(f'[HTTP] 错误: {e}')
        return None


def ensure_token():
    global api_token, token_expiry
    if api_token and time.time() * 1000 < token_expiry:
        return api_token
    result = http_request('POST', f'{API_BASE}/api/v1/login/login', {
        'username': API_USER, 'password': API_PASS
    })
    if result and result.get('code') == 0 and result.get('data', {}).get('token'):
        api_token = result['data']['token']
        token_expiry = time.time() * 1000 + 3600000
        log('[Token] 登录成功')
        return api_token
    log('[Token] 登录失败')
    return None


# ============================================================
# 企业微信推送
# ============================================================
def send_wechat(content):
    """发送 Markdown 消息"""
    body = json.dumps({
        'msgtype': 'markdown',
        'markdown': {'content': content}
    }).encode('utf-8')
    req = urllib.request.Request(WEBHOOK_URL, data=body, method='POST',
                                  headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            if result.get('errcode') == 0:
                log('[WeChat] 推送成功')
                return True
            else:
                log(f'[WeChat] 推送失败: {result}')
                return False
    except Exception as e:
        log(f'[WeChat] 请求错误: {e}')
        return False


def send_wechat_image_file(image_path):
    """读取图片文件，base64+md5 方式发送到企微 Webhook"""
    import base64
    import hashlib

    with open(image_path, 'rb') as f:
        image_data = f.read()

    if len(image_data) > 2 * 1024 * 1024:
        log(f'[WeChat] 图片过大 ({len(image_data)} bytes)，企微限制 2MB')
        return False

    b64 = base64.b64encode(image_data).decode('utf-8')
    md5 = hashlib.md5(image_data).hexdigest()

    body = json.dumps({
        'msgtype': 'image',
        'image': {'base64': b64, 'md5': md5}
    }).encode('utf-8')

    req = urllib.request.Request(WEBHOOK_URL, data=body, method='POST',
                                  headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            if result.get('errcode') == 0:
                log('[WeChat] 图片推送成功')
                return True
            else:
                log(f'[WeChat] 图片推送失败: {result}')
                return False
    except Exception as e:
        log(f'[WeChat] 图片请求错误: {e}')
        return False


# ============================================================
# 解析数据
# ============================================================
def parse_row(row):
    m = {}
    for dp in (row.get('lastdp') or []):
        m[dp['name']] = dp
    return {
        'timestamp': m.get('d_1', {}).get('at') or row.get('createTime', ''),
        'level1': float(m.get('d_1', {}).get('value', 0) or 0),
        'level2': float(m.get('d_2', {}).get('value', 0) or 0),
        'level3': float(m.get('d_3', {}).get('value', 0) or 0),
        'level4': float(m.get('d_4', {}).get('value', 0) or 0),
        'level5': float(m.get('d_5', {}).get('value', 0) or 0),
        'temperature': float(m.get('d_6', {}).get('value', 0) or 0),
        'waterContent': float(m.get('d_7', {}).get('value', 0) or 0),
        'waterActivity': float(m.get('d_8', {}).get('value', 0) or 0)
    }


# ============================================================
# 阈值报警
# ============================================================
def check_alarms(record):
    items = []
    max_level = 0
    channels = [
        ('level1', 'L1(>4um)'), ('level2', 'L2(>6um)'),
        ('level3', 'L3(>14um)'), ('level4', 'L4(>21um)'), ('level5', 'L5(>38um)')
    ]
    for key, name in channels:
        v = record.get(key, 0)
        if v >= THRESHOLDS['particle']['critical']:
            items.append({'name': '颗粒度超标', 'channel': name, 'value': v, 'unit': '个/mL'})
            max_level = max(max_level, 2)
        elif v >= THRESHOLDS['particle']['warning']:
            items.append({'name': '颗粒度超标', 'channel': name, 'value': v, 'unit': '个/mL'})
            max_level = max(max_level, 1)

    temp = record.get('temperature', 0)
    if temp >= THRESHOLDS['temperature']['critical']:
        items.append({'name': '温度过高', 'value': temp, 'unit': '℃'})
        max_level = max(max_level, 2)
    elif temp >= THRESHOLDS['temperature']['warning']:
        items.append({'name': '温度过高', 'value': temp, 'unit': '℃'})
        max_level = max(max_level, 1)

    wc = record.get('waterContent', 0)
    if wc >= THRESHOLDS['waterContent']['critical']:
        items.append({'name': '水含量超标', 'value': wc, 'unit': 'ppm'})
        max_level = max(max_level, 2)
    elif wc >= THRESHOLDS['waterContent']['warning']:
        items.append({'name': '水含量超标', 'value': wc, 'unit': 'ppm'})
        max_level = max(max_level, 1)

    wa = record.get('waterActivity', 0)
    if wa >= THRESHOLDS['waterActivity']['critical']:
        items.append({'name': '水活性过高', 'value': wa, 'unit': 'aw'})
        max_level = max(max_level, 2)
    elif wa >= THRESHOLDS['waterActivity']['warning']:
        items.append({'name': '水活性过高', 'value': wa, 'unit': 'aw'})
        max_level = max(max_level, 1)

    if items:
        return {'level': max_level, 'items': items, 'timestamp': record['timestamp']}
    return None


def build_alarm_message(alarm):
    level_text = '<font color="warning">严重报警</font>' if alarm['level'] == 2 \
        else '<font color="comment">警告提醒</font>'
    lines = [
        '### 油液监测报警通知',
        f'> **级别：** {level_text}',
        f'> **时间：** {alarm["timestamp"]}',
        f'> **设备：** 油液清洁度检测仪 (ID: {API_DEVICE})',
        '---'
    ]
    for item in alarm['items']:
        ch = f' ({item["channel"]})' if item.get('channel') else ''
        lines.append(f'> **{item["name"]}**{ch}：{item["value"]} {item["unit"]}')
    lines.extend(['---', '> 请及时处理，登录系统查看详情'])
    return '\n'.join(lines)


# ============================================================
# 趋势检测
# ============================================================
def check_weekly_trend(records):
    if len(records) < TREND_MIN_POINTS:
        log(f'[Trend] 仅 {len(records)} 条记录，不足{TREND_MIN_POINTS}条，跳过')
        return None
    daily = {}
    for r in records:
        day = r.get('timestamp', '')[:10]
        daily[day] = r
    sorted_days = sorted(daily.keys())[-TREND_DAYS:]
    if len(sorted_days) < TREND_MIN_POINTS:
        log(f'[Trend] 最近{TREND_DAYS}天仅 {len(sorted_days)} 天有数据，跳过')
        return None

    metric_names = {
        'level1': ('颗粒度 L1(>4um)', '个/mL'),
        'level2': ('颗粒度 L2(>6um)', '个/mL'),
        'level3': ('颗粒度 L3(>14um)', '个/mL'),
        'level4': ('颗粒度 L4(>21um)', '个/mL'),
        'level5': ('颗粒度 L5(>38um)', '个/mL'),
        'temperature': ('温度', '℃'),
        'waterContent': ('水含量', 'ppm'),
        'waterActivity': ('水活性', 'aw'),
    }
    trend_items = []
    for metric, (display_name, unit) in metric_names.items():
        values = [(day, daily[day].get(metric, 0)) for day in sorted_days]
        non_zero = [v for _, v in values if v > 0]
        if len(non_zero) < 3:
            continue
        rising = all(values[i][1] > values[i-1][1] for i in range(1, len(values)))
        if rising:
            fmt = '{:.1f}' if unit != 'aw' else '{:.3f}'
            trend_items.append({
                'name': display_name, 'days': len(values),
                'first_val': fmt.format(values[0][1]),
                'last_val': fmt.format(values[-1][1]), 'unit': unit
            })
    if trend_items:
        return {'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'items': trend_items}
    return None


def build_trend_message(trend_info):
    lines = [
        '### <font color="warning">指标趋势异常预警</font>',
        f'> **时间：** {trend_info["timestamp"]}',
        f'> **设备：** 油液清洁度检测仪 (ID: {API_DEVICE})',
        '---'
    ]
    for item in trend_info['items']:
        lines.append(f'> **{item["name"]}** 连续{item["days"]}天上升：'
                     f'{item["first_val"]} → {item["last_val"]} {item["unit"]}')
    lines.extend(['---', '> 指标持续上升可能预示油液劣化加速，建议提前安排换油或检修'])
    return '\n'.join(lines)


# ============================================================
# 趋势图生成（matplotlib）
# ============================================================
def generate_trend_chart(records, days, title_prefix=''):
    """生成趋势图 PNG，返回文件路径或 None"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        # 配置中文字体
        plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'sans-serif']
        plt.rcParams['axes.unicode_minus'] = False
    except ImportError:
        log('[Chart] matplotlib 不可用，跳过图表生成')
        return None

    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    recent = [r for r in records if r.get('timestamp', '')[:10] >= cutoff]
    if len(recent) < 2:
        log(f'[Chart] 数据不足（{len(recent)}条），无法生成图表')
        return None

    # 解析时间轴
    dates = []
    for r in recent:
        try:
            ts = r.get('timestamp', '')
            if ' ' in ts:
                dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S')
            else:
                dt = datetime.strptime(ts[:10], '%Y-%m-%d')
            dates.append(dt)
        except Exception:
            dates.append(None)

    # 过滤无效数据
    valid = [(d, r) for d, r in zip(dates, recent) if d is not None]
    if len(valid) < 2:
        return None
    dates, recent = zip(*valid)

    # 4 子图：颗粒度、温度、水含量、水活性
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.suptitle(f'{title_prefix}油液监测趋势图', fontsize=14, fontweight='bold')
    fig.autofmt_xdate()

    chart_groups = [
        (axes[0, 0], '颗粒度 (个/mL)', [
            ('level1', 'L1(>4um)'), ('level2', 'L2(>6um)'),
            ('level3', 'L3(>14um)'), ('level4', 'L4(>21um)'), ('level5', 'L5(>38um)')
        ], THRESHOLDS['particle']['warning']),
        (axes[0, 1], '温度 (℃)', [('temperature', '温度')], THRESHOLDS['temperature']['warning']),
        (axes[1, 0], '水含量 (ppm)', [('waterContent', '水含量')], THRESHOLDS['waterContent']['warning']),
        (axes[1, 1], '水活性 (aw)', [('waterActivity', '水活性')], THRESHOLDS['waterActivity']['warning']),
    ]

    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']

    for ax, chart_title, metrics_list, warn_line in chart_groups:
        for idx, (key, label) in enumerate(metrics_list):
            vals = [r.get(key, 0) for r in recent]
            if any(v > 0 for v in vals):
                ax.plot(dates, vals, marker='o', markersize=3, label=label,
                        color=colors[idx % len(colors)], linewidth=1.5)

        # 预警线
        if warn_line:
            ax.axhline(y=warn_line, color='red', linestyle='--', alpha=0.5, label=f'预警线 {warn_line}')

        ax.set_title(chart_title, fontsize=11)
        ax.legend(fontsize=8, loc='upper left')
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    plt.savefig(CHART_FILE, dpi=150, bbox_inches='tight')
    plt.close(fig)
    log(f'[Chart] 趋势图已保存: {CHART_FILE}')
    return CHART_FILE


# ============================================================
# 周报 / 月报
# ============================================================
def generate_report(records, report_days, report_type='周报'):
    """生成报告 Markdown"""
    now = datetime.now()
    start = now - timedelta(days=report_days)
    start_str = start.strftime('%Y-%m-%d')
    period_str = f'{start_str} ~ {now.strftime("%Y-%m-%d")}'

    recent = [r for r in records if r.get('timestamp', '')[:10] >= start_str]
    if not recent:
        return None

    metrics = [
        ('level1', '颗粒度 L1(>4um)', '个/mL'),
        ('level2', '颗粒度 L2(>6um)', '个/mL'),
        ('level3', '颗粒度 L3(>14um)', '个/mL'),
        ('level4', '颗粒度 L4(>21um)', '个/mL'),
        ('level5', '颗粒度 L5(>38um)', '个/mL'),
        ('temperature', '温度', '℃'),
        ('waterContent', '水含量', 'ppm'),
        ('waterActivity', '水活性', 'aw'),
    ]

    type_label = '周报' if report_type == '周报' else '月报'
    lines = [
        f'### 油液监测{type_label}',
        f'> **周期：** {period_str}',
        f'> **设备：** 油液清洁度检测仪 (ID: {API_DEVICE})',
        f'> **检测次数：** {len(recent)} 次',
        '---',
        '**指标概览：**'
    ]

    for key, name, unit in metrics:
        vals = [r.get(key, 0) for r in recent if r.get(key, 0) > 0]
        if not vals:
            continue
        fmt = '{:.1f}' if unit != 'aw' else '{:.3f}'
        cur = fmt.format(vals[-1])
        avg = fmt.format(sum(vals) / len(vals))
        mx = fmt.format(max(vals))
        lines.append(f'> {name}：当前 {cur}，均值 {avg}，最高 {mx} {unit}')

    alarm_count = sum(1 for r in recent if check_alarms(r))
    if alarm_count > 0:
        lines.append(f'> <font color="warning">本期触发阈值报警 {alarm_count} 次</font>')
    else:
        lines.append(f'> <font color="info">本期无阈值报警</font>')

    trend = check_weekly_trend(records)
    if trend:
        lines.append('---')
        lines.append('<font color="warning">**趋势预警：**</font>')
        for item in trend['items']:
            lines.append(f'> {item["name"]} 连续{item["days"]}天上升：'
                         f'{item["first_val"]} → {item["last_val"]} {item["unit"]}')

    lines.append('---')
    latest = recent[-1]
    max_particle = max(latest.get(f'level{i}', 0) for i in range(1, 6))
    if max_particle >= THRESHOLDS['particle']['critical']:
        assessment = '<font color="warning">油液污染严重，建议立即换油</font>'
    elif max_particle >= THRESHOLDS['particle']['warning']:
        assessment = '<font color="warning">油液轻度污染，建议加强监测并准备换油</font>'
    elif trend:
        assessment = '油液指标呈上升趋势，需持续关注'
    else:
        assessment = '<font color="info">油液状态良好</font>'

    lines.append(f'**综合评估：** {assessment}')
    lines.append('> 数据来源：MDS-Z4 传感器自动采集')
    return '\n'.join(lines)


def send_report(report_type='周报'):
    """生成并发送报告（含趋势图）"""
    records = load_data()
    report_days = 7 if report_type == '周报' else 30
    title_prefix = '周' if report_type == '周报' else '月'

    report = generate_report(records, report_days, report_type)
    if not report:
        log(f'[Report] 无足够数据生成{report_type}')
        return False

    # 1. 发送文字报告
    send_wechat(report)
    log(f'[Report] {report_type}文字已发送')

    # 2. 生成趋势图
    chart_path = generate_trend_chart(records, report_days, title_prefix)
    if chart_path and os.path.exists(chart_path):
        # 3. 发送图片
        send_wechat_image_file(chart_path)
        log(f'[Report] {report_type}趋势图已发送')
    return True


# ============================================================
# 主轮询
# ============================================================
def poll_and_notify():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log(f'\n[{now}] 开始轮询...')

    token = ensure_token()
    if not token:
        log('[Poll] MDS API 不可用，跳过')
        return

    result = http_request('GET',
        f'{API_BASE}/api/v1/device/lastdps?deviceId={API_DEVICE}')
    if not result or result.get('code') != 0 or not result.get('data', {}).get('rows'):
        log('[Poll] 无数据返回')
        return

    record = parse_row(result['data']['rows'][0])
    records = append_record(record)

    # 1. 阈值报警
    alarm = check_alarms(record)
    if alarm:
        alarm_id = f"{alarm['timestamp']}-{alarm['level']}"
        if alarm_id not in notified_alarms:
            notified_alarms.add(alarm_id)
            send_wechat(build_alarm_message(alarm))
            log(f'[Poll] 阈值报警已推送 (级别:{alarm["level"]})')
    else:
        log('[Poll] 无阈值报警')

    # 2. 趋势报警
    trend = check_weekly_trend(records)
    if trend:
        today = datetime.now().strftime('%Y-%m-%d')
        if today not in notified_trends:
            notified_trends.add(today)
            send_wechat(build_trend_message(trend))
            names = '、'.join(item['name'] for item in trend['items'])
            log(f'[Trend] 趋势报警已推送: {names}')
        else:
            log('[Trend] 今日趋势报警已推送过')
    else:
        log('[Trend] 无异常趋势')

    # 3. 周一自动发送周报
    if datetime.now().weekday() == 0:
        log('[Auto] 周一自动发送周报')
        send_report('周报')

    # 4. 每月1日自动发送月报
    if datetime.now().day == 1:
        log('[Auto] 月初自动发送月报')
        send_report('月报')


# ============================================================
# 调度
# ============================================================
def next_run_time():
    now = datetime.now()
    for h in SCHEDULE_HOURS:
        t = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if t > now:
            return t
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=min(SCHEDULE_HOURS), minute=0, second=0, microsecond=0)


# ============================================================
# 主程序
# ============================================================
if __name__ == '__main__':
    try:
        # 初始化 SQLite 数据库（含旧 JSON 数据迁移）
        init_db()

        # --report: 立即发送周报
        if '--report' in sys.argv:
            log('[启动] 立即发送周报')
            ensure_token()
            # 先拉一次最新数据
            result = http_request('GET',
                f'{API_BASE}/api/v1/device/lastdps?deviceId={API_DEVICE}')
            if result and result.get('code') == 0 and result.get('data', {}).get('rows'):
                append_record(parse_row(result['data']['rows'][0]))
            send_report('周报')
            log('[完成] 周报发送完毕')
            sys.exit(0)

        # --monthly: 立即发送月报
        if '--monthly' in sys.argv:
            log('[启动] 立即发送月报')
            ensure_token()
            result = http_request('GET',
                f'{API_BASE}/api/v1/device/lastdps?deviceId={API_DEVICE}')
            if result and result.get('code') == 0 and result.get('data', {}).get('rows'):
                append_record(parse_row(result['data']['rows'][0]))
            send_report('月报')
            log('[完成] 月报发送完毕')
            sys.exit(0)

        # 正常定时服务
        log('=' * 40)
        log('  油液监测报警推送服务')
        log(f'  执行时间: 每天 9:00')
        log(f'  自动周报: 每周一')
        log(f'  自动月报: 每月1日')
        log(f'  趋势检测: 连续{TREND_DAYS}天上升报警')
        log(f'  设备ID: {API_DEVICE}')
        log('=' * 40)

        log('[启动] 15秒后执行首次检测...')
        time.sleep(15)
        poll_and_notify()

        while True:
            target = next_run_time()
            wait = (target - datetime.now()).total_seconds()
            log(f'[调度] 下次执行: {target.strftime("%Y-%m-%d %H:%M")} (等待{wait/3600:.1f}小时)')
            time.sleep(wait)
            poll_and_notify()

    except KeyboardInterrupt:
        log('\n[退出] 用户中断，服务停止')
    except Exception as e:
        log(f'\n[致命错误] {e}')
        traceback.print_exc()
        log('[退出] 脚本异常终止')
