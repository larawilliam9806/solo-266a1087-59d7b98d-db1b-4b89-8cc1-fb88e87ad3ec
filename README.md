# 车辆轨迹匹配 API

基于 **FastAPI + SQLite** 的地图匹配(Map Matching)服务:登记带版本的有向路网,提交带 UTC 时刻的轨迹,将轨迹点吸附到 50 米内的候选路段,结合路网连通性用 HMM + Viterbi 生成全局最优的连续路径,无法连通或超距的区间标记为缺口。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`,SQLite 数据持久化在命名卷 `traj-data`(`/data/app.db`)。交互式文档见 `http://localhost:8000/docs`。

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

运行测试:

```bash
.venv/bin/python -m pytest tests/ -q
```

## 匹配算法

1. **候选生成**:每个 GPS 点投影到 50 米(`MATCH_RADIUS_M`)以内的有向路段,取投影点作为吸附位置(每点最多保留 8 个最近候选)。
2. **连通性**:候选 a → b 的路径距离 = a 沿路段到终点 + 有向图最短路径(Dijkstra,按源点缓存)+ b 在目标路段上的偏移。端点坐标一致(1e-6 度内)即视为同一图节点;方向不可逆行。
3. **全局最优(HMM + Viterbi)**:
   - 发射概率:点到路段距离的高斯似然(σ = 15m);
   - 转移概率:`exp(-|路网路径距离 − 两点球面直线距离| / 30m)`,不连通则禁止。
   - 同一时刻存在多个候选时,取全局概率最大的路径而非逐点最近邻,保证路径连续。
4. **连续路径展开**:相邻匹配点之间按最短路径补全中间路段,生成完整连续路径;进入/离开时刻按里程比例在相邻采样点时刻间插值。
5. **缺口**:
   - `out_of_range` — 点距所有路段超过 50 米,无候选;
   - `disconnected` — 前后点均有候选,但有向路网不连通,无法生成连续路径。

## 转向抽样

匹配时同时抽取**样本转向**,并作为转向证据随分析一起落库(`analysis_turns`)。只统计**同一次连续匹配**中由**相邻采样点**支撑的转向:

- 两点时间间隔 `0 < Δt ≤ 30s`;
- 两点分别匹配到**不同的有向路段** A / B;
- 两点间的有向路径**直接跨过 A、B 的共同端点**(A 的终点即 B 的起点,中间不经过其他路段);
- 在 A、B 两段上都行驶了**正里程**。

缺口(`out_of_range` / `disconnected`)两侧的点不属于同一次连续匹配,因此跨缺口的点对不计转向。每个转向计入**后一个点**所在的 UTC 日期与小时。

分析(主体 + 路径 + 缺口 + 转向证据)在**同一写事务**中写库;任一写入失败则整次分析回滚,不落库。

## 转向调查清单与统计

调查以 (路网版本, UTC 日期) 为单位,采用一份**分析编号清单**:

- `PUT /api/turn-surveys` **整份替换**该版本该日期的清单(空数组 = 已提交但零次选择)。
- 校验全部通过才落库,否则整份清单拒绝、旧清单与统计保持不变:
  - 路网版本必须存在;
  - 每个分析必须存在且属于该版本;
  - 每个分析必须带有转向证据(升级前的旧分析可查询,但需重新匹配后才可入选);
  - **同一轨迹最多选一份分析**。
- `GET /api/turn-counts` 按 (进入路段, 离开路段, UTC 小时) 返回样本转向次数;`status` 区分:
  - `no_survey` — 尚未提交清单(或清单为空);
  - `survey` — 已提交清单,`turns` 为该清单对应的分组计数(可能为 0 条)。
- 换清单与统计查询都**只读已落库的清单与转向证据,不重跑匹配**;查询只见当前整份清单对应的统计。

## 版本语义

- 路网按版本登记,版本号全局唯一,登记后**不可变**;修订路网 = 登记新版本。
- 每次匹配显式指定 `network_version`,分析全程只读取该版本的快照,**不混用版本**;分析期间登记新版本不影响进行中的匹配。
- 分析结果连同版本号一起落库,路网修订后旧分析仍可按原版本查询(`GET /api/analyses?network_version=...`)。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/networks` | 登记带版本的有向路网(路段编号、起终点坐标、长度) |
| GET | `/api/networks` | 列出路网版本 |
| GET | `/api/networks/{version}` | 查看某版本路网(含全部路段) |
| POST | `/api/trajectories` | 提交轨迹(UTC 时刻 + 坐标,按时间排序存储) |
| GET | `/api/trajectories/{id}` | 查看轨迹 |
| POST | `/api/match` | 在指定版本上匹配,返回匹配路段、进入/离开时刻、里程、缺口、转向证据 |
| GET | `/api/analyses` | 查询历史分析(可按 `network_version`、`trajectory_id` 过滤) |
| GET | `/api/analyses/{id}` | 查看分析结果(含 `turns_computed` 与 `turns` 转向证据) |
| PUT | `/api/turn-surveys` | 按路网版本 + UTC 日期整份替换调查采用的分析编号清单 |
| GET | `/api/turn-counts` | 按进出路段对 + UTC 小时查询样本转向次数 |
| GET | `/health` | 健康检查 |

### 示例

```bash
# 1. 登记路网 v1
curl -X POST localhost:8000/api/networks -H 'Content-Type: application/json' -d '{
  "version": "v1",
  "segments": [
    {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
     "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
    {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
     "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0}
  ]
}'

# 2. 提交轨迹(时刻必须为带时区的 UTC ISO8601)
curl -X POST localhost:8000/api/trajectories -H 'Content-Type: application/json' -d '{
  "name": "trip-1",
  "points": [
    {"timestamp": "2026-01-01T08:00:00Z", "lat": 39.9000, "lon": 116.4000},
    {"timestamp": "2026-01-01T08:00:10Z", "lat": 39.9000, "lon": 116.4008},
    {"timestamp": "2026-01-01T08:00:20Z", "lat": 39.9008, "lon": 116.4010}
  ]
}'

# 3. 匹配(轨迹 1,路网版本 v1)
curl -X POST localhost:8000/api/match -H 'Content-Type: application/json' \
  -d '{"trajectory_id": 1, "network_version": "v1"}'

# 4. 提交调查清单(2026-01-01 采用分析 1)
curl -X PUT localhost:8000/api/turn-surveys -H 'Content-Type: application/json' \
  -d '{"network_version": "v1", "utc_date": "2026-01-01", "analysis_ids": [1]}'

# 5. 查询样本转向次数(可按 from_segment_id / to_segment_id 过滤)
curl 'localhost:8000/api/turn-counts?network_version=v1&utc_date=2026-01-01'
```

响应示例:

```json
{
  "analysis_id": 1,
  "trajectory_id": 1,
  "network_version": "v1",
  "total_points": 7,
  "matched_points": 6,
  "total_distance_m": 183.0,
  "path": [
    {"seq": 0, "segment_id": "S1", "enter_time": "2026-01-01T08:00:00.000Z",
     "leave_time": "2026-01-01T08:00:20.000Z", "distance_m": 85.0}
  ],
  "gaps": [
    {"seq": 0, "start_time": "2026-01-01T08:00:30.000Z",
     "end_time": "2026-01-01T08:00:50.000Z", "reason": "out_of_range"}
  ],
  "turns": [
    {"from_point_index": 2, "to_point_index": 3, "from_segment_id": "S1",
     "to_segment_id": "S2", "from_offset_m": 42.0, "to_offset_m": 0.0,
     "from_time": "2026-01-01T08:00:20.000Z", "to_time": "2026-01-01T08:00:30.000Z",
     "gap_seconds": 10.0, "utc_date": "2026-01-01", "utc_hour": 8}
  ]
}
```

## 项目结构

```
app/
  main.py      FastAPI 路由与持久化(匹配、清单替换、统计查询)
  matcher.py   HMM + Viterbi 匹配引擎(候选、有向图最短路、缺口、转向抽样)
  geo.py       球面距离、点到线段投影
  db.py        SQLite 连接与建表(分析、转向证据、调查清单)
  schemas.py   请求校验(Pydantic)
  util.py      UTC 时间处理
tests/test_api.py    端到端测试(连续路径、两类缺口、多候选全局最优、版本隔离)
tests/test_turns.py  转向抽样、清单替换与统计查询测试
Dockerfile / docker-compose.yml
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/app.db`(容器内 `/data/app.db`) | SQLite 数据库文件路径 |

匹配参数(`app/matcher.py` 顶部常量):吸附半径 50m、发射 σ 15m、转移衰减 β 30m、每点最大候选数 8;转向抽样最大间隔 `TURN_MAX_GAP_S = 30s`。
