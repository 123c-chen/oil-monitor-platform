# 冲压车间油液智能监测平台

基于 Node.js + Express 的油液监测系统，提供实时数据监控、趋势分析、报警记录查询等功能。

## 功能特性

- **实时监控**：颗粒度、油温、水含量、水活性等关键指标
- **趋势分析**：7/14/30天历史数据趋势图，支持鼠标悬停查看数据点
- **报警记录**：按类型、级别、状态、时间范围筛选，支持分页和确认/解决操作
- **设备概览**：在线率、离线率、报警率、健康率环形统计图
- **多主题**：科技蓝、赛博朋克、矩阵绿、深空蓝、赤焰红、极光绿 6种主题
- **国际化**：支持中文/英文切换
- **亮度/对比度调节**：在设置中可调节画面亮度和对比度

## 技术架构

- **前端**：单文件 SPA（HTML + CSS + JS 内联），Canvas 图表绘制
- **后端**：Node.js + Express API 代理服务器
- **数据源**：迈德施 MDS-Z4 颗粒计数器（通过云平台 API）

## 本地运行

```bash
# 安装依赖
npm install

# 启动服务
npm start

# 访问 http://localhost:3000
```

## 部署到 Railway（推荐）

1. 访问 [Railway](https://railway.app/) 并登录（支持 GitHub 登录）
2. 点击 "New Project" → "Deploy from GitHub repo"
3. 选择本仓库
4. Railway 会自动检测 Node.js 项目并部署
5. 在 Settings → Variables 中可配置环境变量（如需修改 API 账号密码）
6. 部署完成后会生成公网 URL，如 `https://xxx.up.railway.app`

## 部署到 Render

1. 访问 [Render](https://render.com/) 并登录
2. 点击 "New" → "Web Service"
3. 连接 GitHub 仓库
4. 配置：
   - **Name**: oil-monitor
   - **Environment**: Node
   - **Build Command**: `npm install`
   - **Start Command**: `npm start`
5. 点击 "Create Web Service"
6. 部署完成后会生成公网 URL，如 `https://xxx.onrender.com`

## 部署到腾讯云/阿里云

如果使用国内云平台，可参考以下步骤：

```bash
# 1. 安装 Node.js
# 2. 上传代码到服务器
# 3. 安装依赖
npm install

# 4. 使用 PM2 保持进程运行
npm install -g pm2
pm2 start server.js --name oil-monitor
pm2 save
pm2 startup

# 5. 配置 Nginx 反向代理（可选）
```

## 项目结构

```
├── dashboard.html    # 前端单页应用（HTML/CSS/JS 内联）
├── server.js         # Express API 代理服务器
├── package.json      # Node.js 项目配置
├── .gitignore        # Git 忽略文件
└── README.md         # 项目说明
```

## API 说明

服务器作为 API 代理，解决浏览器直接请求迈德施云平台的 CORS 问题：

- `GET /` - 前端页面
- `ALL /api/v1/*` - 代理到迈德施云平台 API
- `GET /api/alarms` - 报警记录（支持筛选/分页）
- `GET /api/device-status` - 设备状态

## 许可证

MIT
