"""edge-agent 入口：``python -m edge_agent.main [--simulate] [--nodes N]``。"""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from .config import AgentConfig, load_config
from .heartbeat import HeartbeatPublisher
from .mqtt_client import MqttClient
from .outbox import Outbox
from .registry import build_register_payload, register
from .simulator import planned_simulated_nodes, run_simulated
from .topics import status_topic

logger = logging.getLogger("edge_agent")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="edge-agent", description="EdgePilot edge agent：注册、5s 心跳、断网 outbox"
    )
    parser.add_argument("--node-key", default=None, help="节点唯一标识，默认取主机名")
    parser.add_argument("--mqtt-host", default=None, help="MQTT broker 主机（env MQTT_HOST）")
    parser.add_argument("--mqtt-port", type=int, default=None, help="MQTT broker 端口（env MQTT_PORT）")
    parser.add_argument("--heartbeat-s", type=float, default=None, help="心跳间隔秒，默认 5")
    parser.add_argument("--outbox-path", default=None, help="SQLite outbox 路径")
    parser.add_argument("--agent-version", default=None, help="agent 版本号")
    parser.add_argument(
        "--simulate",
        action="store_true",
        default=None,
        help="模拟模式：生成合成心跳/事件（显式标记 simulated）",
    )
    parser.add_argument("--nodes", type=int, default=2, help="模拟节点数，默认 2")
    parser.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    return parser.parse_args(argv)


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handler(signum, frame):  # noqa: ANN001 - signal 回调签名固定
        logger.info("收到信号 %s，准备优雅退出", signum)
        stop_event.set()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # pragma: no cover - 非主线程/平台限制
            logger.debug("无法安装 %s 处理器", name)


def run_agent(config: AgentConfig, stop_event: threading.Event) -> None:
    """单节点：注册 → 5s 心跳；退出时发布 retained offline。"""
    outbox = Outbox(config.outbox_path)
    mqtt = MqttClient(config, outbox)
    try:
        rc = mqtt.connect()
        if rc != 0:
            raise RuntimeError(f"mqtt connect rc={rc}")
        register(
            mqtt,
            build_register_payload(config.node_key, agent_version=config.agent_version),
        )
        heartbeat = HeartbeatPublisher(
            mqtt, config.node_key, interval_s=config.heartbeat_s
        )
        heartbeat.run(stop_event)
    finally:
        try:
            mqtt.publish(
                status_topic(config.node_key),
                "offline",
                qos=1,
                retain=True,
                persist_on_failure=False,
            )
        finally:
            mqtt.disconnect()
            outbox.close()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    try:
        config = load_config(
            node_key=args.node_key,
            mqtt_host=args.mqtt_host,
            mqtt_port=args.mqtt_port,
            heartbeat_s=args.heartbeat_s,
            outbox_path=args.outbox_path,
            agent_version=args.agent_version,
            simulate=args.simulate,
        )

        if config.simulate:
            nodes = planned_simulated_nodes(
                args.nodes,
                mqtt_host=config.mqtt_host,
                mqtt_port=config.mqtt_port,
                heartbeat_s=config.heartbeat_s,
                agent_version=config.agent_version,
            )
            run_simulated(nodes, stop_event=stop_event)
        else:
            run_agent(config, stop_event)
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        logger.info("已中断")
    except Exception as exc:  # noqa: BLE001 - 顶层入口记录后以非零码退出
        logger.error("edge-agent 运行失败: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
