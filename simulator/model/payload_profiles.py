"""Payload formats the backend accepts per sensor (`payloadProfile`).

See spottrack-platform docs/interoperability-signal-equivalences.md.
"""

from model.signal import Signal

DEFAULT_PROFILE = "default"
COMPACT_PROFILE = "compact-emulator"
SUPPORTED_PROFILES = frozenset({DEFAULT_PROFILE, COMPACT_PROFILE})


def signal_payload(signal: Signal, profile: str = DEFAULT_PROFILE) -> dict:
    """Body of sensors/<deviceId>/signal in the sensor's profile."""
    if profile == COMPACT_PROFILE:
        # dev/sig/dur(seconds)/ts; "mv" is a motion capture
        return {
            "dev": signal.device_id,
            "sig": "mv",
            "dur": signal.duration_ms / 1000,
            "ts": signal.timestamp,
        }
    if profile == DEFAULT_PROFILE:
        return signal.to_dict()
    raise ValueError(f"unsupported payload profile '{profile}'")


def heartbeat_payload(device_id: str, timestamp: str, profile: str = DEFAULT_PROFILE) -> dict:
    """Body of sensors/<deviceId>/heartbeat. The backend only reads the topic, but keep the profile's shape."""
    if profile == COMPACT_PROFILE:
        return {"dev": device_id, "sig": "hb", "ts": timestamp}
    if profile == DEFAULT_PROFILE:
        return {"deviceId": device_id, "timestamp": timestamp}
    raise ValueError(f"unsupported payload profile '{profile}'")
