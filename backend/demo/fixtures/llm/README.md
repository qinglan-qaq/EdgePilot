# 演示用 Mock LLM 夹具

本目录的 `*.json` 由 `backend/scripts/seed_workflow_demo.py` 生成：脚本先从
当前租户的真实 `vision_events` 中取一条高严重度滞留事件，再把事件 id 写进
视觉与报告 Agent 的夹具，保证报告里的证据引用可以点开对应事件。

文件不入库（`.gitignore` 排除 `*.json`），因为事件 id 取决于数据库自增序列；
每次演示前运行一次脚本即可重建。默认路径与 Compose 环境变量
`LLM_FIXTURE_DIR=demo/fixtures/llm` 保持一致。
