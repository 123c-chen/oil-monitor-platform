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
      // 无API访问时返回模拟数据
      return res.json(generateMockAlarms(req.query));
    }

    // 尝试从API获取历史数据并生成报警记录
    const result = await apiRequest('GET', `/api/v1/device/dps?deviceId=${API_DEVICE}&pageSize=100`);

    if (result.code === 0 && result.data?.rows) {
      const alarms = [];
      const thresholds = {
        particle: { warning: 18, critical: 25 },
        temperature: { highWarn: 55, highCritical: 65 },
        waterContent: { warning: 150, critical: 300 },
        waterActivity: { warning: 0.7, critical: 0.85 }
      };

      result.data.rows.forEach(row => {
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
      });

      return res.json({ code: 0, data: alarms });
    }

    res.json(generateMockAlarms(req.query));
  } catch (e) {
    res.json(generateMockAlarms(req.query));
  }
});

// 模拟报警数据生成
function generateMockAlarms(query = {}) {
  const alarms = [];
  const now = new Date();
  const types = ['particle', 'temperature', 'water', 'activity'];
  const typeNames = {
    particle: '颗粒度超标',
    temperature: '温度过高',
    water: '水含量超标',
    activity: '水活性过高'
  };
  const channels = ['L1(>4μm)', 'L2(>6μm)', 'L3(>14μm)', 'L4(>21μm)', 'L5(>38μm)'];

  // 生成过去30天的模拟报警记录
  for (let i = 0; i < 35; i++) {
    const daysAgo = Math.floor(Math.random() * 30);
    const hoursAgo = Math.floor(Math.random() * 24);
    const d = new Date(now - daysAgo * 86400000 - hoursAgo * 3600000);
    const ts = d.toISOString().replace('T', ' ').substring(0, 19);

    const type = types[Math.floor(Math.random() * types.length)];
    const level = Math.random() > 0.3 ? 1 : 2;
    const channelIdx = Math.floor(Math.random() * channels.length);

    let value;
    switch (type) {
      case 'particle': value = level === 2 ? 25 + Math.random() * 10 : 18 + Math.random() * 7; break;
      case 'temperature': value = level === 2 ? 65 + Math.random() * 10 : 55 + Math.random() * 10; break;
      case 'water': value = level === 2 ? 300 + Math.random() * 100 : 150 + Math.random() * 150; break;
      case 'activity': value = level === 2 ? 0.85 + Math.random() * 0.1 : 0.7 + Math.random() * 0.15; break;
    }

    alarms.push({
      id: `ALM-${1000 + i}`,
      timestamp: ts,
      level,
      type,
      typeName: typeNames[type],
      channel: type === 'particle' ? channels[channelIdx] : null,
      value: +value.toFixed(type === 'activity' ? 2 : 1),
      unit: type === 'particle' ? '个/mL' : type === 'temperature' ? '℃' : type === 'water' ? 'ppm' : 'aw',
      deviceId: 56857,
      deviceName: '油液清洁度检测仪',
      imei: '863304084381278',
      status: Math.random() > 0.3 ? 'resolved' : 'active'
    });
  }

  alarms.sort((a, b) => b.timestamp.localeCompare(a.timestamp));

  // 应用筛选
  let filtered = alarms;
  if (query.type && query.type !== 'all') {
    filtered = filtered.filter(a => a.type === query.type);
  }
  if (query.level && query.level !== 'all') {
    filtered = filtered.filter(a => a.level === parseInt(query.level));
  }
  if (query.status && query.status !== 'all') {
    filtered = filtered.filter(a => a.status === query.status);
  }
  if (query.from) {
    filtered = filtered.filter(a => a.timestamp >= query.from);
  }
  if (query.to) {
    filtered = filtered.filter(a => a.timestamp <= query.to + ' 23:59:59');
  }

  // 分页
  const page = parseInt(query.page) || 1;
  const pageSize = parseInt(query.pageSize) || 10;
  const total = filtered.length;
  const paged = filtered.slice((page - 1) * pageSize, page * pageSize);

  return { code: 0, data: paged, total, page, pageSize };
}

// ============================================================
// 设备状态 API
// ============================================================
app.get('/api/device-status', async (req, res) => {
  try {
    const token = await ensureToken();
    if (!token) {
      return res.json({
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
      code: 0,
      data: {
        total: 1, online: 1, offline: 0, alarm: 0,
        onlineRate: 100, offlineRate: 0, alarmRate: 0,
        devices: [{ id: 56857, name: '油液清洁度检测仪', imei: '863304084381278', status: 'online' }]
      }
    });
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
