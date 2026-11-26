# YOLO 视觉识别（Vision Center）设计说明

> 本文已被完整规格取代，不再单独维护。请审查：
>
> [`2026-11-26-edgepilot-vision-spec.md`](./2026-11-26-edgepilot-vision-spec.md)
>
> 该 spec 包含架构、数据模型、API 契约、前端交互、部署、测试与开放决策。

## 许可与边界

- **模型许可**：Ultralytics YOLO11n 预训练权重采用 **AGPL-3.0**；
  权重文件不提交仓库，由 `vision-service/scripts/fetch_model.py` 或离线拷贝提供。
- **两条授权路径**：(1) 遵守 AGPL-3.0 的全部义务；(2) 向 Ultralytics
  购买 **Enterprise License**。闭源商用、以 SaaS 对外提供或分发衍生作品前，
  必须自行选择并落实其中之一。
- **不默认授予商业使用权限**：本仓库与 `commercial_use_notice` 均只做提示，
  不构成任何商业授权。
- **能力边界**：仅使用预训练模型，默认 CPU 推理、GPU 不是运行前提；不做训练、
  微调、自定义数据集、TensorRT / ONNX 导出；不做目标跟踪，`track_id` 恒为 `null`。
- **真实结果边界**：检测框与置信度必须来自真实推理；推理失败就返回失败原因，
  **不使用 Mock 或模拟结果兜底**，不伪造事件、截图或指标。
- **观测边界**：明确**不接入 Grafana / Prometheus**；沿用现有数据库与
  `/metrics`，推理任务本身不创建 Trace，Agent 研判与工作流运行才有关联 Trace。

完整规格见 [`2026-11-26-edgepilot-vision-spec.md`](./2026-11-26-edgepilot-vision-spec.md)。
