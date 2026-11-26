# Vision Center 真实验收证据

本目录保存 2026-11-26 实机推理与端到端联调的**真实产物**，不包含任何视频二进制。
视频素材按约定不入库，可用脚本重新下载并核对 SHA-256。

## 1. 素材与权重

| 项 | 值 |
| --- | --- |
| 验收视频 | OpenCV 公开样例 `vtest.avi` |
| 下载地址 | <https://raw.githubusercontent.com/opencv/opencv/master/samples/data/vtest.avi> |
| 视频大小 | 8,131,690 bytes |
| 视频 SHA-256 | `45cddc9490be69345cbdab64ca583be65987e864ca408038e648db99e10516cf` |
| 权重文件 | `yolo11n.pt`（Ultralytics YOLO11n，固定版本，不训练/不导出） |
| 权重大小 | 5,613,764 bytes |
| 权重 SHA-256 | `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1` |
| 模型版本号 | `yolo11n-0ebbc80d4a76` |
| 运行设备 | CPU（torch 2.5.1+cpu，ultralytics 8.4.219，imgsz=640） |
| 许可证 | AGPL-3.0（商业使用需满足 AGPL 或购买 Ultralytics Enterprise License，二选一） |

复现素材下载：

```bash
cd backend
python scripts/fetch_vision_sample.py        # 打印 URL、大小、SHA-256
```

## 2. 真实推理产物

生成命令（vision-service 目录，使用自带 venv）：

```bash
cd vision-service
RUN_REAL_YOLO=1 VISION_MODEL_PATH=weights/yolo11n.pt \
  .venv/Scripts/python.exe -m pytest tests/test_inference_real.py -v
```

| 产物 | 说明 | SHA-256 |
| --- | --- | --- |
| `result.json`（本地 `vision-service/var/real-evidence/result.json`，2,089,093 bytes） | 完整 `vision-result/v1` artifact，795 帧逐帧检测 | `d6829b00247d105bd3d655a961b1efdf04910797dd3a4c025a49339c7fd11149` |
| [`frame-138.png`](./frame-138.png)（690,005 bytes） | 第 138 帧真实检测框标注图 | `d0c906d7c8459028ec70fac7584603537972ae9868bd711a2a6a565b21ba2f06` |

`result.json` 汇总事实：

| 指标 | 值 |
| --- | --- |
| 视频规格 | 768×576，10 FPS，795 帧，79,500 ms |
| 抽帧 | `sampled_every_n_frames=1`，`processed_frames=795`（目标 900，硬上限 2000） |
| 检测总数 | 6,790 |
| 分类计数 | person 4,618 / truck 805 / car 790 / dog 450 / kite 66 / frisbee 33 / surfboard 25 / skateboard 2 / skis 1 |
| 最高置信度 | 0.9168 |
| 推理耗时 | 77,362 ms（纯推理，不含上传与事件入库） |
| 时间戳来源 | `pts` |

该 artifact 由后端 `VisionStorage` 校验 SHA-256 与 `schema_version` 后入库；
数据库只保存任务汇总与 artifact 指针，不保存帧级明细或视频二进制。

## 3. 端到端实跑（Docker Compose）

`live-run/` 下保存的是同一次真实上传（`vtest.avi`）经 Docker Compose 全链路联调时，
后端 API 实际返回的响应片段：

| 文件 | 内容 |
| --- | --- |
| `task-detail.json` | 任务 `0c3649fd-beda-4a22-87e6-0c608dc744cc`：completed，795 帧、6,790 检测、16 事件、46,930 ms |
| `results-sample.json` | 结果详情（limit=3）与视频元数据、模型版本、`sha256` 回填 |
| `events.json` | 检测事件（`vision.detection`，`simulated=false`，带 `frame_id`/`timestamp_ms`/去重键） |
| `workflow-run-7.json` | 事件联动工作流 Run #7（workflow 4「视觉事件研判（演示）」）成功，携带 16 个事件 id |
| `analyze.json` | 任务级 Agent 研判 AgentRun #253 成功 |
| `agent-trace.json` | 对应 trace `e8bed247f46939ef7928cf7d1652945f` |
| `duplicate-upload.json` | 重复上传返回 `deduplicated=true`，复用同一 `task_uid` |

说明：`live-run/` 中响应体来自开发环境真实请求，未做任何结果改写；
`task-detail.json` 里的 `video_url` 是 1 小时 TTL 的签名 URL，已过期，仅作格式证据。
