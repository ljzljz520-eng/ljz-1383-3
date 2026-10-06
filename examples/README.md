# 示例训练 JSON

网页可直接导入 `.gpx`、`.tcx` 或如下 `.json`：

```json
{
  "source_provider": "garmin",
  "source_uid": "watch-activity-001",
  "device_name": "Forerunner",
  "name": "滨江夜跑",
  "tz_name": "Asia/Shanghai",
  "is_race": false,
  "device_distance_m": 5032.4,
  "device_moving_s": 1740,
  "points": [
    {"time":"2026-03-01T19:00:00+08:00","lat":31.2300,"lon":121.4700},
    {"time":"2026-03-01T19:00:30+08:00","lat":31.2306,"lon":121.4700}
  ]
}
```

字段说明：

- `source_provider` + `source_uid` 表示设备/平台自然身份；同来源 ID 且开始时间接近会判重。
- `device_distance_m` 是设备估算，不会冒充用户确认。手工订正后统计使用订正值并产生新版本。
- `tz_name` 必须是 IANA 时区（如 `Asia/Shanghai`）。轨迹时间支持带偏移 ISO 字符串或 `Z`。
- GPX/TCX 时间按 UTC 解析；JSON 原生时间若没有偏移，则使用 `tz_name`。
