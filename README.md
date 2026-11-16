# 企业经营分析决策平台

面向企业高层管理的经营分析决策平台。覆盖经营指标看板、财务多维分析、销售漏斗洞察、客户画像构建、供应链效率监控、人力效能评估、经营预测模型、异常预警与决策建议推送的完整企业分析链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 经营指标看板 | `kpi` | 经营指标 | 指标编号、指标名称、计算公式 |
| 财务多维分析 | `finance` | 财务科目 | 科目编号、科目名称、科目类型 |
| 销售漏斗洞察 | `sales` | 漏斗阶段 | 阶段编号、阶段名称、客户数量 |
| 客户画像构建 | `customer` | 客户画像 | 画像编号、客户名称、行业分类 |
| 供应链效率监控 | `supply` | 效率指标 | 指标编号、环节名称、当前耗时 |
| 供应链轨迹核算 | `trajectory` | 事件轨迹/瓶颈数据集 | 订单、环节、尝试编号、业务时间、父环节 |
| 人力效能评估 | `hr` | 效能数据 | 数据编号、部门名称、人效指标 |
| 经营预测模型 | `forecast` | 预测任务 | 任务编号、预测对象、预测周期 |
| 异常预警中心 | `alert` | 预警记录 | 记录编号、预警类型、预警级别 |
| 决策建议推送 | `decision` | 决策建议 | 建议编号、分析主题、决策选项 |
| 多维下钻分析 | `drill` | 下钻路径 | 路径编号、起点指标、下钻维度 |
| 经营分析报告 | `report` | 分析报告 | 报告编号、报告主题、分析周期 |
| 同期对比分析 | `compare` | 对比方案 | 方案编号、对比指标、基准周期 |
| 分析数据接入 | `datasource` | 接入配置 | 配置编号、数据源名称、连接字符串 |
| 分析缓存管理 | `cache` | 缓存策略 | 策略编号、缓存对象、缓存时间 |
| 分析模板库 | `template` | 分析模板 | 模板编号、模板名称、适用场景 |
| 数据刷新调度 | `refresh` | 刷新任务 | 任务编号、数据对象、刷新类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 供应链轨迹核算（trajectory）

供应链效率监控的耗时不再使用示例值，而由事件轨迹加工得出。核心规则：

- 事件字段：`order_id`、`stage`、`attempt`（尝试编号）、`event_type`（start/end）、
  `business_time`（ISO 8601，可带时区）、`parent_stage`、`reported_at`。
- 配对：按业务时间栈式（LIFO）配对开始/结束，支持并行、返工与乱序上报；
  多候选时标记 `ambiguous` 并保留全部候选开始事件，作业段关联订单、尝试编号与区间边界。
- 耗时口径：处理时间取作业段**区间并集**（并行重叠不重复计入），等待取并集之间的空窗，
  缺结束事件保留为未完成段（只给观测时长不闭合），返工单独统计；
  结束早于开始的段标为无效、耗时留空且不补零。
- 异常原因：结束早于开始、缺一侧事件、时区不一致、父子关系成环、重复上报均给出
  error/warning 级原因；结构非法事件入库即拒绝，重复事件不重复占用时间。
- 版本化：补报事件只追加新轨迹版本（含事件指纹），配对失败可重试，新旧配对不混为完整订单。
- 瓶颈数据集：按**反事实区间并集**测算“把某环节压到基准后能减少多少订单时长”，
  并行重叠自动抵扣，不直接相加环节耗时；基准必须注明适用环节与周期，
  缺少基准（或基准周期不覆盖）时不生成效率得分或虚假贡献。
- 决策建议推送消费瓶颈数据集（`POST /api/decision/trajectory-consume`），
  每条建议附订单级可展开贡献区间与轨迹依据，重复消费幂等。

主要接口（前缀 `/api/trajectory`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/seed/reset` | 重建内置样例（并行/返工/乱序/跨时区/未结束/四类异常/补报版本） |
| POST | `/events` | 事件入库，受影响订单追加新轨迹版本 |
| POST | `/orders/{order_id}/retry` | 配对失败重试，生成新版本 |
| GET | `/orders` / `/orders/{order_id}` | 订单轨迹汇总与可展开明细（`expand` 控制展开项） |
| GET | `/orders/{order_id}/segments` | 作业段与配对依据 |
| GET | `/bottlenecks` | 瓶颈环节数据集（可折叠贡献区间） |
| GET/POST | `/benchmarks` | 效率基准查询/登记（校验环节与周期） |
| POST/GET | `/recommendations/push`、`/recommendations` | 推送/回查带轨迹依据的决策建议 |

核对脚本（仅标准库，`-I` 隔离运行）：

```bash
cd backend
.venv/bin/python -I tests/test_trajectory_engine.py
```

领域：数据工程
子方向：数据分析与数据应用
