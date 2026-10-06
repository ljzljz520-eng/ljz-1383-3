# 跑者年报站点

零第三方运行时依赖的本地 Web 应用：Python 3.11 标准库 + SQLite，完成训练文件导入、GPS 规范化测量、来源身份/重复活动管理、月份统计、比赛、个人故事、历史年报快照和隐私分享。

## 启动

```bash
python3 -m app.server
# 打开 http://localhost:8000
```

可选环境变量：`PORT=8080`、`HOST=127.0.0.1`、`RUNNER_DB=/path/to/db.sqlite3`、`HTTP_LOG=1`。

## 验收测试

```bash
python3 -m unittest discover -s tests -v
```

## 数据模型要点

- `sources`：文件名、SHA-256、provider/source ID、设备、导入元数据。
- `activities`：规范活动；`canonical_activity_id` 指向去重后的主活动。
- `track_points`：仅保存清洗后的轨迹，原始文件由来源指纹和审计元数据引用。
- `activity_day_allocations`：按本地日期拆分后的里程/移动时间，支持跨午夜。
- `activity_versions`：导入和每次手工订正的前后版本。
- `duplicate_groups` / `duplicate_members` / `duplicate_split_decisions`：重复组、组成员、人工拆分决定。
- `races`：比赛成绩，独立于训练统计。
- `stories`：个人故事。
- `injuries`：默认私密的伤病字段，不写入公开年报。
- `reports`：年报不可变快照、统计规则版本、统一数据范围。
- `share_tokens` / `share_redactions`：分享令牌与住址等隐私圆。

## 判重规则

1. 文件 SHA-256 相同：同一次上传，不双算。
2. provider/source_uid 相同且开始时间相差不超过 30 分钟：同一次活动。
3. 时空轨迹：至少 55% 时间区间重叠、重采样平均路径距离不超过 120 m、综合分至少 0.82。
4. 仅时间接近、轨迹不重叠的相邻训练不会合并。
5. 人工拆分会为活动对写入阻止记录，直到用户显式“恢复为同一次”。

## GPS、距离与移动时间

- 仅当点同时满足高速边缘和孤立“飞出再飞回”特征时删除 GPS 跳点；长暂停不会被当作跳点。
- 距离由保留点 Haversine 累加；用户距离订正保留原值、生成修订版本，并按 GPS 日比例拆分。
- 当前规则：相邻点间隔超过 30 秒不计移动时间。
- 当前规则：跨本地位午夜的线段在午夜插值点拆分，里程和移动时间分别归属两边日期。
- 所有时间以 UTC 存储，统计按活动 IANA 时区本地化。

## 统计规则和年报

- `2024-v1`：旧规则，比赛在训练总量内，整天归到本地开始日，保留旧口径重生成。
- `2025-v1`：整天归到开始日，但训练与比赛分开。
- `2026-v1`：当前规则，跨午夜拆分、训练与比赛分开，距离状态区分为 `user_confirmed`、`device_estimated`、`gps_measured`。
- 报表生成快照，发布后不原地覆盖；“按原规则生成新草稿”保留旧报表并纳入新增记录。
- 所有图表和活动表从 `/api/statistics` 返回的同一 `data_range` 与 monthly/activities 数据渲染。

## 隐私分享

创建活动分享时提供隐私圆（纬度、经度、半径）。服务端先删除圆内轨迹点，并让轨迹在删除处分段，然后才生成：

- 公开分享 JSON；
- SVG 缩略图；
- 可下载 GPX；
- 聚合/泄漏检查结果。

`/api/shares/{token}?check=privacy` 会检查公开点和下载点中是否还有被删除坐标，同时确认聚合入口没有原始坐标或伤病详情。隐私圆心和标签不会返回给公开页。

## 非医疗边界

页面和 `/api/methodology` 明确声明：系统只展示历史数据，不提供训练处方，也不提供伤病治疗建议。伤病备注默认私密且从公开年报快照排除。
