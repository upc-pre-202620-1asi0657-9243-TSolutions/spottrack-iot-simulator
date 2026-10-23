"""CLI del simulador: levanta el simulador y permite inspeccionar cómo va,
incluyendo si el publisher MQTT realmente envió señales al broker."""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

DEFAULT_URL = os.environ.get("SIMULATOR_URL", "http://localhost:5000")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
BACKEND_DEVICES_URL = os.environ.get("BACKEND_DEVICES_URL", "http://localhost:8080/api/devices")
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "sensors")

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _color(text, code):
    return text if os.environ.get("NO_COLOR") else f"{code}{text}{RESET}"


def _ok(flag):
    return _color("yes", GREEN) if flag else _color("no", RED)


def _ago(iso_ts):
    """Segundos transcurridos desde un timestamp ISO, como texto."""
    if not iso_ts:
        return "never"
    try:
        delta = (datetime.now(timezone.utc) - datetime.fromisoformat(iso_ts)).total_seconds()
    except ValueError:
        return iso_ts
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    return f"{delta / 3600:.1f}h ago"


# ---------------- HTTP helpers ----------------

def _request(args, method, path, **kwargs):
    url = args.url.rstrip("/") + path
    try:
        response = requests.request(method, url, timeout=10, **kwargs)
    except requests.RequestException as e:
        print(_color(f"Cannot reach the simulator at {args.url}: {e}", RED), file=sys.stderr)
        print(_color("Is it running? Start it with: python simulator/cli.py serve", DIM), file=sys.stderr)
        raise SystemExit(2)
    try:
        body = response.json()
    except ValueError:
        print(_color(f"HTTP {response.status_code}: {response.text[:200]}", RED), file=sys.stderr)
        raise SystemExit(1)
    return response.status_code, body


def _emit_json(body):
    print(json.dumps(body, indent=2))


# ---------------- rendering ----------------

def _render_stats(stats):
    mqtt_stats = stats["mqtt"]
    lines = [
        "Simulator",
        f"  uptime            {stats['uptimeSeconds']}s",
        f"  emission loop     {_ok(stats['loopRunning'])} (every {stats['emitIntervalSeconds']}s)",
        f"  heartbeat         {_ok(stats['heartbeatRunning'])} (every {stats['heartbeatIntervalSeconds']}s)",
        f"  devices loaded    {stats['deviceCount']}",
        f"  last device sync  {_ago(stats['lastUpdateAt'])}",
        f"  backend           {stats['backendDevicesUrl']}",
        "",
        "MQTT publisher",
        f"  broker            {mqtt_stats['host']}:{mqtt_stats['port']} (prefix {mqtt_stats['topicPrefix']}/)",
        f"  connected         {_ok(mqtt_stats['connected'])}",
        f"  signals sent      {mqtt_stats['published']}",
        f"  confirmed by lib  {mqtt_stats['delivered']}",
        f"  failed            {mqtt_stats['failed']}",
        f"  awaiting confirm  {mqtt_stats['pending']}",
        f"  last signal       {_ago(mqtt_stats['lastPublishAt'])}",
        f"  heartbeats sent   {mqtt_stats['heartbeats']}",
        f"  last heartbeat    {_ago(mqtt_stats['lastHeartbeatAt'])}",
    ]
    if stats.get("lastUpdateError"):
        lines.append(_color(f"  device sync error {stats['lastUpdateError']}", YELLOW))
    if mqtt_stats.get("lastError"):
        lines.append(_color(f"  last mqtt error   {mqtt_stats['lastError']}", YELLOW))
    print("\n".join(lines))


def _render_signals(signals):
    if not signals:
        print(_color("No signals published yet.", DIM))
        return
    print(f"{'WHEN':<10} {'DEVICE':<22} {'DURATION':>9}  {'SENT':<6} {'ACK':<6} TOPIC")
    for s in signals:
        sent = "ok" if s["accepted"] else "FAIL"
        ack = "ok" if s["delivered"] else "..."
        sent_cell = _color(f"{sent:<6}", GREEN if s["accepted"] else RED)
        ack_cell = _color(f"{ack:<6}", GREEN if s["delivered"] else YELLOW)
        when = s["timestamp"][11:19]
        print(
            f"{when:<10} {s['deviceId']:<22} {s['durationMs'] / 1000:>8.0f}s  "
            f"{sent_cell} {ack_cell} {s['topic']}"
        )
        if s.get("error"):
            print(_color(f"    error: {s['error']}", RED))


# ---------------- commands ----------------

def cmd_serve(args):
    import logging

    from interfaces.api import SimulatorApi
    from interfaces.mqtt_publisher import MqttPublisher
    from model.signal import SignalDurationConfig
    from model.signal_generator import SignalGenerator

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    generator = SignalGenerator(SignalDurationConfig(
        min_duration_ms=args.min_duration_min * 60 * 1000,
        max_duration_ms=args.max_duration_min * 60 * 1000,
    ))
    publisher = MqttPublisher(host=args.mqtt_host, port=args.mqtt_port, topic_prefix=args.topic_prefix)
    api = SimulatorApi(
        generator=generator,
        publisher=publisher,
        backend_devices_url=args.backend_url,
        emit_interval_seconds=args.interval,
        heartbeat_interval_seconds=args.heartbeat_interval,
    )
    api.run(host=args.host, port=args.port)


def cmd_health(args):
    _, body = _request(args, "GET", "/health")
    if args.json:
        return _emit_json(body)
    print(f"simulator      {_ok(body['status'] == 'ok')}")
    print(f"mqtt connected {_ok(body['mqttConnected'])}")
    print(f"loop running   {_ok(body['loopRunning'])}")
    print(f"heartbeat      {_ok(body['heartbeatRunning'])}")
    print(f"devices        {body['deviceCount']}")
    print(f"uptime         {body['uptimeSeconds']}s")


def cmd_status(args):
    _, body = _request(args, "GET", "/status")
    if args.json:
        return _emit_json(body)
    print(f"{body['deviceCount']} device(s) loaded")
    for device_id in body["deviceIds"]:
        print(f"  - {device_id}")


def cmd_stats(args):
    _, body = _request(args, "GET", "/stats")
    if args.json:
        return _emit_json(body)
    _render_stats(body)


def cmd_signals(args):
    _, body = _request(args, "GET", "/signals", params={"limit": args.limit})
    if args.json:
        return _emit_json(body)
    _render_signals(body["signals"])


def cmd_update(args):
    code, body = _request(args, "POST", "/update")
    if args.json:
        return _emit_json(body)
    if code == 200:
        print(_color(f"Loaded {body['count']} device(s) from the backend.", GREEN))
        for device_id in body["deviceIds"]:
            print(f"  - {device_id}")
    else:
        print(_color(f"Update failed: {body.get('message')}", RED))
        raise SystemExit(1)


def cmd_devices(args):
    code, body = _request(args, "PUT", "/devices", json={"deviceIds": args.device_ids})
    if args.json:
        return _emit_json(body)
    if code == 200:
        print(_color(f"{body['count']} device(s) set manually.", GREEN))
    else:
        print(_color(f"Failed: {body.get('message')}", RED))
        raise SystemExit(1)


def cmd_emit(args):
    payload = {"deviceId": args.device_id} if args.device_id else {}
    code, body = _request(args, "POST", "/emit", json=payload)
    if args.json:
        return _emit_json(body)
    if code != 200:
        print(_color(f"Emit failed: {body.get('message')}", RED))
        raise SystemExit(1)
    _render_signals([body["signal"]])


def cmd_loop(args):
    code, body = _request(args, "POST", "/loop", json={"action": args.action})
    if args.json:
        return _emit_json(body)
    if code != 200:
        print(_color(f"Failed: {body.get('message')}", RED))
        raise SystemExit(1)
    print(f"loop running   {_ok(body['loopRunning'])}")


def cmd_heartbeat(args):
    code, body = _request(args, "POST", "/heartbeat", json={"action": args.action})
    if args.json:
        return _emit_json(body)
    if code != 200:
        print(_color(f"Failed: {body.get('message')}", RED))
        raise SystemExit(1)
    print(f"heartbeat      {_ok(body['heartbeatRunning'])}")


def cmd_watch(args):
    try:
        while True:
            _, stats = _request(args, "GET", "/stats")
            _, signals = _request(args, "GET", "/signals", params={"limit": args.limit})
            print("\033[2J\033[H", end="")
            print(_color(f"spottrack simulator @ {args.url}  ({time.strftime('%H:%M:%S')})", DIM))
            print()
            _render_stats(stats)
            print()
            print(f"Last {args.limit} signals")
            _render_signals(signals["signals"])
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def cmd_listen(args):
    """Se suscribe al broker para comprobar que las señales llegan de verdad."""
    import paho.mqtt.client as mqtt

    topic = args.topic or f"{args.topic_prefix}/#"
    received = {"count": 0}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", reason_code != 0):
            print(_color(f"Connection refused by broker: {reason_code}", RED), file=sys.stderr)
            client.disconnect()
            return
        client.subscribe(topic)
        print(_color(f"Subscribed to {topic} on {args.mqtt_host}:{args.mqtt_port} - Ctrl+C to stop", DIM), flush=True)

    def on_message(client, userdata, message):
        received["count"] += 1
        try:
            payload = json.dumps(json.loads(message.payload.decode()))
        except (ValueError, UnicodeDecodeError):
            payload = repr(message.payload)
        print(f"{time.strftime('%H:%M:%S')}  {_color('RECV', GREEN)}  {message.topic}  {payload}", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    except OSError as e:
        print(_color(f"Cannot reach the broker at {args.mqtt_host}:{args.mqtt_port}: {e}", RED), file=sys.stderr)
        raise SystemExit(2)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        client.disconnect()
    print(f"\n{received['count']} message(s) received.")


# ---------------- parser ----------------

def build_parser():
    # opciones aceptadas antes o despues del subcomando
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS,
                        help=f"simulator API base url (default {DEFAULT_URL})")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the raw JSON response")

    parser = argparse.ArgumentParser(
        prog="simulator",
        description="Control and monitoring CLI for the spottrack IoT simulator.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the simulator (API + emission loop)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("SIMULATOR_PORT", 5000)))
    p.add_argument("--mqtt-host", default=MQTT_HOST)
    p.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    p.add_argument("--topic-prefix", default=TOPIC_PREFIX)
    p.add_argument("--backend-url", default=BACKEND_DEVICES_URL)
    p.add_argument("--interval", type=int, default=int(os.environ.get("EMIT_INTERVAL_SECONDS", 8)),
                   help="seconds between emitted signals")
    p.add_argument("--heartbeat-interval", type=int,
                   default=int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", 10)),
                   help="seconds between heartbeats of each device")
    p.add_argument("--min-duration-min", type=int, default=5, help="minimum signal duration in minutes")
    p.add_argument("--max-duration-min", type=int, default=20, help="maximum signal duration in minutes")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("health", parents=[common], help="quick liveness check").set_defaults(func=cmd_health)
    sub.add_parser("status", parents=[common], help="devices currently loaded").set_defaults(func=cmd_status)
    sub.add_parser("stats", parents=[common], help="full simulator + MQTT publisher stats").set_defaults(func=cmd_stats)
    sub.add_parser("update", parents=[common], help="reload devices from the backend").set_defaults(func=cmd_update)

    p = sub.add_parser("signals", parents=[common], help="recent signals and whether they were sent")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.set_defaults(func=cmd_signals)

    p = sub.add_parser("devices", parents=[common], help="set device ids manually (test without the backend)")
    p.add_argument("device_ids", nargs="+")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("emit", parents=[common], help="publish one signal right now")
    p.add_argument("--device-id", help="device to emit for (default: random from the loaded ones)")
    p.set_defaults(func=cmd_emit)

    p = sub.add_parser("loop", parents=[common], help="start or stop the emission loop")
    p.add_argument("action", choices=["start", "stop"])
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("heartbeat", parents=[common], help="start or stop the device heartbeats")
    p.add_argument("action", choices=["start", "stop"])
    p.set_defaults(func=cmd_heartbeat)

    p = sub.add_parser("watch", parents=[common], help="live dashboard, refreshed periodically")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("-n", "--limit", type=int, default=10)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("listen", help="subscribe to the broker to verify signals actually arrive")
    p.add_argument("--mqtt-host", default=MQTT_HOST)
    p.add_argument("--mqtt-port", type=int, default=MQTT_PORT)
    p.add_argument("--topic-prefix", default=TOPIC_PREFIX)
    p.add_argument("--topic", help="full topic filter (default: <prefix>/#)")
    p.set_defaults(func=cmd_listen)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    # --url/--json usan SUPPRESS para poder ir antes o despues del subcomando
    args.url = getattr(args, "url", DEFAULT_URL)
    args.json = getattr(args, "json", False)
    args.func(args)


if __name__ == "__main__":
    main()
