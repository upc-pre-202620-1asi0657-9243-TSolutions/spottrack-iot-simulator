import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "simulator"))

import cli  # noqa: E402

EXPIRED = json.dumps({"v": 2, "deviceId": "d", "secret": "super-secret", "host": "h", "port": 8883,
                      "caSha256": "0" * 64, "exp": 1, "profile": "default"})


class ServeWithPairingTest(unittest.TestCase):
    """A rejected QR stops before any MQTT connection and never prints the secret."""

    def _serve(self, *extra):
        err = io.StringIO()
        with mock.patch("interfaces.mqtt_publisher.MqttPublisher") as publisher, \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
            cli.main(["serve", *extra])
        publisher.assert_not_called()
        return exit_.exception.code, err.getvalue()

    def test_expired_qr_is_rejected(self):
        code, err = self._serve("--pairing-json", EXPIRED)
        self.assertEqual(2, code)
        self.assertIn("Pairing rejected: QR expired", err)
        self.assertNotIn("super-secret", err)

    def test_invalid_qr_is_rejected(self):
        code, err = self._serve("--pairing-json", "{}")
        self.assertEqual(2, code)
        self.assertIn("unsupported version", err)

    def test_pairing_and_credentials_file_are_exclusive(self):
        code, err = self._serve("--pairing-json", EXPIRED, "--credentials-file", "x.json")
        self.assertEqual(2, code)
        self.assertIn("not both", err)

    def test_pairing_json_env_is_used(self):
        with mock.patch.object(cli, "PAIRING_JSON", EXPIRED):
            code, err = self._serve()
        self.assertEqual(2, code)
        self.assertIn("QR expired", err)


if __name__ == "__main__":
    unittest.main()
