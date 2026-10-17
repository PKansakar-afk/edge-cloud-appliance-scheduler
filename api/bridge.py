"""MQTT side of the API: request/reply for POST /schedule, plus a cache of device status."""
import json
import logging
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from common import topics

log = logging.getLogger("api.bridge")


class MqttBridge:
    def __init__(self, host, port, username=None, password=None):
        self.host, self.port = host, port
        self.connected = threading.Event()
        self._lock = threading.Lock()
        self._pending = {}     # job_id -> {"event", "reply"}
        self._status = {}      # (house, device) -> last status payload
        self._schedule = {}    # (house, device) -> last schedule payload (physical node only)

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="api")
        if username:
            self.client.username_pw_set(username, password)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 30)

    def start(self):
        self.client.connect_async(self.host, self.port, 60)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()

    # ---------- callbacks ----------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        client.subscribe([("home/+/device/+/status", 1),
                          ("home/+/device/+/command/schedule", 1),
                          ("sim/+/device/+/command/schedule", 1)])
        self.connected.set()
        log.info("connected to broker")

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.connected.clear()
        log.warning("disconnected from broker")

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if any(rc.is_failure for rc in reason_codes):
            log.error("subscription refused - check the ACL entry for user 'api'")

    def _on_message(self, client, userdata, msg):
        try:
            root, house, device, kind = topics.parse_device_topic(msg.topic)
            data = json.loads(msg.payload)
        except ValueError:
            return                      # not a topic/payload we understand (e.g. an empty retained clear)
        if not isinstance(data, dict):
            return
        key = (house, device)
        if kind == "status":
            data["received"] = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._status[key] = data
        elif kind == "command/schedule":
            with self._lock:
                if root == "home":
                    self._schedule[key] = data
                slot = self._pending.get(data.get("job_id"))
            if slot:
                slot["reply"] = data
                slot["event"].set()

    # ---------- used by the REST layer ----------
    def request(self, root, house, device, payload, timeout):
        """Publish a job request and wait for the scheduler's reply (matched by job_id)."""
        if not self.connected.is_set():
            raise ConnectionError("MQTT broker not connected")
        job_id = payload["job_id"]
        slot = {"event": threading.Event(), "reply": None}
        with self._lock:
            self._pending[job_id] = slot
        try:
            self.client.publish(topics.job_request_topic(root, house, device), json.dumps(payload), qos=1)
            slot["event"].wait(timeout)
            return slot["reply"]
        finally:
            with self._lock:
                self._pending.pop(job_id, None)

    def snapshot(self):
        with self._lock:
            keys = sorted(set(self._status) | set(self._schedule))
            return [{"house": h, "device": d,
                     "status": self._status.get((h, d)), "schedule": self._schedule.get((h, d))}
                    for h, d in keys]