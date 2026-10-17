"""Replays simulated households through the broker, in lock-step with the scheduler.
Run:  docker compose run --rm simulator
"""
import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

import paho.mqtt.client as mqtt

from common import topics
from common.config import from_minute, to_minute
from eval.build_households import build_households, load_jobs

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
N_HOUSEHOLDS = int(os.getenv("N_HOUSEHOLDS", "10"))
SEED = int(os.getenv("SEED", "0"))
SIM_SPEED = float(os.getenv("SIM_SPEED", "0"))              # sim minutes per real second; 0 = max speed
TELEMETRY_STEP_MIN = int(os.getenv("TELEMETRY_STEP_MIN", "5"))
SIM_STRATEGY = os.getenv("SIM_STRATEGY", "")                # empty = leave scheduler's strategy alone
JOBS_CSV = os.getenv("JOBS_CSV", "data/processed/jobs.csv")
RESPONSE_TIMEOUT_S = float(os.getenv("RESPONSE_TIMEOUT_S", "10"))
RUN_ID = os.getenv("RUN_ID") or f"{SIM_STRATEGY or 'default'}-n{N_HOUSEHOLDS}-seed{SEED}-{int(time.time())}"

log = logging.getLogger("simulator")


class Simulator:
    def __init__(self):
        self.cond = threading.Condition()
        self.responses = {}
        self.ack = threading.Event()
        self.subscribed = threading.Event()
        self.running = defaultdict(list)       # (house, device) -> [(start, end, kw)]
        self.last_end = 0

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"simulator-{os.getpid()}")
        if MQTT_USERNAME:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.client.on_connect = self.on_connect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_message = self.on_message

    # ---------- MQTT ----------
    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        client.subscribe([("sim/+/device/+/command/schedule", 1), (topics.CONTROL_ACK, 1)])

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if any(rc.is_failure for rc in reason_codes):
            log.error("subscription refused - check the broker ACL")
            return
        self.subscribed.set()

    def on_message(self, client, userdata, msg):
        data = json.loads(msg.payload)
        if msg.topic == topics.CONTROL_ACK:
            if data.get("run_id") == RUN_ID:
                self.ack.set()
            return
        with self.cond:
            self.responses[data.get("job_id")] = data
            self.cond.notify_all()

    def connect(self):
        for _ in range(30):
            try:
                self.client.connect(MQTT_HOST, MQTT_PORT, 60)
                break
            except OSError:
                time.sleep(1)
        else:
            sys.exit("Broker not reachable.")
        self.client.loop_start()
        if not self.subscribed.wait(10):
            sys.exit("Could not subscribe (wrong credentials or ACL?).")

    def handshake(self):
        """Reset the scheduler and wait until it confirms. Doubles as a readiness check."""
        payload = {"run_id": RUN_ID}
        if SIM_STRATEGY:
            payload["strategy"] = SIM_STRATEGY
        for _ in range(30):
            self.client.publish(topics.CONTROL_RESET, json.dumps(payload), qos=1)
            if self.ack.wait(1.0):
                return
        sys.exit("The scheduler did not acknowledge the reset. Is it running?")

    # ---------- simulation ----------
    def wait_for(self, job_ids):
        end = time.time() + RESPONSE_TIMEOUT_S
        with self.cond:
            while not all(j in self.responses for j in job_ids):
                remaining = end - time.time()
                if remaining <= 0:
                    return False
                self.cond.wait(remaining)
        return True

    def dispatch(self, batch, minute):
        ids = []
        for r in batch.itertuples():
            self.client.publish(topics.job_request_topic("sim", r.household, r.appliance), json.dumps({
                "job_id": r.job_id,
                "ts": from_minute(minute).isoformat(),
                "release": from_minute(int(r.release)).isoformat(),
                "duration_min": int(r.duration_min),
                "kw": round(float(r.kw), 4),
                "deadline": from_minute(int(r.deadline)).isoformat(),
                "priority": "normal",
            }), qos=1)
            ids.append(r.job_id)
        if not self.wait_for(ids):
            log.warning("timed out waiting for scheduler replies at minute %d", minute)

        for r in batch.itertuples():
            with self.cond:
                resp = self.responses.pop(r.job_id, None)
            if resp and resp.get("status") == "scheduled":
                start = to_minute(datetime.fromisoformat(resp["start"]))
                end = to_minute(datetime.fromisoformat(resp["end"]))
            else:
                log.warning("job %s was not scheduled (%s); running it at release time", r.job_id, resp)
                start, end = int(r.release), int(r.release) + int(r.duration_min)
            self.running[(r.household, r.appliance)].append((start, end, float(r.kw)))
            self.last_end = max(self.last_end, end)

    def publish_telemetry(self, devices, minute):
        ts = from_minute(minute).isoformat()
        for house, device in devices:
            watts = sum(kw * 1000 for s, e, kw in self.running[(house, device)] if s <= minute < e)
            self.client.publish(topics.telemetry_topic("sim", house, device),
                                json.dumps({"ts": ts, "watts": round(watts, 1)}), qos=0)

    def run(self):
        raw = load_jobs(JOBS_CSV)
        jobs = (build_households(raw, N_HOUSEHOLDS, SEED)
                .sort_values("release", kind="stable").reset_index(drop=True))
        jobs["job_id"] = [f"{r.household}-{r.appliance}-{i:04d}" for i, r in enumerate(jobs.itertuples())]
        devices = sorted(set(zip(jobs["household"], jobs["appliance"])))
        by_minute = {int(m): g for m, g in jobs.groupby("release")}
        last_release = int(jobs["release"].max())
        log.info("run_id=%s: %d households, %d jobs, %d devices", RUN_ID, N_HOUSEHOLDS, len(jobs), len(devices))

        self.connect()
        self.handshake()

        minute = 0
        while True:
            if minute in by_minute:
                self.dispatch(by_minute[minute], minute)
            if minute % TELEMETRY_STEP_MIN == 0:
                self.publish_telemetry(devices, minute)
            if minute > last_release and minute >= self.last_end:
                break
            minute += 1
            if SIM_SPEED > 0:
                time.sleep(1 / SIM_SPEED)

        time.sleep(2)                       # let the last QoS 0 messages flush
        self.client.disconnect()
        self.client.loop_stop()
        log.info("done after %d simulated minutes (run_id=%s)", minute, RUN_ID)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    Simulator().run()