import json
import logging
import ssl
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

import paho.mqtt.client as mqtt

from model.signal import Signal

logger = logging.getLogger(__name__)

RECENT_SIGNALS_SIZE = 50
SHARED = "__shared__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rc_failed(reason_code) -> bool:
    """paho 2.x entrega objetos ReasonCode; las versiones viejas, ints."""
    if hasattr(reason_code, "is_failure"):
        return bool(reason_code.is_failure)
    return int(reason_code) != 0


def load_credentials(path: Optional[str]) -> Optional[Dict[str, str]]:
    """JSON {"<device_id>": "<secreto>"} (la salida de `terraform output mqtt_device_credentials`)."""
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"{path} must be a JSON object of device_id -> password")
    return data


class MqttPublisher:
    """Publishes signals to the broker and keeps track of what was actually sent.
    """

    def __init__(self, host: str, port: int = 1883, topic_prefix: str = "sensors",
                 credentials: Optional[Dict[str, str]] = None, ca_cert: Optional[str] = None):
        self._host = host
        self._port = port
        self._topic_prefix = topic_prefix
        self._credentials = credentials
        self._ca_cert = ca_cert

        self._lock = threading.Lock()
        self._started = False
        self._clients: Dict[str, mqtt.Client] = {}
        self._connected: Dict[str, bool] = {}
        self._missing_credentials: set = set()
        self._connected_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._published_count = 0
        self._failed_count = 0
        self._delivered_count = 0
        self._last_publish_at: Optional[str] = None
        self._heartbeat_count = 0
        self._last_heartbeat_at: Optional[str] = None
        self._recent: deque = deque(maxlen=RECENT_SIGNALS_SIZE)
        self._pending_mids = {}
        self._early_acks = set()

    @property
    def _per_device(self) -> bool:
        return self._credentials is not None

    # ---------- connection ----------

    def connect(self):
        """Conecta en segundo plano: si el broker no esta arriba, paho reintenta
        solo y el simulador sigue en pie (el CLI muestra connected: no)."""
        with self._lock:
            self._started = True
        if not self._per_device:
            self._open(SHARED)
        logger.info("MQTT connecting to %s:%s in the background (%s, %s)", self._host, self._port,
                    "per-device credentials" if self._per_device else "anonymous",
                    "TLS" if self._ca_cert else "plain TCP")

    def set_devices(self, device_ids: Iterable[str]):
        """Con credenciales: una conexion por dispositivo cargado; cierra las que sobran."""
        if not self._per_device:
            return
        wanted = set(device_ids)
        with self._lock:
            started = self._started
            current = set(self._clients)
        for device_id in current - wanted:
            self._close(device_id)
        if not started:
            return
        for device_id in wanted - current:
            if device_id not in self._credentials:
                with self._lock:
                    self._missing_credentials.add(device_id)
                    self._last_error = f"no MQTT credentials for {device_id}"
                logger.error("No MQTT credentials for device %s: it cannot publish", device_id)
                continue
            self._open(device_id)
        with self._lock:
            self._missing_credentials &= wanted

    def _open(self, key: str):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, userdata=key)
        if key != SHARED:
            client.username_pw_set(key, self._credentials[key])
        if self._ca_cert:
            client.tls_set(ca_certs=self._ca_cert, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_publish = self._on_publish
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        with self._lock:
            self._clients[key] = client
            self._connected[key] = False
        # keepalive: la conexion queda abierta, no se reconecta por cada mensaje.
        client.connect_async(self._host, self._port, keepalive=60)
        client.loop_start()

    def _close(self, key: str):
        with self._lock:
            client = self._clients.pop(key, None)
            self._connected.pop(key, None)
        if client is not None:
            client.disconnect()
            client.loop_stop()

    def disconnect(self):
        with self._lock:
            keys = list(self._clients)
        for key in keys:
            self._close(key)

    def _on_connect(self, client, key, flags, reason_code, properties=None):
        ok = not _rc_failed(reason_code)
        with self._lock:
            if key in self._clients:
                self._connected[key] = ok
            if ok:
                self._connected_at = _now()
            else:
                self._last_error = f"connect refused ({key}): {reason_code}"
        log = logger.info if ok else logger.error
        log("MQTT connect %s to %s:%s -> %s", key, self._host, self._port, reason_code)

    def _on_disconnect(self, client, key, *args):
        reason_code = args[1] if len(args) > 1 else (args[0] if args else "unknown")
        with self._lock:
            if key in self._connected:
                self._connected[key] = False
            self._last_error = f"disconnected ({key}): {reason_code}"
        logger.warning("MQTT %s disconnected from %s:%s (%s)", key, self._host, self._port, reason_code)

    def _on_publish(self, client, key, mid, reason_code=None, properties=None):
        """Broker/network confirmed the message left the client."""
        with self._lock:
            self._delivered_count += 1
            record = self._pending_mids.pop((key, mid), None)
            if record is not None:
                record["delivered"] = True
                record["deliveredAt"] = _now()
            else:
                # el ack llegó antes de que publish_signal registrara el mid
                self._early_acks.add((key, mid))

    def _client_for(self, device_id: str):
        key = device_id if self._per_device else SHARED
        with self._lock:
            return key, self._clients.get(key), self._connected.get(key, False)

    # ---------- publishing ----------

    def publish_signal(self, signal: Signal) -> dict:
        topic = f"{self._topic_prefix}/{signal.device_id}/signal"
        payload = json.dumps(signal.to_dict())
        key, client, _ = self._client_for(signal.device_id)
        if client is None:
            sent, mid, error = False, None, f"no MQTT connection for {signal.device_id}"
        else:
            info = client.publish(topic, payload)
            sent, mid = info.rc == mqtt.MQTT_ERR_SUCCESS, info.mid
            error = None if sent else mqtt.error_string(info.rc)

        record = {
            "topic": topic,
            "deviceId": signal.device_id,
            "signalType": signal.signal_type,
            "durationMs": signal.duration_ms,
            "timestamp": signal.timestamp,
            "mid": mid,
            "accepted": sent,
            "delivered": False,
            "deliveredAt": None,
            "error": error,
        }

        with self._lock:
            if sent:
                self._published_count += 1
                self._last_publish_at = record["timestamp"]
                if (key, mid) in self._early_acks:
                    self._early_acks.discard((key, mid))
                    record["delivered"] = True
                    record["deliveredAt"] = _now()
                else:
                    self._pending_mids[(key, mid)] = record
            else:
                self._failed_count += 1
                self._last_error = f"publish failed: {record['error']}"
            self._recent.appendleft(record)

        if sent:
            logger.info(
                "Signal published: %s duration %.1fs (topic %s, mid %s)",
                signal.device_id, signal.duration_ms / 1000, topic, mid
            )
        else:
            logger.error("Signal NOT published: %s (%s)", signal.device_id, record["error"])

        return record

    def publish_heartbeat(self, device_id: str) -> bool:
        """Sign of life for one device; the backend marks it offline when these stop.

        QoS 0 on purpose: a lost heartbeat is simply replaced by the next one.
        """
        _, client, connected = self._client_for(device_id)
        if client is None or not connected:
            return False
        topic = f"{self._topic_prefix}/{device_id}/heartbeat"
        timestamp = _now()
        payload = json.dumps({"deviceId": device_id, "timestamp": timestamp})
        info = client.publish(topic, payload, qos=0)
        sent = info.rc == mqtt.MQTT_ERR_SUCCESS
        if sent:
            with self._lock:
                self._heartbeat_count += 1
                self._last_heartbeat_at = timestamp
            logger.debug("Heartbeat published: %s (topic %s)", device_id, topic)
        else:
            logger.warning("Heartbeat NOT published: %s (%s)", device_id, mqtt.error_string(info.rc))
        return sent

    # ---------- introspection ----------

    @property
    def is_connected(self) -> bool:
        """Conectado = hay al menos una conexion y todas estan arriba."""
        with self._lock:
            return bool(self._connected) and all(self._connected.values()) and not self._missing_credentials

    def stats(self) -> dict:
        with self._lock:
            connected = bool(self._connected) and all(self._connected.values()) and not self._missing_credentials
            return {
                "host": self._host,
                "port": self._port,
                "topicPrefix": self._topic_prefix,
                "tls": bool(self._ca_cert),
                "auth": "per-device" if self._per_device else "anonymous",
                "connected": connected,
                "connections": len(self._connected),
                "connectionsUp": sum(self._connected.values()),
                "devicesWithoutCredentials": sorted(self._missing_credentials),
                "connectedAt": self._connected_at,
                "published": self._published_count,
                "delivered": self._delivered_count,
                "failed": self._failed_count,
                "pending": len(self._pending_mids),
                "lastPublishAt": self._last_publish_at,
                "heartbeats": self._heartbeat_count,
                "lastHeartbeatAt": self._last_heartbeat_at,
                "lastError": self._last_error,
            }

    def recent_signals(self, limit: int = 10) -> List[dict]:
        with self._lock:
            return [dict(r) for r in list(self._recent)[:limit]]