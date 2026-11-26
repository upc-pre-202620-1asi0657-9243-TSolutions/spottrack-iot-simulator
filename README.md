# spottrack-iot-simulator

IoT simulator for the spottrack app. It pulls the device list from the backend,
generates parking signals and publishes them to an MQTT broker, exposing an HTTP
API plus a CLI to check how it is running and whether the signals were actually sent.

## Install

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python simulator/cli.py serve                     # or: python simulator/main.py
```

Configuration comes from flags or environment variables:

| Variable | Flag | Default |
|---|---|---|
| `MQTT_HOST` | `--mqtt-host` | `localhost` |
| `MQTT_PORT` | `--mqtt-port` | `1883` |
| `MQTT_TOPIC_PREFIX` | `--topic-prefix` | `sensors` |
| `BACKEND_DEVICES_URL` | `--backend-url` | `http://localhost:8080/api/devices` |
| `SIMULATOR_PORT` | `--port` | `5000` |
| `EMIT_INTERVAL_SECONDS` | `--interval` | `8` |
| `HEARTBEAT_INTERVAL_SECONDS` | `--heartbeat-interval` | `10` |
| — | `--min-duration-min` / `--max-duration-min` | `5` / `20` |
| `MQTT_CA_CERT` | `--ca-cert` | — (plain TCP) |
| `PAIRING_JSON` | `--pairing-json` (repeatable) | — |
| `MQTT_CREDENTIALS_FILE` | `--credentials-file` (legacy) | — |

The MQTT connection is made in the background: if the broker is down the simulator
still starts, keeps retrying and reconnects on its own. Signals published while
disconnected are counted as failed and shown as such by the CLI.

### Pairing by QR (recommended, TA-04)

Instead of passwords taken from Terraform outputs, a sensor gets its credentials
and payload profile from the pairing QR issued by the backend
(`POST /api/v1/monitoring/motion-sensors/<id>/pairing`, ADMIN, optional body
`{"payloadProfile":"compact-emulator"}`). Pass the response's `payloadJson`
(the exact QR text, format v2) to the simulator:

```bash
# CA of the brokers (spottrack-platform/infra/terraform)
./tf.sh dev output -raw mqtt_ca_cert > mqtt-ca.crt

PAIRING_JSON="$(jq -r .payloadJson pairing.json)" \
  python simulator/cli.py serve --ca-cert mqtt-ca.crt
# several sensors: --pairing-json '<payloadJson 1>' --pairing-json '<payloadJson 2>'
```

Before connecting, the simulator checks the QR like a device would and **does not
connect** (exit code 2, `Pairing rejected: ...`) when:

* the JSON is malformed, a field is missing or `v` is not `2`;
* `exp` already passed (`QR expired`);
* `profile` is not one it can publish (`default`, `compact-emulator`);
* the SHA-256 of `--ca-cert` (DER) does not match `caSha256` (`CA not recognised`),
  or no CA was given.

If the QR is valid, it connects over TLS to the `host:port` from the QR (`--mqtt-host`/`--mqtt-port`
are ignored) as user `deviceId` with password `secret`, and publishes heartbeats and
signals in the QR's `profile`. The first heartbeat makes the backend mark the pairing as `USED`.
Paired devices are fixed: `update` and `devices` answer 409 because the backend's
device list is not used. The secret is never logged nor returned by the API.
`--pairing-json` and `--credentials-file` cannot be used together.

`--credentials-file` (`{device_id: password}` from `terraform output mqtt_device_credentials`)
is **legacy**, only for brokers in `static` mode.

### Payload profiles

| Profile | `sensors/<id>/signal` | `sensors/<id>/heartbeat` |
|---|---|---|
| `default` | `{"deviceId","signalType","durationMs","timestamp"}` | `{"deviceId","timestamp"}` |
| `compact-emulator` | `{"dev","sig":"mv","dur"(seconds),"ts"}` | `{"dev","sig":"hb","ts"}` |

Devices without a pairing use `default`.

Signals in the `default` profile are published to `sensors/<deviceId>/signal` as:

```json
{"deviceId": "SENSOR-001", "signalType": "ACTIVATED", "durationMs": 585000, "timestamp": "2026-09-02T05:19:41+00:00"}
```

### Heartbeat

Every loaded device also publishes a sign of life to `sensors/<deviceId>/heartbeat`
every `HEARTBEAT_INTERVAL_SECONDS`, independently of the signal loop:

```json
{"deviceId": "SENSOR-001", "timestamp": "2026-09-02T05:19:41+00:00"}
```

The backend marks a sensor as disconnected when it stops receiving heartbeats (and
signals) for its timeout (30s by default), and back online on the next one. To see
a sensor go offline, stop the simulator, or pause both loops without stopping it:

```bash
python simulator/cli.py heartbeat stop
python simulator/cli.py loop stop
```

## Tests

```bash
python -m unittest discover -s tests
```

## CLI

All commands except `serve` and `listen` talk to a running simulator over HTTP.
Point them somewhere else with `--url` or `SIMULATOR_URL` (e.g. the VM's address);
add `--json` to any of them for the raw response.

```bash
python simulator/cli.py health            # is it alive, is MQTT connected, is the loop running
python simulator/cli.py stats             # full picture: devices, loop, broker, sent/failed counters
python simulator/cli.py signals -n 20     # last signals: sent ok / FAIL, and whether the broker acked
python simulator/cli.py watch             # live dashboard (stats + last signals), refreshed every 2s
python simulator/cli.py status            # devices currently loaded
python simulator/cli.py update            # reload the device list from the backend
python simulator/cli.py devices A-1 A-2   # load device ids by hand, to test without the backend
python simulator/cli.py emit              # publish one signal right now
python simulator/cli.py emit --device-id A-1
python simulator/cli.py loop stop|start   # pause / resume the automatic emission
python simulator/cli.py heartbeat stop|start  # pause / resume the device heartbeats
python simulator/cli.py listen            # subscribe to the broker and print the signals as they arrive
```

`signals` is the direct answer to "did the publisher really send anything":

```
WHEN       DEVICE                  DURATION  SENT   ACK    TOPIC
05:19:41   SENSOR-A                    585s  ok     ok     sensors/SENSOR-A/signal
05:19:39   SENSOR-A                    918s  FAIL   ...    sensors/SENSOR-A/signal
    error: The client is not currently connected.
```

* `SENT` — the client accepted the publish (broker reachable).
* `ACK` — paho confirmed the message left the client towards the broker.
* `listen` is the independent check: it connects to the broker as a subscriber, so
  whatever it prints really made it across the network.

## HTTP API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | liveness, MQTT connection, loop state |
| GET | `/status` | devices currently loaded |
| GET | `/stats` | simulator + MQTT publisher counters |
| GET | `/signals?limit=N` | last published signals (max 50) |
| POST | `/update` | reload devices from the backend |
| PUT | `/devices` | `{"deviceIds": [...]}` — set devices manually |
| POST | `/emit` | `{"deviceId": "..."}` (optional) — publish one signal now |
| POST | `/loop` | `{"action": "start"\|"stop"}` |
| POST | `/heartbeat` | `{"action": "start"\|"stop"}` — pause / resume heartbeats |

## Running on the VM

```bash
git clone <repo> && cd spottrack-iot-simulator
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
MQTT_HOST=<broker-host> BACKEND_DEVICES_URL=<backend>/api/devices \
  .venv/bin/python simulator/cli.py serve
```

Then, from your machine, check it with:

```bash
python simulator/cli.py --url http://<vm-ip>:5000 watch
```

Open port 5000 only to whoever needs to monitor it — the API has no authentication,
and Flask's development server is not meant for production traffic.
