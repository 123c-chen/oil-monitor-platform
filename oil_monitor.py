"""
冲压车间压机润滑油智能监测系统 v2.0
功能：数据采集 + 智能报警 + 趋势图表 + 企业微信推送
"""
import requests
import sqlite3
import json
import sys
import os
import time
import math
import hashlib
import hmac
import base64
from datetime import datetime, timedelta
from pathlib import Path

import urllib3
urllib3.disable_warnings()

# ============================================================
# 配置区
# ============================================================
CONFIG = {
    # 云平台API
    "api_base": "https://mds.bodazl.com:8090/api/v1",
    "username": "mds26061103",
    "password": "123456",
    "device_id": 56857,

    # 本地数据库
    "db_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "oil_monitor.db"),

    # 企业微信群机器人Webhook（需要替换为实际地址）
    "wecom_webhook": "",  # 填入你的企业微信群机器人Webhook URL

    # 报警阈值（基于ISO 4406 / NAS 1638 / 长城100号润滑油特性）
    "thresholds": {
        # 颗粒度等级报警（每个通道对应不同粒径）
        # 等级1(>4um), 等级2(>6um), 等级3(>14um), 等级4(>21um), 等级5(>38um)
        "particle": {
            "warning": 18,   # 预警：颗粒数超过此值
            "critical": 25,  # 严重：颗粒数超过此值，立即换油/检查
        },
        # 温度报警（℃）
        "temperature": {
            "low": 10,       # 油温过低（冷启动需预热）
            "high_warn": 55, # 温度偏高
            "high_critical": 65,  # 温度过高，严重
        },
        # 水含量报警（ppm）
        "water_content": {
            "warning": 150,
            "critical": 300,
        },
        # 水活性报警（aw）
        "water_activity": {
            "warning": 0.7,
            "critical": 0.85,
        },
        # 动态报警：连续上升趋势次数
        "trend_rise_count": 3,
        # 动态报警：单次跳变幅度（相对上次值）
        "spike_ratio": 1.5,
    },

    # 图表输出目录
    "chart_dir": os.path.join(os.path.dirname(os.path.abspath(__file__)), "charts"),
}

# 数据点名称映射
DP_MAP = {
    "d_1": ("等级1", ">4um颗粒数"),
    "d_2": ("等级2", ">6um颗粒数"),
    "d_3": ("等级3", ">14um颗粒数"),
    "d_4": ("等级4", ">21um颗粒数"),
    "d_5": ("等级5", ">38um颗粒数"),
    "d_6": ("温度", "油温"),
    "d_7": ("水含量", "水分"),
    "d_8": ("水活性", "水活性"),
}


# ============================================================
# 数据库
# ============================================================
def init_db():
    conn = sqlite3.connect(CONFIG["db_path"])
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sensor_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL UNIQUE,
        level1 INTEGER, level2 INTEGER, level3 INTEGER,
        level4 INTEGER, level5 INTEGER,
        temperature REAL, water_content REAL, water_activity REAL,
        alarm_level INTEGER DEFAULT 0,
        alarm_msg TEXT DEFAULT '',
        raw_json TEXT
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_ts ON sensor_data(timestamp)')
    conn.commit()
    return conn


def save_record(conn, record, alarm_level=0, alarm_msg=""):
    c = conn.cursor()
    try:
        c.execute('''INSERT OR IGNORE INTO sensor_data
            (timestamp, level1, level2, level3, level4, level5,
             temperature, water_content, water_activity,
             alarm_level, alarm_msg, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (record["timestamp"],
             record.get("level1"), record.get("level2"), record.get("level3"),
             record.get("level4"), record.get("level5"),
             record.get("temperature"), record.get("water_content"),
             record.get("water_activity"),
             alarm_level, alarm_msg,
             json.dumps(record, ensure_ascii=False)))
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        print(f"[DB] 保存失败: {e}")
        return False


def get_recent_data(conn, hours=24):
    """获取最近N小时的数据"""
    c = conn.cursor()
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''SELECT timestamp, level1, level2, level3, level4, level5,
                        temperature, water_content, water_activity, alarm_level
                 FROM sensor_data WHERE timestamp >= ? ORDER BY timestamp ASC''', (since,))
    return c.fetchall()


def get_data_range(conn, start_date, end_date):
    """获取指定日期范围的数据"""
    c = conn.cursor()
    c.execute('''SELECT timestamp, level1, level2, level3, level4, level5,
                        temperature, water_content, water_activity, alarm_level
                 FROM sensor_data WHERE timestamp >= ? AND timestamp <= ?
                 ORDER BY timestamp ASC''', (start_date, end_date))
    return c.fetchall()


def get_statistics(conn, days=7):
    """计算最近N天的统计信息"""
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''SELECT
        COUNT(*) as cnt,
        AVG(level1), AVG(level2), AVG(level3), AVG(level4), AVG(level5),
        MAX(level1), MAX(level2), MAX(level3), MAX(level4), MAX(level5),
        AVG(temperature), MAX(temperature), MIN(temperature),
        AVG(water_content), MAX(water_content),
        AVG(water_activity), MAX(water_activity)
    FROM sensor_data WHERE timestamp >= ? AND level1 >= 0''', (since,))
    row = c.fetchone()
    if row and row[0] > 0:
        return {
            "count": row[0],
            "avg_l1": row[1], "avg_l2": row[2], "avg_l3": row[3],
            "avg_l4": row[4], "avg_l5": row[5],
            "max_l1": row[6], "max_l2": row[7], "max_l3": row[8],
            "max_l4": row[9], "max_l5": row[10],
            "avg_temp": row[11], "max_temp": row[12], "min_temp": row[13],
            "avg_water": row[14], "max_water": row[15],
            "avg_activity": row[16], "max_activity": row[17],
        }
    return None


# ============================================================
# 云平台数据采集
# ============================================================
class CloudClient:
    def __init__(self):
        self.session = requests.Session()
        self.token = None

    def login(self):
        try:
            r = self.session.post(
                f"{CONFIG['api_base']}/login/login",
                json={"username": CONFIG["username"], "password": CONFIG["password"]},
                verify=False, timeout=10)
            data = r.json()
            if data.get("code") == 0:
                self.token = data["data"]["token"]
                self.session.headers.update({"X-Token": self.token})
                return True
        except Exception as e:
            print(f"[API] 登录异常: {e}")
        return False

    def fetch_latest(self):
        try:
            r = self.session.get(
                f"{CONFIG['api_base']}/device/lastdps",
                params={"deviceId": CONFIG["device_id"]},
                verify=False, timeout=10)
            data = r.json()
            if data.get("code") != 0:
                return None
            rows = data["data"]["rows"]
            if not rows:
                return None
            dp_map = {}
            for dp in rows[0].get("lastdp", []):
                dp_map[dp["name"]] = {
                    "title": dp["title"],
                    "value": dp["value"],
                    "unit": dp.get("unit", ""),
                    "at": dp.get("at", ""),
                }
            return {
                "timestamp": dp_map.get("d_1", {}).get("at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "level1": dp_map.get("d_1", {}).get("value"),
                "level2": dp_map.get("d_2", {}).get("value"),
                "level3": dp_map.get("d_3", {}).get("value"),
                "level4": dp_map.get("d_4", {}).get("value"),
                "level5": dp_map.get("d_5", {}).get("value"),
                "temperature": dp_map.get("d_6", {}).get("value"),
                "water_content": dp_map.get("d_7", {}).get("value"),
                "water_activity": dp_map.get("d_8", {}).get("value"),
            }
        except Exception as e:
            print(f"[API] 获取数据异常: {e}")
            return None

    def fetch_history(self, page_size=100, max_pages=10):
        """
        获取历史数据点（分页）
        返回: list of record dicts, 每个包含 timestamp, level1-5, temperature, water_content, water_activity
        """
        all_rows = []
        page = 1
        for _ in range(max_pages):
            try:
                r = self.session.get(
                    f"{CONFIG['api_base']}/device/dps",
                    params={"deviceId": CONFIG["device_id"], "pageSize": page_size, "page": page},
                    verify=False, timeout=15)
                data = r.json()
                if data.get("code") != 0 or not data.get("data", {}).get("rows"):
                    break
                rows = data["data"]["rows"]
                all_rows.extend(rows)
                # 如果返回数量小于pageSize，说明没有更多数据了
                if len(rows) < page_size:
                    break
                page += 1
                time.sleep(0.3)  # 避免请求过快
            except Exception as e:
                print(f"[API] 获取历史数据异常(第{page}页): {e}")
                break

        records = []
        for row in all_rows:
            dp_map = {}
            for dp in row.get("lastdp", []):
                dp_map[dp["name"]] = {
                    "value": dp.get("value"),
                    "at": dp.get("at", ""),
                }
            ts = dp_map.get("d_1", {}).get("at") or row.get("createTime", "")
            if not ts:
                continue
            # 统一时间格式
            try:
                if "T" in ts:
                    ts = ts.replace("T", " ").split(".")[0]
                else:
                    ts = ts.split(".")[0]
            except:
                pass
            records.append({
                "timestamp": ts,
                "level1": dp_map.get("d_1", {}).get("value"),
                "level2": dp_map.get("d_2", {}).get("value"),
                "level3": dp_map.get("d_3", {}).get("value"),
                "level4": dp_map.get("d_4", {}).get("value"),
                "level5": dp_map.get("d_5", {}).get("value"),
                "temperature": dp_map.get("d_6", {}).get("value"),
                "water_content": dp_map.get("d_7", {}).get("value"),
                "water_activity": dp_map.get("d_8", {}).get("value"),
            })
        return records


# ============================================================
# 智能报警引擎
# ============================================================
class AlarmEngine:
    """
    三级报警：
      0 = 正常（绿色）
      1 = 预警（黄色）— 接近阈值或出现上升趋势
      2 = 严重报警（红色）— 超过关键阈值
    """

    def __init__(self, conn):
        self.conn = conn
        self.thresholds = CONFIG["thresholds"]

    def check(self, record):
        """对一条数据执行全部报警检查，返回 (alarm_level, [messages])"""
        level = 0
        messages = []

        # --- 1. 无效数据过滤 ---
        if record.get("level1") == -1 or record.get("level1") is None:
            return 0, []  # 传感器未就绪，不报警

        # --- 2. 静态阈值检查 ---
        # 颗粒度
        for key in ["level1", "level2", "level3", "level4", "level5"]:
            val = record.get(key)
            if val is None or val < 0:
                continue
            if val >= self.thresholds["particle"]["critical"]:
                level = max(level, 2)
                title = DP_MAP.get(key.replace("level", "d_"), (key,))[0]
                messages.append(f"[严重] {title}={val}，超过临界值{self.thresholds['particle']['critical']}")
            elif val >= self.thresholds["particle"]["warning"]:
                level = max(level, 1)
                title = DP_MAP.get(key.replace("level", "d_"), (key,))[0]
                messages.append(f"[预警] {title}={val}，超过预警值{self.thresholds['particle']['warning']}")

        # 温度
        temp = record.get("temperature")
        if temp is not None:
            if temp >= self.thresholds["temperature"]["high_critical"]:
                level = max(level, 2)
                messages.append(f"[严重] 油温={temp}℃，超过{self.thresholds['temperature']['high_critical']}℃")
            elif temp >= self.thresholds["temperature"]["high_warn"]:
                level = max(level, 1)
                messages.append(f"[预警] 油温={temp}℃，超过{self.thresholds['temperature']['high_warn']}℃")
            elif temp <= self.thresholds["temperature"]["low"]:
                level = max(level, 1)
                messages.append(f"[预警] 油温={temp}℃，低于{self.thresholds['temperature']['low']}℃，建议预热")

        # 水含量
        wc = record.get("water_content")
        if wc is not None:
            if wc >= self.thresholds["water_content"]["critical"]:
                level = max(level, 2)
                messages.append(f"[严重] 水含量={wc}ppm，超过{self.thresholds['water_content']['critical']}ppm")
            elif wc >= self.thresholds["water_content"]["warning"]:
                level = max(level, 1)
                messages.append(f"[预警] 水含量={wc}ppm，超过{self.thresholds['water_content']['warning']}ppm")

        # 水活性
        wa = record.get("water_activity")
        if wa is not None:
            if wa >= self.thresholds["water_activity"]["critical"]:
                level = max(level, 2)
                messages.append(f"[严重] 水活性={wa}aw，超过{self.thresholds['water_activity']['critical']}aw")
            elif wa >= self.thresholds["water_activity"]["warning"]:
                level = max(level, 1)
                messages.append(f"[预警] 水活性={wa}aw，超过{self.thresholds['water_activity']['warning']}aw")

        # --- 3. 动态趋势检查（需要历史数据） ---
        recent = get_recent_data(self.conn, hours=24*30)  # 近30天数据（每天1条）
        if len(recent) >= self.thresholds["trend_rise_count"]:
            # 检查颗粒数连续上升趋势
            for col_idx, col_name in [(1, "等级1"), (2, "等级2"), (3, "等级3"), (4, "等级4"), (5, "等级5")]:
                recent_vals = [r[col_idx] for r in recent[-self.thresholds["trend_rise_count"]:] if r[col_idx] is not None and r[col_idx] >= 0]
                if len(recent_vals) >= self.thresholds["trend_rise_count"]:
                    rising = all(recent_vals[i] < recent_vals[i+1] for i in range(len(recent_vals)-1))
                    if rising:
                        level = max(level, 1)
                        messages.append(f"[趋势] {col_name}连续{len(recent_vals)}次上升: {'→'.join(str(v) for v in recent_vals)}")

            # 检查温度连续上升
            temps = [r[6] for r in recent[-5:] if r[6] is not None]
            if len(temps) >= 3:
                rising_temp = all(temps[i] < temps[i+1] for i in range(len(temps)-1))
                if rising_temp:
                    level = max(level, 1)
                    messages.append(f"[趋势] 温度连续上升: {'→'.join(f'{v}℃' for v in temps[-3:])}")

        # --- 4. 突变检测 ---
        if len(recent) >= 2:
            last = recent[-1]
            for col_idx, col_name in [(1, "等级1"), (6, "温度"), (7, "水含量")]:
                prev_val = last[col_idx]
                curr_val = record.get({1: "level1", 6: "temperature", 7: "water_content"}[col_idx])
                if prev_val and curr_val and prev_val > 0 and curr_val > 0:
                    ratio = curr_val / prev_val
                    if ratio >= self.thresholds["spike_ratio"]:
                        level = max(level, 1)
                        messages.append(f"[突变] {col_name}从{prev_val}跳变到{curr_val}（幅度{ratio:.1f}倍）")

        return level, messages


# ============================================================
# 趋势图表生成
# ============================================================
def generate_trend_chart(conn, days=7, output_path=None):
    """生成趋势曲线图"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    # 设置中文字体
    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    end_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    data = get_data_range(conn, start_date, end_date)

    if not data or len(data) < 2:
        print("[图表] 数据不足，无法生成趋势图")
        return None

    os.makedirs(CONFIG["chart_dir"], exist_ok=True)
    if output_path is None:
        output_path = os.path.join(CONFIG["chart_dir"], f"trend_{days}d.png")

    timestamps = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in data]
    levels = {i: [r[i] for r in data] for i in range(1, 6)}
    temps = [r[6] for r in data]
    waters = [r[7] for r in data]
    activities = [r[8] for r in data]
    alarms = [r[9] for r in data]

    # 过滤无效值
    def filter_valid(vals):
        return [v if v is not None and v >= 0 else None for v in vals]

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(f'压机润滑油监测趋势（最近{days}天）', fontsize=16, fontweight='bold')

    # --- 子图1：颗粒度等级 ---
    ax1 = axes[0]
    colors = ['#e74c3c', '#e67e22', '#f1c40f', '#2ecc71', '#3498db']
    labels = ['等级1(>4um)', '等级2(>6um)', '等级3(>14um)', '等级4(>21um)', '等级5(>38um)']
    for i in range(1, 6):
        vals = filter_valid(levels[i])
        ax1.plot(timestamps, vals, color=colors[i-1], label=labels[i-1], linewidth=1.5, alpha=0.8)
    ax1.axhline(y=CONFIG["thresholds"]["particle"]["warning"], color='orange',
                linestyle='--', alpha=0.7, label=f'预警线({CONFIG["thresholds"]["particle"]["warning"]})')
    ax1.axhline(y=CONFIG["thresholds"]["particle"]["critical"], color='red',
                linestyle='--', alpha=0.7, label=f'严重线({CONFIG["thresholds"]["particle"]["critical"]})')
    ax1.set_ylabel('颗粒数')
    ax1.legend(loc='upper right', fontsize=8)
    ax1.grid(True, alpha=0.3)
    ax1.set_title('颗粒度等级趋势')

    # 标记报警点
    for idx, alm in enumerate(alarms):
        if alm >= 2:
            ax1.axvline(x=timestamps[idx], color='red', alpha=0.15, linewidth=2)
        elif alm >= 1:
            ax1.axvline(x=timestamps[idx], color='orange', alpha=0.1, linewidth=1)

    # --- 子图2：温度 ---
    ax2 = axes[1]
    temp_vals = filter_valid(temps)
    ax2.plot(timestamps, temp_vals, color='#e74c3c', linewidth=1.5, label='油温(℃)')
    ax2.axhline(y=CONFIG["thresholds"]["temperature"]["high_warn"], color='orange',
                linestyle='--', alpha=0.7, label=f'预警线({CONFIG["thresholds"]["temperature"]["high_warn"]}℃)')
    ax2.axhline(y=CONFIG["thresholds"]["temperature"]["high_critical"], color='red',
                linestyle='--', alpha=0.7, label=f'严重线({CONFIG["thresholds"]["temperature"]["high_critical"]}℃)')
    ax2.set_ylabel('温度 (℃)')
    ax2.legend(loc='upper right', fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.set_title('油温趋势')

    # --- 子图3：水分 ---
    ax3 = axes[2]
    water_vals = filter_valid(waters)
    activity_vals = filter_valid(activities)
    ax3.plot(timestamps, water_vals, color='#3498db', linewidth=1.5, label='水含量(ppm)')
    ax3_twin = ax3.twinx()
    ax3_twin.plot(timestamps, activity_vals, color='#9b59b6', linewidth=1.5, linestyle='--', label='水活性(aw)')
    ax3.axhline(y=CONFIG["thresholds"]["water_content"]["warning"], color='orange',
                linestyle='--', alpha=0.5, label=f'水含量预警({CONFIG["thresholds"]["water_content"]["warning"]}ppm)')
    ax3.set_ylabel('水含量 (ppm)')
    ax3_twin.set_ylabel('水活性 (aw)')
    ax3.legend(loc='upper left', fontsize=8)
    ax3_twin.legend(loc='upper right', fontsize=8)
    ax3.grid(True, alpha=0.3)
    ax3.set_title('水分趋势')

    # X轴时间格式
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    ax3.xaxis.set_major_locator(mdates.AutoDateLocator())
    plt.xticks(rotation=45)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"[图表] 已保存: {output_path}")
    return output_path


def generate_summary_chart(conn, output_path=None):
    """生成日报/周报摘要图（单张紧凑图，适合企业微信推送）"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    stats = get_statistics(conn, days=7)
    if not stats:
        return None

    os.makedirs(CONFIG["chart_dir"], exist_ok=True)
    if output_path is None:
        output_path = os.path.join(CONFIG["chart_dir"], "summary.png")

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle('油液监测周报摘要', fontsize=14, fontweight='bold')

    # 左图：各等级平均颗粒数柱状图
    ax1 = axes[0]
    categories = ['等级1', '等级2', '等级3', '等级4', '等级5']
    avgs = [stats[f'avg_l{i}'] or 0 for i in range(1, 6)]
    maxs = [stats[f'max_l{i}'] or 0 for i in range(1, 6)]
    x = range(len(categories))
    width = 0.35
    bars1 = ax1.bar([i - width/2 for i in x], avgs, width, label='平均值', color='#3498db', alpha=0.8)
    bars2 = ax1.bar([i + width/2 for i in x], maxs, width, label='最大值', color='#e74c3c', alpha=0.8)
    ax1.axhline(y=CONFIG["thresholds"]["particle"]["warning"], color='orange', linestyle='--', alpha=0.7, label='预警线')
    ax1.set_xticks(list(x))
    ax1.set_xticklabels(categories)
    ax1.set_ylabel('颗粒数')
    ax1.legend(fontsize=8)
    ax1.set_title('颗粒度分布')
    ax1.grid(True, alpha=0.3, axis='y')

    # 右图：温度/水含量/水活性 平均+最大
    ax2 = axes[1]
    metrics = ['温度(℃)', '水含量(ppt)', '水活性(aw×100)']
    metric_avgs = [stats['avg_temp'] or 0, stats['avg_water'] or 0, (stats['avg_activity'] or 0) * 100]
    metric_maxs = [stats['max_temp'] or 0, stats['max_water'] or 0, (stats['max_activity'] or 0) * 100]
    x2 = range(len(metrics))
    ax2.bar([i - width/2 for i in x2], metric_avgs, width, label='平均值', color='#2ecc71', alpha=0.8)
    ax2.bar([i + width/2 for i in x2], metric_maxs, width, label='最大值', color='#e67e22', alpha=0.8)
    ax2.set_xticks(list(x2))
    ax2.set_xticklabels(metrics)
    ax2.legend(fontsize=8)
    ax2.set_title('温度/水分概况')
    ax2.grid(True, alpha=0.3, axis='y')

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"[图表] 摘要图已保存: {output_path}")
    return output_path


def generate_dashboard_image(conn, output_path=None):
    """生成每日数据看板图片（深色科技感仪表盘样式，竖版适合手机查看）"""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch
    import matplotlib.dates as mdates

    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    # 获取最新数据
    c = conn.cursor()
    c.execute('''SELECT timestamp, level1, level2, level3, level4, level5,
                        temperature, water_content, water_activity, alarm_level
                 FROM sensor_data ORDER BY timestamp DESC LIMIT 1''')
    latest = c.fetchone()

    # 获取近7天历史数据（每天1条，共约7个数据点）
    recent_history = get_recent_data(conn, hours=24*7)

    if not latest:
        print("[看板] 无数据，无法生成看板")
        return None

    os.makedirs(CONFIG["chart_dir"], exist_ok=True)
    if output_path is None:
        output_path = os.path.join(CONFIG["chart_dir"], "dashboard_daily.png")

    ts, l1, l2, l3, l4, l5, temp, wc, wa, alarm = latest
    alarm_level = alarm or 0

    # 状态颜色（去掉emoji，用文字+颜色）
    if alarm_level >= 2:
        status_color = '#e74c3c'
        status_text = '[严重报警]'
    elif alarm_level >= 1:
        status_color = '#f39c12'
        status_text = '[预警提醒]'
    else:
        status_color = '#2ecc71'
        status_text = '[运行正常]'

    # 创建深色背景画布（竖版，适合手机）
    fig = plt.figure(figsize=(6, 14), facecolor='#0d1117')

    # ========== 顶部标题栏 ==========
    ax_header = fig.add_axes([0.08, 0.93, 0.84, 0.055])
    ax_header.set_facecolor('#161b22')
    ax_header.set_xlim(0, 1)
    ax_header.set_ylim(0, 1)
    ax_header.axis('off')

    ax_header.text(0.02, 0.55, '冲压车间压机润滑油智能监测系统', fontsize=11, fontweight='bold',
                   color='#e6edf3', va='center', transform=ax_header.transAxes)
    ax_header.text(0.98, 0.55, status_text, fontsize=10, fontweight='bold',
                   color=status_color, va='center', ha='right', transform=ax_header.transAxes)

    # 时间信息
    ax_time = fig.add_axes([0.08, 0.895, 0.84, 0.035])
    ax_time.set_facecolor('#161b22')
    ax_time.set_xlim(0, 1)
    ax_time.set_ylim(0, 1)
    ax_time.axis('off')
    ax_time.text(0.02, 0.5, f'数据时间: {ts}', fontsize=7.5, color='#8b949e',
                 va='center', transform=ax_time.transAxes)
    ax_time.text(0.98, 0.5, f'生成: {datetime.now().strftime("%m-%d %H:%M")}',
                 fontsize=7.5, color='#8b949e', va='center', ha='right', transform=ax_time.transAxes)

    # ========== 数据卡片区域 ==========
    def draw_card(ax, x, y, w, h, title, value, unit, color, bg_color='#161b22'):
        """绘制一个数据卡片"""
        rect = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.01",
                              facecolor=bg_color, edgecolor=color, linewidth=1.2, alpha=0.9)
        ax.add_patch(rect)
        ax.text(x + w/2, y + h*0.68, title, fontsize=6.5, color='#8b949e',
                ha='center', va='center', transform=ax.transData)
        ax.text(x + w/2, y + h*0.38, f'{value}', fontsize=13, fontweight='bold',
                color=color, ha='center', va='center', transform=ax.transData)
        ax.text(x + w/2, y + h*0.12, unit, fontsize=5.5, color='#8b949e',
                ha='center', va='center', transform=ax.transData)

    # 颗粒度卡片行（5个）
    ax_cards1 = fig.add_axes([0.05, 0.78, 0.90, 0.10], facecolor='none')
    ax_cards1.set_xlim(0, 10)
    ax_cards1.set_ylim(0, 1)
    ax_cards1.axis('off')

    card_w, card_h = 1.75, 0.72
    card_gap = 0.12
    start_x = 0.45
    particle_data = [
        ('等级1(>4um)', l1, '个/mL', '#e74c3c'),
        ('等级2(>6um)', l2, '个/mL', '#e67e22'),
        ('等级3(>14um)', l3, '个/mL', '#f1c40f'),
        ('等级4(>21um)', l4, '个/mL', '#2ecc71'),
        ('等级5(>38um)', l5, '个/mL', '#3498db'),
    ]
    for i, (title, val, unit, color) in enumerate(particle_data):
        x = start_x + i * (card_w + card_gap)
        draw_card(ax_cards1, x, 0.1, card_w, card_h, title, val if val is not None else '--', unit, color)

    # 温度/水分卡片行（3个）
    ax_cards2 = fig.add_axes([0.05, 0.69, 0.90, 0.08], facecolor='none')
    ax_cards2.set_xlim(0, 10)
    ax_cards2.set_ylim(0, 1)
    ax_cards2.axis('off')

    other_data = [
        ('油温', temp, '℃', '#e74c3c' if temp and temp >= 55 else '#2ecc71'),
        ('水含量', wc, 'ppm', '#f39c12' if wc and wc >= 150 else '#2ecc71'),
        ('水活性', wa, 'aw', '#9b59b6' if wa and wa >= 0.7 else '#2ecc71'),
    ]
    for i, (title, val, unit, color) in enumerate(other_data):
        x = 1.8 + i * 2.8
        draw_card(ax_cards2, x, 0.1, 2.4, 0.6, title,
                  f'{val}' if val is not None else '--', unit, color)

    # ========== 近7天历史趋势图 ==========
    if recent_history and len(recent_history) >= 2:
        timestamps_hist = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in recent_history]

        # 颗粒度趋势
        ax_trend1 = fig.add_axes([0.10, 0.52, 0.82, 0.15], facecolor='#161b22')
        ax_trend1.set_facecolor('#161b22')
        colors_p = ['#e74c3c', '#e67e22', '#f1c40f', '#2ecc71', '#3498db']
        labels_p = ['L1(>4um)', 'L2(>6um)', 'L3(>14um)', 'L4(>21um)', 'L5(>38um)']
        for i in range(1, 6):
            vals = [r[i] if r[i] is not None and r[i] >= 0 else None for r in recent_history]
            ax_trend1.plot(timestamps_hist, vals, color=colors_p[i-1], label=labels_p[i-1],
                          linewidth=1.5, alpha=0.8, marker='o', markersize=3)
        ax_trend1.axhline(y=CONFIG["thresholds"]["particle"]["warning"], color='orange',
                         linestyle='--', alpha=0.5, linewidth=0.7)
        ax_trend1.axhline(y=CONFIG["thresholds"]["particle"]["critical"], color='red',
                         linestyle='--', alpha=0.5, linewidth=0.7)
        ax_trend1.set_ylabel('颗粒数', fontsize=7, color='#8b949e')
        ax_trend1.tick_params(colors='#8b949e', labelsize=6)
        ax_trend1.legend(loc='upper right', fontsize=5, facecolor='#161b22',
                        edgecolor='#30363d', labelcolor='#c9d1d9', ncol=3)
        ax_trend1.set_title('近7天 颗粒度趋势', fontsize=8, color='#e6edf3', pad=4)
        ax_trend1.grid(True, alpha=0.15, color='#30363d')
        for spine in ax_trend1.spines.values():
            spine.set_color('#30363d')
        ax_trend1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))

        # 温度/水分趋势
        ax_trend2 = fig.add_axes([0.10, 0.35, 0.82, 0.15], facecolor='#161b22')
        ax_trend2.set_facecolor('#161b22')
        temps_hist = [r[6] if r[6] is not None else None for r in recent_history]
        wc_hist = [r[7] if r[7] is not None else None for r in recent_history]
        wa_hist = [r[8] if r[8] is not None else None for r in recent_history]

        ax_trend2.plot(timestamps_hist, temps_hist, color='#e74c3c', linewidth=1.5,
                       label='油温(℃)', marker='o', markersize=3)
        ax_trend2.axhline(y=CONFIG["thresholds"]["temperature"]["high_warn"], color='orange',
                         linestyle='--', alpha=0.5, linewidth=0.7)
        ax_trend2.set_ylabel('温度(℃)', fontsize=7, color='#8b949e')
        ax_trend2.tick_params(colors='#8b949e', labelsize=6)
        ax_trend2.set_title('近7天 温度/水分趋势', fontsize=8, color='#e6edf3', pad=4)
        ax_trend2.grid(True, alpha=0.15, color='#30363d')
        for spine in ax_trend2.spines.values():
            spine.set_color('#30363d')
        ax_trend2.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
        ax_trend2.legend(loc='upper left', fontsize=5, facecolor='#161b22',
                        edgecolor='#30363d', labelcolor='#c9d1d9')

        ax_twin = ax_trend2.twinx()
        ax_twin.plot(timestamps_hist, wc_hist, color='#3498db', linewidth=1.5,
                     label='水含量(ppm)', alpha=0.7, marker='o', markersize=3)
        ax_twin.plot(timestamps_hist, wa_hist, color='#9b59b6', linewidth=1.5,
                     linestyle='--', label='水活性(aw)', alpha=0.7, marker='o', markersize=3)
        ax_twin.set_ylabel('水分', fontsize=7, color='#8b949e')
        ax_twin.tick_params(colors='#8b949e', labelsize=6)
        ax_twin.legend(loc='upper right', fontsize=5, facecolor='#161b22',
                      edgecolor='#30363d', labelcolor='#c9d1d9')
        for spine in ax_twin.spines.values():
            spine.set_color('#30363d')
    else:
        ax_note = fig.add_axes([0.10, 0.35, 0.82, 0.32], facecolor='#161b22')
        ax_note.set_xlim(0, 1)
        ax_note.set_ylim(0, 1)
        ax_note.axis('off')
        ax_note.text(0.5, 0.5, '历史数据不足，暂无法生成趋势图', fontsize=9,
                    color='#8b949e', ha='center', va='center', transform=ax_note.transAxes)

    # ========== 底部统计栏 ==========
    ax_footer = fig.add_axes([0.05, 0.02, 0.90, 0.30], facecolor='#161b22')
    ax_footer.set_xlim(0, 1)
    ax_footer.set_ylim(0, 1)
    ax_footer.axis('off')

    # 近7天统计
    if recent_history:
        valid_data = [r for r in recent_history if r[1] is not None and r[1] >= 0]
        # 统计实际天数（去重日期）
        unique_days = set()
        for r in recent_history:
            try:
                dt = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")
                unique_days.add(dt.strftime("%Y-%m-%d"))
            except:
                pass
        count_days = len(unique_days) if unique_days else len(valid_data)
        alarm_hist = sum(1 for r in recent_history if r[9] and r[9] > 0)
        avg_l1 = sum(r[1] for r in valid_data) / len(valid_data) if valid_data else 0

        valid_temps = [r[6] for r in valid_data if r[6] is not None]
        valid_wc = [r[7] for r in valid_data if r[7] is not None]

        stats_lines = [
            ('近7天统计', 0.88, '#e6edf3', 8, True),
            (f"覆盖天数: {count_days}天    报警次数: {alarm_hist}次", 0.72, '#8b949e', 7, False),
            (f"等级1均值: {avg_l1:.1f}    温度范围: {min(valid_temps):.1f}~{max(valid_temps):.1f}℃", 0.58, '#8b949e', 7, False),
            (f"水含量均值: {sum(valid_wc)/len(valid_wc):.1f}ppm" if valid_wc else "", 0.44, '#8b949e', 7, False),
        ]
    else:
        stats_lines = [
            ('近7天统计', 0.88, '#e6edf3', 8, True),
            ('暂无数据', 0.58, '#8b949e', 7, False),
        ]

    for text, y, color, size, bold in stats_lines:
        if text:
            ax_footer.text(0.02, y, text, fontsize=size, color=color,
                          fontweight='bold' if bold else 'normal',
                          va='center', transform=ax_footer.transAxes)

    # 阈值参考
    threshold_text = (
        f"预警阈值: 颗粒≥{CONFIG['thresholds']['particle']['warning']} | "
        f"温度≥{CONFIG['thresholds']['temperature']['high_warn']}℃ | "
        f"水含量≥{CONFIG['thresholds']['water_content']['warning']}ppm"
    )
    ax_footer.text(0.02, 0.15, threshold_text, fontsize=5.5, color='#484f58',
                   va='center', transform=ax_footer.transAxes)

    plt.savefig(output_path, dpi=120, facecolor='#0d1117', edgecolor='none', bbox_inches='tight')
    plt.close()
    print(f"[看板] 已保存: {output_path}")
    return output_path


# ============================================================
# 企业微信推送
# ============================================================
def wecom_send_text(webhook_url, content):
    """发送文本消息到企业微信群"""
    if not webhook_url:
        print("[微信] Webhook未配置，跳过推送")
        return False
    try:
        payload = {
            "msgtype": "text",
            "text": {"content": content}
        }
        r = requests.post(webhook_url, json=payload, timeout=10)
        result = r.json()
        if result.get("errcode") == 0:
            print("[微信] 消息发送成功")
            return True
        else:
            print(f"[微信] 发送失败: {result}")
            return False
    except Exception as e:
        print(f"[微信] 发送异常: {e}")
        return False


def wecom_send_markdown(webhook_url, content):
    """发送Markdown消息到企业微信群"""
    if not webhook_url:
        print("[微信] Webhook未配置，跳过推送")
        return False
    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": content}
        }
        r = requests.post(webhook_url, json=payload, timeout=10)
        result = r.json()
        if result.get("errcode") == 0:
            print("[微信] Markdown消息发送成功")
            return True
        else:
            print(f"[微信] 发送失败: {result}")
            return False
    except Exception as e:
        print(f"[微信] 发送异常: {e}")
        return False


def wecom_send_image(webhook_url, image_path):
    """发送图片到企业微信群（base64编码）"""
    if not webhook_url:
        print("[微信] Webhook未配置，跳过推送")
        return False
    if not os.path.exists(image_path):
        print(f"[微信] 图片不存在: {image_path}")
        return False
    try:
        with open(image_path, 'rb') as f:
            img_data = f.read()
        img_base64 = base64.b64encode(img_data).decode()
        img_md5 = hashlib.md5(img_data).hexdigest()

        payload = {
            "msgtype": "image",
            "image": {
                "base64": img_base64,
                "md5": img_md5
            }
        }
        r = requests.post(webhook_url, json=payload, timeout=10)
        result = r.json()
        if result.get("errcode") == 0:
            print(f"[微信] 图片发送成功: {image_path}")
            return True
        else:
            print(f"[微信] 图片发送失败: {result}")
            return False
    except Exception as e:
        print(f"[微信] 图片发送异常: {e}")
        return False


# ============================================================
# 报告生成
# ============================================================
def build_alarm_message(record, alarm_level, messages):
    """构建报警消息"""
    if alarm_level == 0:
        return None

    level_text = "🔴 严重报警" if alarm_level == 2 else "🟡 预警提醒"
    now_str = record.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    msg = f"""{level_text}
时间: {now_str}
设备: 油液清洁度检测仪
位置: 冲压车间机械压机
---
"""
    for m in messages:
        msg += f"{m}\n"

    msg += f"""
---
当前数据:
  等级1~5: {record.get('level1')}, {record.get('level2')}, {record.get('level3')}, {record.get('level4')}, {record.get('level5')}
  温度: {record.get('temperature')}℃
  水含量: {record.get('water_content')}ppm
  水活性: {record.get('water_activity')}aw
"""

    if alarm_level == 2:
        msg += "\n⚠️ 建议立即检查设备油液状态！"
    elif alarm_level == 1:
        msg += "\n📋 建议关注并安排巡检。"

    return msg


def build_report_message(conn, period="周"):
    """构建定期报告消息"""
    days = 7 if period == "周" else 30
    stats = get_statistics(conn, days=days)
    if not stats:
        return f"油液监测{period}报：最近{days}天无数据记录。"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    start_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    msg = f"""📊 油液监测{period}报
周期: {start_str} ~ {now_str}
设备: 油液清洁度检测仪
数据点数: {stats['count']}
---
颗粒度（平均值/最大值）:
  等级1: {stats['avg_l1']:.1f} / {stats['max_l1']}
  等级2: {stats['avg_l2']:.1f} / {stats['max_l2']}
  等级3: {stats['avg_l3']:.1f} / {stats['max_l3']}
  等级4: {stats['avg_l4']:.1f} / {stats['max_l4']}
  等级5: {stats['avg_l5']:.1f} / {stats['max_l5']}

温度: 平均{stats['avg_temp']:.1f}℃ 最高{stats['max_temp']}℃ 最低{stats['min_temp']}℃
水含量: 平均{stats['avg_water']:.1f}ppm 最高{stats['max_water']}ppm
水活性: 平均{stats['avg_activity']:.2f}aw 最高{stats['max_activity']}aw
"""

    # 统计报警次数
    c = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute('SELECT COUNT(*) FROM sensor_data WHERE timestamp >= ? AND alarm_level >= 2', (since,))
    critical_count = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM sensor_data WHERE timestamp >= ? AND alarm_level = 1', (since,))
    warning_count = c.fetchone()[0]

    if critical_count > 0 or warning_count > 0:
        msg += f"\n报警统计: 严重{critical_count}次，预警{warning_count}次"
    else:
        msg += "\n✅ 本周期内无报警记录"

    # 健康评估
    avg_max_particle = max(stats['max_l1'], stats['max_l2'], stats['max_l3'], stats['max_l4'], stats['max_l5'])
    if avg_max_particle >= CONFIG["thresholds"]["particle"]["critical"]:
        msg += "\n\n🔴 健康评估: 差 — 建议尽快检查油液并考虑换油"
    elif avg_max_particle >= CONFIG["thresholds"]["particle"]["warning"]:
        msg += "\n\n🟡 健康评估: 一般 — 油液劣化趋势明显，建议加强监测"
    else:
        msg += "\n\n🟢 健康评估: 良好 — 油液状态正常"

    return msg


# ============================================================
# 开机补采（填补电脑关机期间缺失的数据）
# ============================================================
def backfill_missing_data(conn, client, alarm_engine):
    """
    检查数据库中最后一条记录的时间，从云平台拉取历史数据补填空缺。
    适用于电脑关机/断网后重新开机时自动补采。
    """
    c = conn.cursor()
    # 获取数据库中最新一条记录的时间
    c.execute('SELECT MAX(timestamp) FROM sensor_data')
    last_ts = c.fetchone()[0]

    if not last_ts:
        print("[补采] 数据库为空，无需补采")
        return 0

    print(f"[补采] 数据库最新记录: {last_ts}")

    # 从云平台获取历史数据
    history = client.fetch_history(page_size=100, max_pages=10)
    if not history:
        print("[补采] 未获取到历史数据")
        return 0

    # 过滤出比数据库最新记录更新的数据
    new_records = [r for r in history if r["timestamp"] > last_ts]
    if not new_records:
        print("[补采] 没有缺失数据")
        return 0

    print(f"[补采] 发现 {len(new_records)} 条缺失数据，开始补填...")

    saved_count = 0
    alarm_count = 0
    for record in new_records:
        # 对补采数据也执行报警检查
        level, messages = alarm_engine.check(record)
        if save_record(conn, record, level, "; ".join(messages)):
            saved_count += 1
            if level > 0:
                alarm_count += 1
                # 补采期间的报警也推送
                if CONFIG["wecom_webhook"]:
                    msg = build_alarm_message(record, level, messages)
                    if msg:
                        wecom_send_markdown(CONFIG["wecom_webhook"], msg)

    print(f"[补采] 完成: 补填 {saved_count} 条数据，其中 {alarm_count} 条有报警")
    return saved_count


# ============================================================
# 主流程
# ============================================================
def run_once():
    """执行一次数据采集+报警检查（启动时自动补采缺失数据）"""
    conn = init_db()
    client = CloudClient()
    alarm_engine = AlarmEngine(conn)

    # 登录
    if not client.login():
        print("[主流程] 登录失败")
        return None, 0, []

    # 开机补采：填补关机期间缺失的历史数据
    backfill_missing_data(conn, client, alarm_engine)

    # 获取最新数据
    record = client.fetch_latest()
    if not record:
        print("[主流程] 获取数据失败")
        return None, 0, []

    print(f"[主流程] 采集到数据 @ {record['timestamp']}")

    # 报警检查
    alarm_level, messages = alarm_engine.check(record)

    # 保存数据
    saved = save_record(conn, record, alarm_level, "; ".join(messages))
    if saved:
        print(f"[主流程] 数据已保存 (报警等级: {alarm_level})")

    # 推送报警
    if alarm_level > 0 and CONFIG["wecom_webhook"]:
        msg = build_alarm_message(record, alarm_level, messages)
        if msg:
            wecom_send_markdown(CONFIG["wecom_webhook"], msg)

    conn.close()
    return record, alarm_level, messages


def run_report(period="周"):
    """生成并推送定期报告（生成前先补采缺失数据，再采集一次最新数据入库）"""
    conn = init_db()

    # 先采集一次最新数据
    client = CloudClient()
    alarm_engine = AlarmEngine(conn)
    if client.login():
        # 开机补采：填补关机期间缺失的历史数据
        backfill_missing_data(conn, client, alarm_engine)

        record = client.fetch_latest()
        if record:
            alarm_level, messages = alarm_engine.check(record)
            saved = save_record(conn, record, alarm_level, "; ".join(messages))
            if saved:
                print(f"[报告] 已采集最新数据 @ {record['timestamp']} (报警等级: {alarm_level})")
        else:
            print("[报告] 采集数据失败，使用已有历史数据生成报告")
    else:
        print("[报告] 登录失败，使用已有历史数据生成报告")

    # 生成报告文本
    msg = build_report_message(conn, period)
    print(f"\n{msg}")

    # 生成趋势图
    days = 7 if period == "周" else 30
    chart_path = generate_trend_chart(conn, days=days)
    summary_path = generate_summary_chart(conn)

    # 推送
    webhook = CONFIG["wecom_webhook"]
    if webhook:
        wecom_send_markdown(webhook, msg)
        if chart_path and os.path.exists(chart_path):
            time.sleep(1)  # 避免频率限制
            wecom_send_image(webhook, chart_path)
        if summary_path and os.path.exists(summary_path):
            time.sleep(1)
            wecom_send_image(webhook, summary_path)
    else:
        print("[报告] Webhook未配置，图表已保存到本地")

    conn.close()
    return msg, chart_path, summary_path


def run_daily_dashboard():
    """执行每日看板：补采缺失数据 + 采集最新数据 + 生成看板图 + 输出文本摘要"""
    conn = init_db()
    client = CloudClient()
    alarm_engine = AlarmEngine(conn)

    # 登录并采集
    if not client.login():
        print("[每日看板] 登录失败")
        conn.close()
        return None, None

    # 开机补采：填补关机期间缺失的历史数据
    backfill_missing_data(conn, client, alarm_engine)

    record = client.fetch_latest()
    if not record:
        print("[每日看板] 获取数据失败")
        conn.close()
        return None, None

    # 报警检查
    alarm_level, messages = alarm_engine.check(record)
    save_record(conn, record, alarm_level, "; ".join(messages))

    # 生成看板图
    dashboard_path = generate_dashboard_image(conn)

    # 构建文本摘要
    status = "🔴严重" if alarm_level == 2 else ("🟡预警" if alarm_level == 1 else "🟢正常")
    summary = (
        f"📊 每日油液监测报告\n"
        f"时间: {record['timestamp']}\n"
        f"状态: {status}\n"
        f"---\n"
        f"颗粒度: {record.get('level1')}/{record.get('level2')}/{record.get('level3')}/{record.get('level4')}/{record.get('level5')}\n"
        f"油温: {record.get('temperature')}℃\n"
        f"水含量: {record.get('water_content')}ppm\n"
        f"水活性: {record.get('water_activity')}aw\n"
    )
    if messages:
        summary += "---\n"
        for m in messages:
            summary += f"{m}\n"

    conn.close()
    return dashboard_path, summary


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "report_week":
            run_report("周")
        elif cmd == "report_month":
            run_report("月")
        elif cmd == "chart":
            conn = init_db()
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
            p = generate_trend_chart(conn, days=days)
            s = generate_summary_chart(conn)
            conn.close()
        elif cmd == "daily":
            dashboard_path, summary = run_daily_dashboard()
            if dashboard_path:
                print(f"\n{summary}")
            else:
                print("[每日看板] 生成失败")
        else:
            print("用法: python oil_monitor.py [report_week|report_month|chart [days]|daily]")
    else:
        # 默认：执行一次采集+报警
        record, level, msgs = run_once()
        if record:
            status = "🔴严重" if level == 2 else ("🟡预警" if level == 1 else "🟢正常")
            print(f"\n当前状态: {status}")
            for m in msgs:
                print(f"  {m}")
