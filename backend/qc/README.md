# 自动筛选（预留）

标准确认前本目录不实现规则引擎或后台任务。

计划接入方式：

1. 从 `datasets` 的 UnifiedView 读取 state/action（及可选视频）
2. 规则产出 `suggestedDecision` + `evidence.segments[]`
3. 人工确认后再写入 `.review.json` 并允许导出

参考实现仍保留在仓库 `demo/backend/auto_filter.py`，请勿在标准确认前迁入。
