const express = require('express');
const cors = require('cors');
const path = require('path');
const https = require('https');
const http = require('http');

const app = express();
const PORT = process.env.PORT || 3000;

// ============================================================
// MIDDLEWARE
// ============================================================
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname)));

// ============================================================
// API PROXY - 解决浏览器CORS问题
// 将前端请求代理到迈德施云端API
// ============================================================
const API_BASE = 'https://mds.bodazl.com:8090';
const API_USER = 'mds26061103';
const API_PASS = '123456';
const API_DEVICE = 56857;
const API_SDID = 18806;        // 从设备ID
const API_GROUP_PID = 23891;   // 项目组ID
const API_DSIDS = '361240,361241,361242,361243,361244,361245,361246,361247';

let apiToken = null;
let tokenExpiry = 0;

function apiRequest(method, urlPath, body = null) {
  return new Promise((resolve, reject) => {
    const url = new URL(API_BASE + urlPath);
    const options = {
      hostname: url.hostname,
      port: url.port || 443,
      path: url.pathname + url.search,
      method: method,
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      rejectUnauthorized: false // 自签名证书兼容
    };

    if (apiToken && Date.now() < tokenExpiry) {
      options.headers['X-Token'] = apiToken;
    }

    const req = (url.protocol === 'https:' ? https : http).request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          resolve({ raw: data });
        }
      });
    });

    req.on('error', reject);
    req.setTimeout(10000, () => { req.destroy(); reject(new Error('timeout')); });

    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

async function ensureToken() {
  if (apiToken && Date.now() < tokenExpiry) return apiToken;
  try {
    const result = await apiRequest('POST', '/api/v1/login/login', {
      username: API_USER,
      password: API_PASS
    });
    if (result.code === 0 && result.data?.token) {
      apiToken = result.data.token;
      tokenExpiry = Date.now() + 3600000; // 1小时过期
      return apiToken;
    }
  } catch (e) {
    console.error('Login failed:', e.message);
  }
  return null;
}

// 代理所有API请求
app.all('/api/v1/*', async (req, res) => {
  try {
    await ensureToken();
    const urlPath = '/api/v1' + req.path.replace('/api/v1', '');
    const result = await apiRequest(req.method, urlPath, req.body);
    res.json(result);
  } catch (e) {
    console.error('Proxy error:', e.message);
    res.status(502).json({ code: -1, msg: 'API proxy error: ' + e.message });
  }
});

// ============================================================
// 报警记录 API - 从历史数据生成报警记录
// ============================================================
app.get('/api/alarms', async (req, res) => {
  try {
    const token = await ensureToken();
    if (!token) {
      return res.json({ code: 0, data: [] });
    }

    // 尝试从API获取历史数据并生成报警记录
    // 使用 hisdps 获取最近7天数据
    const now = new Date();
    const weekAgo = new Date(now.getTime() - 7 * 24 * 3600 * 1000);
    const fmt = d => d.toISOString().replace('T', ' ').substring(0, 19);
    const hisUrl = `/api/v1/device/hisdps?pid=${API_GROUP_PID}&did=${API_DEVICE}&sdid=${API_SDID}&dsids=${API_DSIDS}&start=${fmt(weekAgo)}&end=${fmt(now)}&order=desc`;
    const result = await apiRequest('GET', hisUrl);

    if (result.code === 0 && result.data?.dss) {
      // 将 hisdps 响应转换为记录数组
      const DSID_MAP = {361240:'d_1',361241:'d_2',361242:'d_3',361243:'d_4',361244:'d_5',361245:'d_6',361246:'d_7',361247:'d_8'};
      const tsMap = {};
      for (const ds of result.data.dss) {
        const dsName = DSID_MAP[ds.id] || ds.name;
        for (const dp of (ds.dps || [])) {
          if (!tsMap[dp.at]) tsMap[dp.at] = {};
          tsMap[dp.at][dsName] = dp.value;
        }
      }
      const records = Object.entries(tsMap).sort((a, b) => b[0] - a[0]).map(([at, vals]) => ({
        timestamp: new Date(parseInt(at) * 1000).toISOString().replace('T', ' ').substring(0, 19),
        level1: parseFloat(vals.d_1), level2: parseFloat(vals.d_2),
        level3: parseFloat(vals.d_3), level4: parseFloat(vals.d_4), level5: parseFloat(vals.d_5),
        temperature: parseFloat(vals.d_6), waterContent: parseFloat(vals.d_7), waterActivity: parseFloat(vals.d_8)
      }));

      const alarms = [];
      const thresholds = {
        particle: { warning: 18, critical: 25 },
        temperature: { highWarn: 55, highCritical: 65 },
        waterContent: { warning: 150, critical: 300 },
        waterActivity: { warning: 0.7, critical: 0.85 }
      };

      for (const record of records) {
        // 检查报警条件
        const alarmTypes = [];
        let maxLevel = 0;

        ['level1','level2','level3','level4','level5'].forEach(k => {
          const v = record[k];
          if (v >= thresholds.particle.critical) {
            alarmTypes.push({ type: 'particle', level: 2, channel: k, value: v });
            maxLevel = Math.max(maxLevel, 2);
          } else if (v >= thresholds.particle.warning) {
            alarmTypes.push({ type: 'particle', level: 1, channel: k, value: v });
            maxLevel = Math.max(maxLevel, 1);
          }
        });

        if (record.temperature >= thresholds.temperature.highCritical) {
          alarmTypes.push({ type: 'temperature', level: 2, value: record.temperature });
          maxLevel = Math.max(maxLevel, 2);
        } else if (record.temperature >= thresholds.temperature.highWarn) {
          alarmTypes.push({ type: 'temperature', level: 1, value: record.temperature });
          maxLevel = Math.max(maxLevel, 1);
        }

        if (record.waterContent >= thresholds.waterContent.critical) {
          alarmTypes.push({ type: 'water', level: 2, value: record.waterContent });
          maxLevel = Math.max(maxLevel, 2);
        } else if (record.waterContent >= thresholds.waterContent.warning) {
          alarmTypes.push({ type: 'water', level: 1, value: record.waterContent });
          maxLevel = Math.max(maxLevel, 1);
        }

        if (record.waterActivity >= thresholds.waterActivity.critical) {
          alarmTypes.push({ type: 'activity', level: 2, value: record.waterActivity });
          maxLevel = Math.max(maxLevel, 2);
        } else if (record.waterActivity >= thresholds.waterActivity.warning) {
          alarmTypes.push({ type: 'activity', level: 1, value: record.waterActivity });
          maxLevel = Math.max(maxLevel, 1);
        }

        if (alarmTypes.length > 0) {
          alarms.push({
            id: `ALM-${Date.now()}-${Math.random().toString(36).substr(2, 6)}`,
            timestamp: record.timestamp,
            level: maxLevel,
            types: alarmTypes,
            deviceId: API_DEVICE,
            deviceName: '油液清洁度检测仪',
            imei: '863304084381278'
          });
        }
      }

      return res.json({ code: 0, data: alarms });
    }

    res.json({ code: 0, data: [] });
  } catch (e) {
    res.json({ code: 0, data: [] });
  }
});

// ============================================================
// 设备状态 API
// ============================================================
app.get('/api/device-status', async (req, res) => {
  try {
    const token = await ensureToken();
    if (!token) {
      return res.json({
        code: -1,
        msg: '无法连接迈德施平台，设备状态未知',
        data: {
          total: 1,
          online: 0,
          offline: 0,
          alarm: 0,
          onlineRate: 0,
          offlineRate: 0,
          alarmRate: 0,
          devices: [{
            id: 56857,
            name: '油液清洁度检测仪',
            imei: '863304084381278',
            status: 'unknown',
            lastReport: null
          }]
        }
      });
    }

    const result = await apiRequest('GET', `/api/v1/device/lastdps?deviceId=56857`);
    if (result.code === 0 && result.data?.rows?.length) {
      res.json({
        code: 0,
        data: {
          total: 1,
          online: 1,
          offline: 0,
          alarm: 0,
          onlineRate: 100,
          offlineRate: 0,
          alarmRate: 0,
          devices: [{
            id: 56857,
            name: '油液清洁度检测仪',
            imei: '863304084381278',
            status: 'online',
            lastReport: new Date().toISOString().replace('T', ' ').substring(0, 19)
          }]
        }
      });
    } else {
      res.json({ code: -1, msg: 'No device data' });
    }
  } catch (e) {
    res.json({
      code: -1,
      msg: '获取设备状态异常',
      data: {
        total: 1, online: 0, offline: 0, alarm: 0,
        onlineRate: 0, offlineRate: 0, alarmRate: 0,
        devices: [{ id: 56857, name: '油液清洁度检测仪', imei: '863304084381278', status: 'unknown', lastReport: null }]
      }
    });
  }
});

// ============================================================
// 历史数据 API - 从迈德施获取历史数据并聚合
// ============================================================
app.get('/api/history', async (req, res) => {
  try {
    const token = await ensureToken();
    if (!token) {
      return res.json({ code: -1, msg: '无法连接迈德施平台' });
    }

    const { start, end, dataPoint, algorithm, interval } = req.query;
    const startDate = start ? new Date(start) : new Date(Date.now() - 7 * 24 * 3600 * 1000);
    const endDate = end ? new Date(end) : new Date();
    const dp = dataPoint || 'temperature';
    const algo = algorithm || 'raw';
    const intervalMin = parseInt(interval) || 5;

    const fmt = d => d.toISOString().replace('T', ' ').substring(0, 19);

    // 各数据点的合理值范围（用于过滤传感器通信错误产生的异常数据）
    const VALID_RANGES = {
      level1:        { min: 0, max: 100 },
      level2:        { min: 0, max: 100 },
      level3:        { min: 0, max: 100 },
      level4:        { min: 0, max: 100 },
      level5:        { min: 0, max: 100 },
      temperature:   { min: -40, max: 200 },
      waterContent:  { min: 0, max: 100 },
      waterActivity: { min: 0, max: 1 },
    };
    const range = VALID_RANGES[dp] || { min: -1e9, max: 1e9 };

    // 数据点名称→ds_id映射
    const DP_TO_DSID = {
      level1: '361240', level2: '361241', level3: '361242',
      level4: '361243', level5: '361244',
      temperature: '361245', waterContent: '361246', waterActivity: '361247'
    };
    const dsid = DP_TO_DSID[dp] || '361245';

    // 从MDS获取历史数据（支持cursor分页）
    const rawPoints = [];
    let cursor = 0;
    const pageSize = 1000; // MDS API实际每页最多返回1000条
    let hasMore = true;
    let filteredCount = 0;

    while (hasMore) {
      const hisUrl = `/api/v1/device/hisdps?pid=${API_GROUP_PID}&did=${API_DEVICE}&sdid=${API_SDID}&dsids=${dsid}&start=${fmt(startDate)}&end=${fmt(endDate)}&order=asc&cursor=${cursor}`;
      const result = await apiRequest('GET', hisUrl);

      if (result.code !== 0 || !result.data?.dss?.length) {
        break;
      }

      for (const ds of result.data.dss) {
        for (const dpItem of (ds.dps || [])) {
          const ts = new Date(parseInt(dpItem.at) * 1000);
          const val = parseFloat(dpItem.value);
          if (!isNaN(val) && val >= range.min && val <= range.max) {
            rawPoints.push({ ts, val });
          } else if (!isNaN(val)) {
            filteredCount++;
          }
        }
      }

      // 分页：API实际返回条数 < pageSize 时说明已到最后一页
      const actualCount = result.data.dss[0]?.dps?.length || 0;
      const nextCursor = result.data.cursor;
      if (nextCursor && nextCursor > 0 && actualCount > 0) {
        cursor = nextCursor;
      } else {
        hasMore = false;
      }

      // 安全限制：最多20页
      if (cursor > pageSize * 20) hasMore = false;
    }

    if (filteredCount > 0) {
      console.log(`[History] 已过滤 ${filteredCount} 条异常数据（超出合理范围 ${range.min}~${range.max}）`);
    }

    rawPoints.sort((a, b) => a.ts - b.ts);

    if (rawPoints.length === 0) {
      return res.json({ code: 0, data: { labels: [], values: [], stats: {} } });
    }

    // 按时间间隔聚合
    const intervalMs = intervalMin * 60 * 1000;
    const groups = {};
    rawPoints.forEach(p => {
      const bucket = Math.floor(p.ts.getTime() / intervalMs) * intervalMs;
      if (!groups[bucket]) groups[bucket] = [];
      groups[bucket].push(p.val);
    });

    const sortedBuckets = Object.keys(groups).map(Number).sort((a, b) => a - b);
    const labels = [];
    const values = [];

    for (const bucket of sortedBuckets) {
      const vals = groups[bucket];
      let aggVal;
      const bucketDate = new Date(bucket);

      switch (algo) {
        case 'raw':
          // 原始数据：每个点单独显示
          for (const v of vals) {
            labels.push(fmt(bucketDate));
            values.push(v);
          }
          continue;
        case 'diff':
          // 差值：当前值 - 前一个聚合值
          aggVal = vals[vals.length - 1];
          break;
        case 'avg':
          aggVal = vals.reduce((s, v) => s + v, 0) / vals.length;
          break;
        case 'max':
          aggVal = Math.max(...vals);
          break;
        case 'min':
          aggVal = Math.min(...vals);
          break;
        case 'latest':
          aggVal = vals[vals.length - 1];
          break;
        case 'earliest':
          aggVal = vals[0];
          break;
        case 'sum':
          aggVal = vals.reduce((s, v) => s + v, 0);
          break;
        case 'count':
          aggVal = vals.length;
          break;
        default:
          aggVal = vals[vals.length - 1];
      }

      labels.push(fmt(bucketDate));
      values.push(Math.round(aggVal * 100) / 100);
    }

    // diff算法特殊处理：计算相邻聚合值的差
    if (algo === 'diff' && values.length > 1) {
      for (let i = values.length - 1; i > 0; i--) {
        values[i] = Math.round((values[i] - values[i - 1]) * 100) / 100;
      }
      values[0] = 0;
    }

    // 计算统计信息
    const allVals = values.filter(v => v != null);
    const stats = {
      count: allVals.length,
      avg: allVals.length ? parseFloat((allVals.reduce((s, v) => s + v, 0) / allVals.length).toFixed(2)) : 0,
      max: allVals.length ? parseFloat(Math.max(...allVals).toFixed(2)) : 0,
      min: allVals.length ? parseFloat(Math.min(...allVals).toFixed(2)) : 0,
    };

    res.json({ code: 0, data: { labels, values, stats, dataPoint: dp, algorithm: algo, interval: intervalMin } });
  } catch (e) {
    console.error('[History API] Error:', e.message);
    res.json({ code: -1, msg: '获取历史数据失败: ' + e.message });
  }
});

// ============================================================
// 企业微信 Webhook 推送
// ============================================================
const WEBHOOK_URL = process.env.WECOM_WEBHOOK || 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=c7ba1fb2-236a-4e20-8a69-9b2e0d1447bd';
const POLL_INTERVAL = 60000; // 每60秒检测一次
const notifiedAlarms = new Set(); // 已推送过的报警ID

function sendWeChatNotification(content) {
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
            console.log('[WeChat Push] 推送成功');
            resolve(result);
          } else {
            console.error('[WeChat Push] 推送失败:', result);
            reject(new Error(result.errmsg || 'unknown error'));
          }
        } catch (e) {
          reject(e);
        }
      });
    });

    req.on('error', (e) => {
      console.error('[WeChat Push] 请求错误:', e.message);
      reject(e);
    });
    req.setTimeout(10000, () => { req.destroy(); reject(new Error('timeout')); });
    req.write(body);
    req.end();
  });
}

function buildAlarmMessage(alarm) {
  const levelText = alarm.level === 2 ? '<font color="warning">严重</font>' : '<font color="comment">警告</font>';
  const typeMap = {
    particle: '颗粒度超标',
    temperature: '温度过高',
    water: '水含量超标',
    activity: '水活性过高'
  };
  const unitMap = {
    particle: '个/mL',
    temperature: '℃',
    water: 'ppm',
    activity: 'aw'
  };

  let lines = [
    `### 油液监测报警通知`,
    `> **级别：** ${levelText}`,
    `> **时间：** ${alarm.timestamp}`,
    `> **设备：** ${alarm.deviceName || '油液清洁度检测仪'}`,
    `---`
  ];

  if (alarm.types && alarm.types.length > 0) {
    alarm.types.forEach(t => {
      const ch = t.channel ? ` (${t.channel})` : '';
      lines.push(`> **${typeMap[t.type] || t.type}**${ch}：${t.value} ${unitMap[t.type] || ''}`);
    });
  } else if (alarm.type) {
    const ch = alarm.channel ? ` (${alarm.channel})` : '';
    lines.push(`> **${typeMap[alarm.type] || alarm.typeName || alarm.type}**${ch}：${alarm.value} ${unitMap[alarm.type] || alarm.unit || ''}`);
  }

  lines.push(`---`, `> 请及时处理，登录系统查看详情`);
  return lines.join('\n');
}

// 启动定时轮询，检测新报警并推送
let lastPollTime = null;

async function pollAndNotify() {
  try {
    const token = await ensureToken();
    if (!token) {
      console.log('[Poll] API不可用，跳过轮询');
      return;
    }

    const result = await apiRequest('GET', `/api/v1/device/lastdps?deviceId=${API_DEVICE}`);
    if (result.code !== 0 || !result.data?.rows) return;

    const thresholds = {
      particle: { warning: 18, critical: 25 },
      temperature: { highWarn: 55, highCritical: 65 },
      waterContent: { warning: 150, critical: 300 },
      waterActivity: { warning: 0.7, critical: 0.85 }
    };

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

      const alarmTypes = [];
      let maxLevel = 0;

      ['level1','level2','level3','level4','level5'].forEach(k => {
        const v = record[k];
        if (v >= thresholds.particle.critical) {
          alarmTypes.push({ type: 'particle', level: 2, channel: k, value: v });
          maxLevel = Math.max(maxLevel, 2);
        } else if (v >= thresholds.particle.warning) {
          alarmTypes.push({ type: 'particle', level: 1, channel: k, value: v });
          maxLevel = Math.max(maxLevel, 1);
        }
      });

      if (record.temperature >= thresholds.temperature.highCritical) {
        alarmTypes.push({ type: 'temperature', level: 2, value: record.temperature });
        maxLevel = Math.max(maxLevel, 2);
      } else if (record.temperature >= thresholds.temperature.highWarn) {
        alarmTypes.push({ type: 'temperature', level: 1, value: record.temperature });
        maxLevel = Math.max(maxLevel, 1);
      }

      if (record.waterContent >= thresholds.waterContent.critical) {
        alarmTypes.push({ type: 'water', level: 2, value: record.waterContent });
        maxLevel = Math.max(maxLevel, 2);
      } else if (record.waterContent >= thresholds.waterContent.warning) {
        alarmTypes.push({ type: 'water', level: 1, value: record.waterContent });
        maxLevel = Math.max(maxLevel, 1);
      }

      if (record.waterActivity >= thresholds.waterActivity.critical) {
        alarmTypes.push({ type: 'activity', level: 2, value: record.waterActivity });
        maxLevel = Math.max(maxLevel, 2);
      } else if (record.waterActivity >= thresholds.waterActivity.warning) {
        alarmTypes.push({ type: 'activity', level: 1, value: record.waterActivity });
        maxLevel = Math.max(maxLevel, 1);
      }

      if (alarmTypes.length > 0) {
        const alarmId = `${record.timestamp}-${maxLevel}`;
        if (!notifiedAlarms.has(alarmId)) {
          notifiedAlarms.add(alarmId);
          const alarm = {
            timestamp: record.timestamp,
            level: maxLevel,
            types: alarmTypes,
            deviceName: '油液清洁度检测仪'
          };
          const msg = buildAlarmMessage(alarm);
          await sendWeChatNotification(msg);
          console.log(`[Poll] 推送报警: ${record.timestamp}`);
        }
      }
    }

    lastPollTime = new Date().toISOString();
    console.log(`[Poll] 轮询完成 at ${lastPollTime}`);
  } catch (e) {
    console.error('[Poll] 轮询错误:', e.message);
  }
}

// 服务启动后延迟30秒开始首次轮询，之后每60秒一次
setTimeout(() => {
  pollAndNotify();
  setInterval(pollAndNotify, POLL_INTERVAL);
}, 30000);

// 测试推送接口
app.get('/api/notify/test', async (req, res) => {
  try {
    const testMsg = [
      '### 油液监测报警通知（测试）',
      '> **级别：** <font color="warning">严重</font>',
      '> **时间：** ' + new Date().toISOString().replace('T', ' ').substring(0, 19),
      '> **设备：** 油液清洁度检测仪',
      '---',
      '> **颗粒度超标** (L1(>4um))：28.5 个/mL',
      '> **温度过高**：67.3 ℃',
      '---',
      '> 请及时处理，登录系统查看详情'
    ].join('\n');

    await sendWeChatNotification(testMsg);
    res.json({ code: 0, msg: '测试推送成功' });
  } catch (e) {
    res.status(500).json({ code: -1, msg: '推送失败: ' + e.message });
  }
});

// ============================================================
// SPA 路由 - 所有路径返回 dashboard.html
// ============================================================
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'dashboard.html'));
});

// ============================================================
// START
// ============================================================
app.listen(PORT, '0.0.0.0', () => {
  console.log(`\n  油液智能监测平台已启动`);
  console.log(`  本地访问: http://localhost:${PORT}`);
  console.log(`  局域网访问: http://0.0.0.0:${PORT}`);
  console.log(`  API代理: http://localhost:${PORT}/api/v1/...\n`);
});
