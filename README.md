# 希悦系统模块使用报告

本仓库用于从希悦 Redash 单校活跃度看板获取学校使用JSON数据，并基于JSON数据生成面向学校管理层的单文件 HTML 深度分析报告。

## 环境配置

复制示例配置：

```bash
cp .env.example .env
```

然后编辑 `.env`，填入自己的 Redash API key：

```text
REDASH_API_KEY=替换为自己的 Redash API key
REDASH_BASE_URL=https://redash.seiue.com
REDASH_DASHBOARD_ID=12
```

## 快速使用

在 Codex 中使用 skill 生成报告，推荐直接说明学校 ID：

```text
使用 $seiue-usage-report 为 school_id=589 生成希悦系统模块使用深度洞察报告
```


## 只获取数据快照

如需只拉取或刷新 JSON 快照，可直接运行脚本：

```bash
python3 .codex/skills/seiue-usage-report/scripts/fetch_redash_dashboard.py 589
```

## 常见问题

如果提示缺少 Redash 配置：

- 确认 `.env` 位于仓库根目录。
- 确认 `REDASH_API_KEY`、`REDASH_BASE_URL`、`REDASH_DASHBOARD_ID` 都有值。

如果生成报告很慢：

- 优先复用当天同校快照。
- 如果正在刷新快照，可先基于 `fetch_status.is_complete=false` 的增量快照生成阶段性预览。

如果报告缺少某些模块：

- 该模块可能没有有效数据、query 为空或尚未完成拉取。
- 只有有明确数据支撑的模块才会进入报告正文。
