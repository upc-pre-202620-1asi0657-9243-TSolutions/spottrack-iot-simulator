import logging
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

from interfaces.mqtt_publisher import MqttPublisher
from model.signal_generator import SignalGenerator

logger = logging.getLogger(__name__)


class SimulatorApi:
    """Expone los endpoints de control/monitoreo y controla el loop de emisión de señales."""

    def __init__(
        self,
        generator: SignalGenerator,
        publisher: MqttPublisher,
        backend_devices_url: str,
        emit_interval_seconds: int = 8,
        heartbeat_interval_seconds: int = 10
    ):
        self._generator = generator
        self._publisher = publisher
        self._backend_devices_url = backend_devices_url
        self._emit_interval_seconds = emit_interval_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._device_ids = []
        self._started_at = datetime.now(timezone.utc)
        self._last_update_at = None
        self._last_update_error = None
        self._loop_enabled = threading.Event()
        self._loop_enabled.set()
        self._heartbeat_enabled = threading.Event()
        self._heartbeat_enabled.set()
        self._app = Flask(__name__)
        self._register_routes()

    # ---------- devices ----------

    def _fetch_devices(self):
        response = requests.get(self._backend_devices_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        return [d["deviceId"] for d in data]

    # ---------- routes ----------

    def _register_routes(self):
        app = self._app

        @app.route("/health", methods=["GET"])
        def health():
            return jsonify({
                "status": "ok",
                "uptimeSeconds": round((datetime.now(timezone.utc) - self._started_at).total_seconds(), 1),
                "mqttConnected": self._publisher.is_connected,
                "loopRunning": self._loop_enabled.is_set(),
                "heartbeatRunning": self._heartbeat_enabled.is_set(),
                "deviceCount": len(self._device_ids),
            }), 200

        @app.route("/update", methods=["POST"])
        def update_devices():
            try:
                self._device_ids = self._fetch_devices()
                self._last_update_at = datetime.now(timezone.utc).isoformat()
                self._last_update_error = None
                return jsonify({"status": "ok", "count": len(self._device_ids), "deviceIds": self._device_ids}), 200
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                self._last_update_error = str(e)
                logger.error("Device update from %s failed: %s", self._backend_devices_url, e)
                return jsonify({"status": "error", "message": str(e)}), 500

        @app.route("/status", methods=["GET"])
        def status():
            return jsonify({"deviceCount": len(self._device_ids), "deviceIds": self._device_ids}), 200

        @app.route("/devices", methods=["PUT"])
        def set_devices():
            """Carga ids de dispositivos a mano (útil para probar sin el backend)."""
            body = request.get_json(silent=True) or {}
            device_ids = body.get("deviceIds")
            if not isinstance(device_ids, list) or not all(isinstance(d, str) for d in device_ids):
                return jsonify({"status": "error", "message": "deviceIds must be a list of strings"}), 400
            self._device_ids = device_ids
            self._last_update_at = datetime.now(timezone.utc).isoformat()
            return jsonify({"status": "ok", "count": len(self._device_ids), "deviceIds": self._device_ids}), 200

        @app.route("/stats", methods=["GET"])
        def stats():
            return jsonify({
                "uptimeSeconds": round((datetime.now(timezone.utc) - self._started_at).total_seconds(), 1),
                "startedAt": self._started_at.isoformat(),
                "emitIntervalSeconds": self._emit_interval_seconds,
                "loopRunning": self._loop_enabled.is_set(),
                "heartbeatIntervalSeconds": self._heartbeat_interval_seconds,
                "heartbeatRunning": self._heartbeat_enabled.is_set(),
                "backendDevicesUrl": self._backend_devices_url,
                "deviceCount": len(self._device_ids),
                "deviceIds": self._device_ids,
                "lastUpdateAt": self._last_update_at,
                "lastUpdateError": self._last_update_error,
                "mqtt": self._publisher.stats(),
            }), 200

        @app.route("/signals", methods=["GET"])
        def signals():
            try:
                limit = int(request.args.get("limit", 10))
            except ValueError:
                return jsonify({"status": "error", "message": "limit must be an integer"}), 400
            limit = max(1, min(limit, 50))
            return jsonify({"signals": self._publisher.recent_signals(limit)}), 200

        @app.route("/emit", methods=["POST"])
        def emit():
            """Fuerza la emisión de una señal, sin esperar al loop."""
            body = request.get_json(silent=True) or {}
            device_id = body.get("deviceId")
            if device_id is not None and not isinstance(device_id, str):
                return jsonify({"status": "error", "message": "deviceId must be a string"}), 400
            try:
                record = self._emit_once(device_id)
            except ValueError as e:
                return jsonify({"status": "error", "message": str(e)}), 409
            return jsonify({"status": "ok" if record["accepted"] else "error", "signal": record}), 200

        @app.route("/loop", methods=["POST"])
        def loop_control():
            body = request.get_json(silent=True) or {}
            action = body.get("action")
            if action == "start":
                self._loop_enabled.set()
            elif action == "stop":
                self._loop_enabled.clear()
            else:
                return jsonify({"status": "error", "message": "action must be 'start' or 'stop'"}), 400
            return jsonify({"status": "ok", "loopRunning": self._loop_enabled.is_set()}), 200

        @app.route("/heartbeat", methods=["POST"])
        def heartbeat_control():
            """Pausa los heartbeats para simular sensores caidos sin apagar el simulador."""
            body = request.get_json(silent=True) or {}
            action = body.get("action")
            if action == "start":
                self._heartbeat_enabled.set()
            elif action == "stop":
                self._heartbeat_enabled.clear()
            else:
                return jsonify({"status": "error", "message": "action must be 'start' or 'stop'"}), 400
            return jsonify({"status": "ok", "heartbeatRunning": self._heartbeat_enabled.is_set()}), 200

    # ---------- emission ----------

    def _emit_once(self, device_id=None):
        signal = self._generator.generate(self._device_ids, device_id=device_id)
        return self._publisher.publish_signal(signal)

    def _simulation_loop(self):
        while True:
            try:
                if self._loop_enabled.is_set() and self._device_ids:
                    self._emit_once()
            except Exception as e:  # el loop nunca debe morir por una señal fallida
                logger.exception("Simulation loop error: %s", e)
            time.sleep(self._emit_interval_seconds)

    def _heartbeat_loop(self):
        """Cada device reporta que sigue vivo, aunque el loop de señales este pausado."""
        while True:
            try:
                if self._heartbeat_enabled.is_set():
                    for device_id in list(self._device_ids):
                        self._publisher.publish_heartbeat(device_id)
            except Exception as e:  # el loop nunca debe morir por un heartbeat fallido
                logger.exception("Heartbeat loop error: %s", e)
            time.sleep(self._heartbeat_interval_seconds)

    def run(self, host: str = "0.0.0.0", port: int = 5000):
        self._publisher.connect()
        threading.Thread(target=self._simulation_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        logger.info("Simulator API listening on %s:%s", host, port)
        self._app.run(host=host, port=port)
