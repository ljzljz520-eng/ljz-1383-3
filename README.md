# 跑者年报站点

导入手表/手机训练文件（GPX/TCX），规范化轨迹与测量，维护活动身份、来源与订正版本，
生成可分享、保护住址隐私的年度报告。纯 Python + FastAPI + SQLite，前端零依赖（原生 SVG）。

## 运行

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
# 打开 http://127.0.0.1:8000
python -m pytest tests/ -q   # 23 个验收测试
```

## 数据模型（身份 / 来源 / 版本）

| 表 | 作用 |
|---|---|
| `activity` | 一次真实跑步的**规范身份**；`canonical=0` 表示被判重隐藏，统计不计 |
| `source_file` | 每个上传文件：来源设备、外部ID、sha256 指纹（精确重复文件 409 拒绝） |
| `activity_source` | 上传→活动多对一：同一次跑步的手表+手机文件挂在同一身份 |
| `track_point` | 规范化轨迹，跳点单独标记审计，不参与统计/导出 |
| `correction` | 订正版本历史，`device_estimated` 与 `user_confirmed` 永不混淆 |
| `activity_link` | 人工去重决定：`confirmed_duplicate` / `split`（拆分永久保留） |
| `story` / `race_result` / `injury_note` | 故事 / 比赛成绩（独立统计）/ 伤病（`private=1` 永不外泄） |
| `privacy_zone` / `share_link` | 住址脱敏区 / 分享令牌 |
| `report` + `report_snapshot` | 年报与冻结快照（年份×规则版本唯一） |

## 公开口径（也见 `GET /api/methodology` 与前端“统计口径”页）

- **距离**：WGS84 haversine 逐段求和；**GPS 跳点**：相邻点速度 >50 m/s 且“瞬移后返回”
  才剔除（真实高速移动不会被误删），剔除计数随活动展示。
- **移动时间**：相邻有效点速度 ≥0.5 m/s 的时间段累加；`elapsed` 为首末定位墙钟跨度，
  等红灯/休息计入 elapsed 但不计 moving。
- **跨午夜**：全部里程归到活动**开始日**，不拆日；v1=UTC 日期，v2=文件原始时区的当地日期
  （`start_tz_offset_min` 保留文件原始偏移）。
- **去重**：① 同来源外部ID且时间窗重叠 → 自动重复；② 否则必须**时间 AND 空间**都相似
  （重叠率、起点差、端点距、Hausdorff 四重条件）。仅时间接近或仅空间相近都不合并。
  ③ 人工 split 优先且持久，恢复只能再显式 merge。
- **成绩与训练分开**：`race` 活动（含 race_result）不进月度/年度训练跑量，单独列成绩。
- **估算与确认**：页面标签区分“设备估算/用户确认”，订正保留原值；系统不提供训练或
  伤病治疗建议（前端明示）。
- **年报快照**：生成开始即冻结 `frozen_activity_ids`，生成期间新增/订正不影响本报；
  所有图表（月度、设备vs确认、比赛）引用同一数据范围并做计数对账；
  历史年报可按 `v1-2023` 原规则重新生成（`/api/reports/{year}/regenerate`）。

## 隐私分享（四出口同裁剪）

`/s/{token}` 分享页对四个出口统一去除隐私区内点并检查：

1. **页面轨迹** GeoJSON 裁剪；整条都在家附近则整条不发布。
2. **缩略图** 纯 SVG 像素坐标，文本中不含任何经纬度。
3. **下载文件** GPX 仅含脱敏后点（测试逐点断言 > 半径）。
4. **聚合入口** 约 5 km 网格取整 + 网格边界；当活动大多从家出发且只剩单网格时，
   整体返回不可用以避免反推住址。

伤病字段不出现在任何分享 payload、缩略图、下载或年报快照中。

## 验收测试对照

| 文件 | 覆盖 |
|---|---|
| `test_pipeline.py` | GPS 跳点剔除、移动/停留时间、TCX 解析、坏文件 422 |
| `test_dedup.py` | 手表+手机同跑不双算、重复文件 409、背靠背训练不误合、仅时间近不合、人工拆分恢复 |
| `test_time_and_corrections.py` | 跨午夜归属、v1/v2 时区差异、手工订正保留版本、口径公开 |
| `test_privacy.py` | 轨迹/缩略图/下载/聚合四出口无住址坐标、伤病不泄漏、令牌校验 |
| `test_reports.py` | 快照隔离新增记录、图表同数据范围对账、成绩/训练分离、设备vs确认、历史规则重算 |
