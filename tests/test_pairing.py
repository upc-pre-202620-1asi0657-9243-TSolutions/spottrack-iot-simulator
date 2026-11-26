import hashlib
import json
import os
import ssl
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "simulator"))

from model.pairing import PairingError, ca_sha256_of, load_pairings, parse_pairing  # noqa: E402

NOW = 1_790_000_000
DER = b"not a real certificate, only bytes to pin"
CA_SHA = hashlib.sha256(DER).hexdigest()
DEVICE = "066dd222-31ed-496a-bddf-e4711b0ee957"
SECRET = "Zk3Qm-_9aB1cD2eF3gH4iJ5kL6mN7oP8"


def qr(**overrides):
    data = {"v": 2, "deviceId": DEVICE, "secret": SECRET, "host": "34.123.45.67", "port": 8883,
            "caSha256": CA_SHA, "exp": NOW + 900, "profile": "compact-emulator"}
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    return json.dumps(data, separators=(",", ":"))


class ParsePairingTest(unittest.TestCase):

    def test_valid_v2_qr(self):
        p = parse_pairing(qr(), now=NOW)
        self.assertEqual(DEVICE, p.device_id)
        self.assertEqual(SECRET, p.secret)
        self.assertEqual(("34.123.45.67", 8883), (p.host, p.port))
        self.assertEqual("compact-emulator", p.profile)

    def test_secret_is_not_in_the_repr(self):
        self.assertNotIn(SECRET, repr(parse_pairing(qr(), now=NOW)))

    def test_v1_is_rejected(self):
        with self.assertRaisesRegex(PairingError, "unsupported version 1"):
            parse_pairing(qr(v=1), now=NOW)

    def test_not_json(self):
        with self.assertRaisesRegex(PairingError, "not JSON"):
            parse_pairing("hello", now=NOW)

    def test_missing_fields(self):
        for name in ("deviceId", "secret", "host", "port", "caSha256", "exp", "profile"):
            data = json.loads(qr())
            del data[name]
            with self.subTest(name), self.assertRaisesRegex(PairingError, name):
                parse_pairing(json.dumps(data), now=NOW)

    def test_wrong_types_and_blanks(self):
        for overrides in ({"port": "8883"}, {"port": True}, {"exp": 1.5}, {"secret": "  "}, {"deviceId": 7}):
            with self.subTest(overrides), self.assertRaises(PairingError):
                parse_pairing(qr(**overrides), now=NOW)

    def test_expired(self):
        with self.assertRaisesRegex(PairingError, "expired"):
            parse_pairing(qr(exp=NOW), now=NOW)

    def test_bad_ca_fingerprint(self):
        with self.assertRaisesRegex(PairingError, "caSha256"):
            parse_pairing(qr(caSha256=CA_SHA.upper()), now=NOW)

    def test_bad_port(self):
        with self.assertRaisesRegex(PairingError, "port"):
            parse_pairing(qr(port=70000), now=NOW)

    def test_unknown_profile(self):
        with self.assertRaisesRegex(PairingError, "unsupported payload profile 'vendor-x'"):
            parse_pairing(qr(profile="vendor-x"), now=NOW)


class CaPinTest(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.ca = os.path.join(self._dir.name, "ca.crt")
        with open(self.ca, "w", encoding="ascii") as f:
            f.write(ssl.DER_cert_to_PEM_cert(DER))

    def tearDown(self):
        self._dir.cleanup()

    def test_sha256_is_over_the_der(self):
        self.assertEqual(CA_SHA, ca_sha256_of(self.ca))

    def test_matching_ca_gives_credentials_and_profiles(self):
        paired = load_pairings([qr()], self.ca, now=NOW)
        self.assertEqual({DEVICE: SECRET}, paired.credentials)
        self.assertEqual({DEVICE: "compact-emulator"}, paired.profiles)
        self.assertEqual([DEVICE], paired.device_ids)
        self.assertEqual(("34.123.45.67", 8883), (paired.host, paired.port))
        self.assertNotIn(SECRET, repr(paired))

    def test_other_ca_is_not_recognised(self):
        with self.assertRaisesRegex(PairingError, "CA not recognised"):
            load_pairings([qr(caSha256="0" * 64)], self.ca, now=NOW)

    def test_ca_is_required(self):
        with self.assertRaisesRegex(PairingError, "needs the broker CA"):
            load_pairings([qr()], None, now=NOW)

    def test_not_a_pem(self):
        with open(self.ca, "w", encoding="ascii") as f:
            f.write("garbage")
        with self.assertRaisesRegex(PairingError, "not a PEM"):
            load_pairings([qr()], self.ca, now=NOW)

    def test_several_devices_must_share_the_brokers(self):
        other = qr(deviceId="other", host="10.0.0.1")
        with self.assertRaisesRegex(PairingError, "other brokers"):
            load_pairings([qr(), other], self.ca, now=NOW)

    def test_several_devices(self):
        paired = load_pairings([qr(), qr(deviceId="dev-2", secret="s2", profile="default")], self.ca, now=NOW)
        self.assertEqual({DEVICE: "compact-emulator", "dev-2": "default"}, paired.profiles)

    def test_duplicate_device(self):
        with self.assertRaisesRegex(PairingError, "more than one"):
            load_pairings([qr(), qr()], self.ca, now=NOW)


if __name__ == "__main__":
    unittest.main()
