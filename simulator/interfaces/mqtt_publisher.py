import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import List, Optional

import paho.mqtt.client as mqtt

from model.signal import Signal

logger = logging.getLogger(__name__)

RECENT_SIGNALS_SIZE = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rc_failed(reason_code) -> bool:
    """paho 2.x entrega objetos ReasonCode; las versiones viejas, ints."""
    if hasattr(reason_code, "is_failure"):
        return bool(reason_code.is_failure)
    return int(reason_code) != 0


class MqttPublisher:
    """Publishes signals to the broker and keeps track of what was actually sent."""

    def __init__(self, host: str, port: int = 1883, topic_prefix: str = "sensors"):
        self._host = host
        self._port = port
        self._topic_prefix = topic_prefix
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish

        self._lock = threading.Lock()
        self._connected = False
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

    # ---------- connection ----------

    def connect(self):
        """Conecta en segundo plano: si el broker no esta arriba, paho reintenta
        solo y el simulador sigue en pie (el CLI muestra connected: no)."""
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()
        logger.info("MQTT connecting to %s:%s in the background", self._host, self._port)

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        ok = not _rc_failed(reason_code)
        with self._lock:
            self._connected = ok
            if ok:
                self._connected_at = _now()
                self._last_error = None
            else:
                self._last_error = f"connect refused: {reason_code}"
        logger.info("MQTT connect to %s:%s -> %s", self._host, self._port, reason_code)

    def _on_disconnect(self, client, userdata, *args):
        reason_code = args[1] if len(args) > 1 else (args[0] if args else "unknown")
        with self._lock:
            self._connected = False
            self._last_error = f"disconnected: {reason_code}"
        logger.warning("MQTT disconnected from %s:%s (%s)", self._host, self._port, reason_code)

    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None):
        """Broker/network confirmed the message left the client."""
        with self._lock:
            self._delivered_count += 1
            record = self._pending_mids.pop(mid, None)
            if record is not None:
                record["delivered"] = True
                record["deliveredAt"] = _now()
            else:
                # el ack llegó antes de que publish_signal registrara el mid
                self._early_acks.add(mid)

    # ---------- publishing ----------

    def publish_signal(self, signal: Signal) -> dict:
        topic = f"{self._topic_prefix}/{signal.device_id}/signal"
        payload = json.dumps(signal.to_dict())
        info = self._client.publish(topic, payload)
        sent = info.rc == mqtt.MQTT_ERR_SUCCESS

        record = {
            "topic": topic,
            "deviceId": signal.device_id,
            "signalType": signal.signal_type,
            "durationMs": signal.duration_ms,
            "timestamp": signal.timestamp,
            "mid": info.mid,
            "accepted": sent,
            "delivered": False,
            "deliveredAt": None,
            "error": None if sent else mqtt.error_string(info.rc),
        }

        with self._lock:
            if sent:
                self._published_count += 1
                self._last_publish_at = record["timestamp"]
                if info.mid in self._early_acks:
                    self._early_acks.discard(info.mid)
                    record["delivered"] = True
                    record["deliveredAt"] = _now()
                else:
                    self._pending_mids[info.mid] = record
            else:
                self._failed_count += 1
                self._last_error = f"publish failed: {record['error']}"
            self._recent.appendleft(record)

        if sent:
            logger.info(
                "Signal published: %s duration %.1fs (topic %s, mid %s)",
                signal.device_id, signal.duration_ms / 1000, topic, info.mid
            )
        else:
            logger.error("Signal NOT published: %s (%s)", signal.device_id, record["error"])

        return record

    def publish_heartbeat(self, device_id: str) -> bool:
        """Sign of life for one device; the backend marks it offline when these stop.

        QoS 0 on purpose: a lost heartbeat is simply replaced by the next one.
        """
        if not self.is_connected:
            return False
        topic = f"{self._topic_prefix}/{device_id}/heartbeat"
        timestamp = _now()
        payload = json.dumps({"deviceId": device_id, "timestamp": timestamp})
        info = self._client.publish(topic, payload, qos=0)
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
        with self._lock:
            return self._connected

    def stats(self) -> dict:
        with self._lock:
            return {
                "host": self._host,
                "port": self._port,
                "topicPrefix": self._topic_prefix,
                "connected": self._connected,
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
