/**
 * 油液监测报警 - 企业微信推送脚本（本地独立运行）
 * 用法: node notify.js
 * 
 * 功能：
 * - 定时轮询 MDS 设备数据
 * - 检测超标报警
 * - 通过企业微信 Webhook 推送报警通知
 * - 自动去重，同一条报警不重复推送
 */

const https = require('https');
const http = require('http');

// ============================================================
// 配置
// ============================================================
const API_BASE = 'https://mds.bodazl.com:8090';
const API_USER = 'mds26061103';
const API_PASS = '123456';
const API_DEVICE = 56857;

const WEBHOOK_URL = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=c7ba1fb2-236a-4e20-8a69-9b2e0d1447bd';
const POLL_INTERVAL = 60000; // 每60秒检测一次

// 报警阈值
const THRESHOLDS = {
  particle:    { warning: 18,  critical: 25  },
  temperature: { highWarn: 55, highCritical: 65 },
  waterContent:{ warning: 150, critical: 300 },
  waterActivity:{ warning: 0.7, critical: 0.85 }
};

// 类型中文名和单位
const TYPE_NAMES = {
  particle: '颗粒度超标',
  temperature: '温度过高',
  water: '水含量超标',
  activity: '水活性过高'
};
const TYPE_UNITS = {
  particle: '个/mL',
  temperature: '℃',
  water: 'ppm',
  activity: 'aw'
};

// 报警阈值中文名
const THRESHOLD_NAMES = {
  particle:    { warning: '警告(≥18)',  critical: '严重(≥25)'  },
  temperature: { warning: '警告(≥55℃)', critical: '严重(≥65℃)' },
  waterContent:{ warning: '警告(≥150ppm)', critical: '严重(≥300ppm)' },
  waterActivity:{ warning: '警告(≥0.7)', critical: '严重(≥0.85)' }
};

// ============================================================
// 状态
// ============================================================
let apiToken = null;
let tokenExpiry = 0;
const notifiedAlarms = new Set();

// ============================================================
// HTTP 请求工具
// ============================================================
function httpRequest(method, url, body = null) {
  return new Promise((resolve, reject) => {
    const urlObj = new URL(url);
    const options = {
      hostname: urlObj.hostname,
      port: urlObj.port || 443,
      path: urlObj.pathname + urlObj.search,
      method: method,
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      rejectUnauthorized: false
    };

    if (apiToken && Date.now() < tokenExpiry) {
      options.headers['X-Token'] = apiToken;
    }

    const req = (urlObj.protocol === 'https:' ? https : http).request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { resolve({ raw: data }); }
      });
    });

    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('timeout')); });
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

// ============================================================
// MDS API 登录
// ============================================================
async function ensureToken() {
  if (apiToken && Date.now() < tokenExpiry) return apiToken;
  try {
    const result = await httpRequest('POST', API_BASE + '/api/v1/login/login', {
      username: API_USER,
      password: API_PASS
    });
    if (result.code === 0 && result.data?.token) {
      apiToken = result.data.token;
      tokenExpiry = Date.now() + 3600000;
      console.log('[Token] 登录成功');
      return apiToken;
    }
  } catch (e) {
    console.error('[Token] 登录失败:', e.message);
  }
  return null;
}

// ============================================================
// 企业微信推送
// ============================================================
function sendWeChat(content) {
  return new Promise((resolve, reject) => {
    const url = new URL(WEBHOOK_URL);
    const body = JSON.stringify({
      msgtype: 'markdown',
      markdown: { content }
    });

    const options = {
      hostname: url.hostname,
      port: 443,
      path: url.pathname + url.search,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body)
      }
    };

    const req = https.request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          const result = JSON.parse(data);
          if (result.errcode === 0) {
            console.log('[WeChat] 推送成功');
            resolve(result);
          } else {
            console.error('[WeChat] 推送失败:', result);
            reject(new Error(result.errmsg));
          }
        } catch (e) { reject(e); }
      });
    });

    req.on('error', (e) => { console.error('[WeChat] 请求错误:', e.message); reject(e); });
    req.setTimeout(10000, () => { req.destroy(); reject(new Error('timeout')); });
    req.write(body);
    req.end();
  });
}

// ============================================================
// 构建报警消息
// ============================================================
function buildMessage(alarm) {
  const levelText = alarm.level === 2
    ? '<font color="warning">严重报警</font>'
    : '<font color="comment">警告提醒</font>';

  const lines = [
    '### 油液监测报警通知',
    `> **级别：** ${levelText}`,
    `> **时间：** ${alarm.timestamp}`,
    `> **设备：** 油液清洁度检测仪 (ID: ${API_DEVICE})`,
    '---'
  ];

  alarm.items.forEach(item => {
    const ch = item.channel ? ` (${item.channel})` : '';
    lines.push(`> **${item.name}**${ch}：${item.value} ${item.unit}`);
  });

  lines.push('---', '> 请及时处理，登录系统查看详情');
  return lines.join('\n');
}

// ============================================================
// 检测一条数据是否报警
// ============================================================
function checkAlarms(record) {
  const items = [];
  let maxLevel = 0;

  // 颗粒度 L1-L5
  const channels = [
    { key: 'level1', name: 'L1(>4um)' },
    { key: 'level2', name: 'L2(>6um)' },
    { key: 'level3', name: 'L3(>14um)' },
    { key: 'level4', name: 'L4(>21um)' },
    { key: 'level5', name: 'L5(>38um)' }
  ];

  channels.forEach(ch => {
    const v = record[ch.key];
    if (v >= THRESHOLDS.particle.critical) {
      items.push({ name: '颗粒度超标', channel: ch.name, value: v, unit: '个/mL' });
      maxLevel = Math.max(maxLevel, 2);
    } else if (v >= THRESHOLDS.particle.warning) {
      items.push({ name: '颗粒度超标', channel: ch.name, value: v, unit: '个/mL' });
      maxLevel = Math.max(maxLevel, 1);
    }
  });

  // 温度
  if (record.temperature >= THRESHOLDS.temperature.highCritical) {
    items.push({ name: '温度过高', value: record.temperature, unit: '℃' });
    maxLevel = Math.max(maxLevel, 2);
  } else if (record.temperature >= THRESHOLDS.temperature.highWarn) {
    items.push({ name: '温度过高', value: record.temperature, unit: '℃' });
    maxLevel = Math.max(maxLevel, 1);
  }

  // 水含量
  if (record.waterContent >= THRESHOLDS.waterContent.critical) {
    items.push({ name: '水含量超标', value: record.waterContent, unit: 'ppm' });
    maxLevel = Math.max(maxLevel, 2);
  } else if (record.waterContent >= THRESHOLDS.waterContent.warning) {
    items.push({ name: '水含量超标', value: record.waterContent, unit: 'ppm' });
    maxLevel = Math.max(maxLevel, 1);
  }

  // 水活性
  if (record.waterActivity >= THRESHOLDS.waterActivity.critical) {
    items.push({ name: '水活性过高', value: record.waterActivity, unit: 'aw' });
    maxLevel = Math.max(maxLevel, 2);
  } else if (record.waterActivity >= THRESHOLDS.waterActivity.warning) {
    items.push({ name: '水活性过高', value: record.waterActivity, unit: 'aw' });
    maxLevel = Math.max(maxLevel, 1);
  }

  return items.length > 0 ? { level: maxLevel, items, timestamp: record.timestamp } : null;
}

// ============================================================
// 轮询检测 + 推送
// ============================================================
async function pollAndNotify() {
  const now = new Date().toLocaleString('zh-CN');
  console.log(`\n[${now}] 开始轮询...`);

  try {
    const token = await ensureToken();
    if (!token) {
      console.log('[Poll] MDS API 不可用，跳过');
      return;
    }

    const result = await httpRequest('GET',
      `${API_BASE}/api/v1/device/dps?deviceId=${API_DEVICE}&pageSize=20`);

    if (result.code !== 0 || !result.data?.rows) {
      console.log('[Poll] 无数据返回');
      return;
    }

    let newAlarms = 0;

    for (const row of result.data.rows) {
      const m = {};
      (row.lastdp || []).forEach(dp => m[dp.name] = dp);

      const record = {
        timestamp: m.d_1?.at || row.createTime,
        level1: parseFloat(m.d_1?.value),
        level2: parseFloat(m.d_2?.value),
        level3: parseFloat(m.d_3?.value),
        level4: parseFloat(m.d_4?.value),
        level5: parseFloat(m.d_5?.value),
        temperature: parseFloat(m.d_6?.value),
        waterContent: parseFloat(m.d_7?.value),
        waterActivity: parseFloat(m.d_8?.value)
      };

      const alarm = checkAlarms(record);
      if (alarm) {
        const alarmId = `${alarm.timestamp}-${alarm.level}`;
        if (!notifiedAlarms.has(alarmId)) {
          notifiedAlarms.add(alarmId);
          const msg = buildMessage(alarm);
          await sendWeChat(msg);
          newAlarms++;
          console.log(`[Poll] 推送报警: ${alarm.timestamp} (级别:${alarm.level})`);
        }
      }
    }

    if (newAlarms === 0) {
      console.log('[Poll] 无新报警');
    } else {
      console.log(`[Poll] 本轮推送 ${newAlarms} 条报警`);
    }

  } catch (e) {
    console.error('[Poll] 错误:', e.message);
  }
}

// ============================================================
// 启动
// ============================================================
console.log('========================================');
console.log('  油液监测报警推送服务');
console.log('  企业微信 Webhook 已配置');
console.log(`  轮询间隔: ${POLL_INTERVAL / 1000} 秒`);
console.log(`  设备ID: ${API_DEVICE}`);
console.log('========================================');

// 启动后10秒首次检测，之后每60秒一次
setTimeout(() => {
  pollAndNotify();
  setInterval(pollAndNotify, POLL_INTERVAL);
}, 10000);
