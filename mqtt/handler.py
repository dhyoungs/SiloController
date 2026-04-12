"""
MQTT handler — subscribes to commands and publishes state changes.

Broker  : Mosquitto running locally (localhost:1883)

Topics — subscribe
------------------
silo/command      payloads: open | close | status
silo/record       payloads: start | stop | status
silo/message      any text payload — posted as a camera status message

Topics — publish (retained)
---------------------------
silo/status       open | closed | opening | closing
silo/recording    true | false
silo/event        JSON: {"event":"opening","ts":"2026-04-12T03:11:00Z","source":"mqtt"}

Install broker:
    sudo apt install mosquitto mosquitto-clients
    sudo systemctl enable --now mosquitto

Test from another host:
    mosquitto_pub -h <pi-ip> -t silo/command -m open
    mosquitto_pub -h <pi-ip> -t silo/message -m "LAUNCH SEQUENCE ARMED"
    mosquitto_sub -h <pi-ip> -t silo/status
    mosquitto_sub -h <pi-ip> -t silo/event
"""

import json
import logging
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from core.api_log import log_api_message

logger = logging.getLogger(__name__)

BROKER_HOST  = "localhost"
BROKER_PORT  = 1883
TOPIC_CMD    = "silo/command"
TOPIC_STATUS = "silo/status"
TOPIC_RECORD = "silo/record"
TOPIC_RECSTT = "silo/recording"
TOPIC_MSG    = "silo/message"
TOPIC_EVENT  = "silo/event"


class MqttHandler:
    def __init__(self, silo, recorder, cam=None):
        self._silo     = silo
        self._recorder = recorder
        self._cam      = cam

        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        except AttributeError:
            self._client = mqtt.Client()   # paho-mqtt < 2.0

        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

        silo.add_listener(self._on_silo_state)
        silo.add_event_listener(self._on_silo_event)
        recorder.add_listener(self._on_recording_change)

    def start(self) -> None:
        try:
            self._client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
            self._client.loop_start()
            logger.info("MQTT connecting to %s:%d", BROKER_HOST, BROKER_PORT)
        except Exception as exc:
            logger.error("MQTT connect failed (%s) — MQTT interface disabled", exc)

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    # ── paho callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected")
            client.subscribe([(TOPIC_CMD, 1), (TOPIC_RECORD, 1), (TOPIC_MSG, 1)])
            self._publish(TOPIC_STATUS, self._silo.state)
            self._publish(TOPIC_RECSTT, str(self._recorder.active).lower())
        else:
            logger.error("MQTT connection refused (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            logger.warning("MQTT disconnected unexpectedly (rc=%d)", rc)

    def _on_message(self, client, userdata, msg):
        topic   = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace").strip().lower()
        logger.info("MQTT ← %s  %r", topic, payload)

        if topic == TOPIC_CMD:
            self._handle_silo_command(payload)
        elif topic == TOPIC_RECORD:
            self._handle_record_command(payload)
        elif topic == TOPIC_MSG:
            raw = msg.payload.decode("utf-8", errors="replace").strip()
            if raw:
                log_api_message(source="mqtt", action="message", payload=raw, result="ok")
                if self._cam:
                    self._cam.post_message(raw)

    # ── Command handlers ─────────────────────────────────────────────────────

    def _handle_silo_command(self, cmd: str) -> None:
        if cmd == "open":
            result = self._silo.open(source="mqtt")
            log_api_message(source="mqtt", action="open",
                            result="ok" if result["ok"] else result.get("reason", ""))
        elif cmd == "close":
            result = self._silo.close(source="mqtt")
            log_api_message(source="mqtt", action="close",
                            result="ok" if result["ok"] else result.get("reason", ""))
        elif cmd == "status":
            log_api_message(source="mqtt", action="status", result="ok")
            self._publish(TOPIC_STATUS, self._silo.state)
            return
        else:
            logger.warning("Unknown silo command: %r", cmd)
            return
        if not result["ok"]:
            logger.warning("Silo command ignored: %s", result.get("reason"))

    def _handle_record_command(self, cmd: str) -> None:
        if cmd == "start":
            result = self._recorder.start_recording(source="mqtt")
            log_api_message(source="mqtt", action="record/start",
                            result="ok" if result["ok"] else result.get("reason", ""))
        elif cmd == "stop":
            result = self._recorder.stop_recording(source="mqtt")
            log_api_message(source="mqtt", action="record/stop",
                            result="ok" if result["ok"] else result.get("reason", ""))
        elif cmd == "status":
            log_api_message(source="mqtt", action="record/status", result="ok")
            self._publish(TOPIC_RECSTT, str(self._recorder.active).lower())
            return
        else:
            logger.warning("Unknown record command: %r", cmd)
            return
        if not result["ok"]:
            logger.warning("Record command ignored: %s", result.get("reason"))

    # ── Listeners ────────────────────────────────────────────────────────────

    def _on_silo_state(self, state: str) -> None:
        self._publish(TOPIC_STATUS, state)

    def _on_silo_event(self, state: str, source: str) -> None:
        ts = (lambda n: n.strftime(f"%Y-%m-%dT%H:%M:%S.{n.microsecond // 1000:03d}Z"))(datetime.now(timezone.utc))
        payload = json.dumps({"event": state, "ts": ts, "source": source})
        self._client.publish(TOPIC_EVENT, payload, qos=1, retain=False)
        logger.info("MQTT → %s  %s", TOPIC_EVENT, payload)

    def _on_recording_change(self, active: bool) -> None:
        self._publish(TOPIC_RECSTT, str(active).lower())

    def _publish(self, topic: str, payload: str) -> None:
        self._client.publish(topic, payload, qos=1, retain=True)
        logger.info("MQTT → %s  %s", topic, payload)
