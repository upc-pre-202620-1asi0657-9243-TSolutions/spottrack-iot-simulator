import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "simulator"))

import paho.mqtt.client as mqtt  # noqa: E402

from interfaces.mqtt_publisher import MqttPublisher  # noqa: E402
from model.payload_profiles import heartbeat_payload, signal_payload  # noqa: E402
from model.signal import Signal  # noqa: E402

TS = "2026-11-26T12:00:00+00:00"
SIGNAL = Signal(device_id="dev-1", signal_type="ACTIVATED", duration_ms=1500, timestamp=TS)


class PayloadProfilesTest(unittest.TestCase):

    def test_default_signal_is_the_camel_case_format(self):
        self.assertEqual({"deviceId": "dev-1", "signalType": "ACTIVATED", "durationMs": 1500, "timestamp": TS},
                         signal_payload(SIGNAL))

    def test_compact_signal(self):
        self.assertEqual({"dev": "dev-1", "sig": "mv", "dur": 1.5, "ts": TS},
                         signal_payload(SIGNAL, "compact-emulator"))

    def test_heartbeats(self):
        self.assertEqual({"deviceId": "dev-1", "timestamp": TS}, heartbeat_payload("dev-1", TS))
        self.assertEqual({"dev": "dev-1", "sig": "hb", "ts": TS}, heartbeat_payload("dev-1", TS, "compact-emulator"))

    def test_unknown_profile(self):
        with self.assertRaises(ValueError):
            signal_payload(SIGNAL, "vendor-x")


class _Info:
    rc = mqtt.MQTT_ERR_SUCCESS
    mid = 1


class _FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0):
        self.sent.append((topic, json.loads(payload)))
        return _Info()


class PublisherUsesTheDeviceProfileTest(unittest.TestCase):

    def setUp(self):
        self.publisher = MqttPublisher("h", 8883, credentials={"dev-1": "s", "dev-2": "s"},
                                       profiles={"dev-1": "compact-emulator"})
        self.clients = {"dev-1": _FakeClient(), "dev-2": _FakeClient()}
        for key, client in self.clients.items():
            self.publisher._clients[key] = client
            self.publisher._connected[key] = True

    def test_signal_in_the_paired_profile(self):
        record = self.publisher.publish_signal(SIGNAL)
        self.assertEqual("compact-emulator", record["profile"])
        self.assertEqual([("sensors/dev-1/signal", {"dev": "dev-1", "sig": "mv", "dur": 1.5, "ts": TS})],
                         self.clients["dev-1"].sent)

    def test_device_without_profile_uses_the_default(self):
        signal = Signal(device_id="dev-2", signal_type="ACTIVATED", duration_ms=1500, timestamp=TS)
        self.publisher.publish_signal(signal)
        topic, body = self.clients["dev-2"].sent[0]
        self.assertEqual("sensors/dev-2/signal", topic)
        self.assertEqual("dev-2", body["deviceId"])

    def test_heartbeat_in_the_paired_profile(self):
        self.assertTrue(self.publisher.publish_heartbeat("dev-1"))
        topic, body = self.clients["dev-1"].sent[0]
        self.assertEqual("sensors/dev-1/heartbeat", topic)
        self.assertEqual({"dev", "sig", "ts"}, set(body))
        self.assertEqual("hb", body["sig"])

    def test_stats_show_profiles_but_not_secrets(self):
        stats = self.publisher.stats()
        self.assertEqual({"dev-1": "compact-emulator"}, stats["profiles"])
        self.assertNotIn("credentials", stats)


if __name__ == "__main__":
    unittest.main()
