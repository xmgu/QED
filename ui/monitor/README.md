# QED NiceGUI Monitor (A1 阶段)

独立于 Streamlit 的只读实时监控页面，使用 NiceGUI 框架。

## 原理

与 TUI 共享同一 `qed_progress/` 数据层，从 `<output_dir>/.qed_ui/progress_live.json` 轮询。

## 启动

```bash
# 安装依赖
.venv/bin/pip install "nicegui>=1.4,<3.0"

# 启动（默认 127.0.0.1:8502）
.venv/bin/python -m ui.monitor --port 8502

# 浏览器打开
http://127.0.0.1:8502/monitor?dir=/path/to/proof/output
```

## 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--dir` | `""` | 默认 output_dir，留空则在 URL 中通过 `?dir=` 指定 |
| `--host` | `127.0.0.1` | 监听地址；页面可枚举任意目录，默认仅本机可达，确需局域网访问时显式指定 |
| `--port` | `8502` | 监听端口 |
| `--interval` | `1.5` | 轮询间隔（秒） |
| `--stale` | `60.0` | 超过此秒数判定为 STALE |
| `--reload` | `false` | 开发热重载 |
| `--storage-secret` | `QED_MONITOR_SECRET` env | 用于 app.storage.user |

## 与 Streamlit 的关系

| 保留 Streamlit | 新增 NiceGUI |
|---|---|
| 配置编辑（config.yaml、human_help） | /monitor 监控页 |
| 启动 / 停止 / 恢复 pipeline | 实时 stage / agent / model |
| 输出目录选择 | attempt → revision → proof 树 |
| 运行历史列表 | stale / ERROR / DEAD 状态 |

启动方式：两个独立进程，Streamlit 中通过"打开监控页"链接跳转。

## 架构

```
pipeline 进程 ──写──▶ <output_dir>/.qed_ui/progress_live.json
                              │
                    ┌─────────▼──────────┐
                    │  qed_progress/     │  共享只读数据层
                    │  live/pid/tree     │
                    └─────┬──────┬───────┘
                          │      │
                     TUI  │      │  NiceGUI
                   (终端) │      │  (浏览器 8502)
                          │      │
                    Streamlit (8501) — "打开监控页" 链接
```

## 回退

删除 `ui/monitor/` 目录和 Streamlit 的 3 行链接即恢复原状。