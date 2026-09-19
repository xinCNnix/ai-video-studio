"""Agent Harness 层：模型不可信，骨架兜底。

稳定性机制集中于此，业务层（services/pipeline、agent、generation）只调用不重造：
- errors   错误三分类（Retryable / Fatal / NeedsUserInput），处置策略由类别决定
- bus      项目事件总线（线程安全，SSE 推送）
- trace    run 级事件溯源账本（data/runs/*.jsonl，可回放）
- checkpoint 阶段检查点助手（断点续跑）
- watchdog single-flight 运行锁 + 心跳（同项目并发 run 互斥、卡死判死）
- state    项目 v2 状态模型 + 写入口不变量（非法状态不可表示）
"""
