"""MQTT scheduler service.   Run:  python -m scheduler.service"""
import json
import logging
import os
import signal
from datetime import datetime

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision

from common import topics
from common.config import from_minute, to_minute
from scheduler.strategies import OnlineScheduler

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
STRATEGY = os.getenv("STRATEGY", "dynamic")
NEIGHBORHOOD = os.getenv("NEIGHBORHOOD_ID", "N01")
INFLUX_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUX_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUX_ORG = os.environ["INFLUXDB_ORG"]
INFLUX_BUCKET = os.environ["INFLUXDB_BUCKET"]

log = logging.getLogger("scheduler")


class SchedulerService:
    def __init__(self):
        self.sched = OnlineScheduler(STRATEGY)
        self.run_id = "default"
        self.count = 0

        self.influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        self.write = self.influx.write_api()          # batching writer

        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="scheduler")
        if MQTT_USERNAME:
            self.mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        self.mqtt.reconnect_delay_set(1, 30)

    # ---------- MQTT callbacks ----------
    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to broker, strategy=%s", self.sched.strategy)
        client.subscribe([
            ("sim/+/device/+/job/request", 1),
            ("home/+/device/+/job/request", 1),
            ("sim/+/device/+/telemetry/power", 0),
            ("home/+/device/+/telemetry/power", 1),
            (topics.CONTROL_RESET, 1),
        ])

    def on_message(self, client, userdata, msg):
        try:
            if msg.topic == topics.CONTROL_RESET:
                self.reset(json.loads(msg.payload or b"{}"))
                return
            root, house, device, kind = topics.parse_device_topic(msg.topic)
            data = json.loads(msg.payload)
            if kind == "job/request":
                self.handle_job(root, house, device, data)
            elif kind == "telemetry/power":
                self.handle_telemetry(root, house, device, data)
        except Exception:
            log.exception("could not process message on %s", msg.topic)

    # ---------- handlers ----------
    def reset(self, data):
        strategy = data.get("strategy", self.sched.strategy)
        self.sched = OnlineScheduler(strategy)
        self.run_id = data.get("run_id", "default")
        self.count = 0
        log.info("reset: strategy=%s run_id=%s", strategy, self.run_id)
        self.mqtt.publish(topics.CONTROL_ACK,
                          json.dumps({"event": "reset", "run_id": self.run_id, "strategy": strategy}),
                          qos=1)

    def handle_job(self, root, house, device, d):
        job_id = str(d.get("job_id", ""))
        try:
            release = to_minute(datetime.fromisoformat(d["release"]))
            deadline = to_minute(datetime.fromisoformat(d["deadline"]))
            duration, kw = int(d["duration_min"]), float(d["kw"])
            if not (duration > 0 and kw > 0) or deadline < release + duration:
                raise ValueError("duration and kw must be positive and the deadline must fit the job")
            start, cost = self.sched.place(release, duration, kw, deadline)
        except (KeyError, ValueError, TypeError) as e:
            log.warning("rejected job %s: %s", job_id, e)
            self.reply(root, house, device, {"job_id": job_id, "status": "rejected", "reason": str(e)})
            return

        self.count += 1
        schedule_id = f"S{self.count:05d}"
        peak_window = float(self.sched.load[start:start + duration].max())
        peak_total = float(self.sched.load.max())
        release_dt = from_minute(release)

        self.reply(root, house, device, {
            "job_id": job_id, "schedule_id": schedule_id, "status": "scheduled",
            "start": from_minute(start).isoformat(),
            "end": from_minute(start + duration).isoformat(),
            "estimated_cost": round(cost, 4),
            "estimated_peak_contribution_kw": round(kw, 3),
            "planned_aggregate_peak_kw": round(peak_window, 3),
        })
        self.mqtt.publish(topics.aggregate_topic(NEIGHBORHOOD), json.dumps({
            "ts": release_dt.isoformat(), "planned_peak_kw": round(peak_total, 3),
            "jobs_scheduled": self.count, "strategy": self.sched.strategy}), qos=0)

        tags = dict(run_id=self.run_id, strategy=self.sched.strategy)
        decision = Point("schedule_decision").tag("root", root).tag("house", house).tag("device", device)
        neighborhood = Point("neighborhood").tag("neighborhood", NEIGHBORHOOD)
        for p in (decision, neighborhood):
            for k, v in tags.items():
                p.tag(k, v)
        decision.field("job_id", job_id).field("schedule_id", schedule_id) \
            .field("delay_min", int(start - release)).field("duration_min", duration) \
            .field("kw", kw).field("cost", cost) \
            .field("deadline_met", int(start + duration <= deadline)) \
            .time(release_dt, WritePrecision.S)
        neighborhood.field("planned_peak_kw", peak_total).field("jobs_scheduled", self.count) \
            .time(release_dt, WritePrecision.S)
        self.write.write(bucket=INFLUX_BUCKET, record=[decision, neighborhood])

    def handle_telemetry(self, root, house, device, d):
        p = (Point("power").tag("root", root).tag("house", house).tag("device", device)
             .tag("run_id", self.run_id)
             .field("watts", float(d["watts"]))
             .time(datetime.fromisoformat(d["ts"]), WritePrecision.S))
        self.write.write(bucket=INFLUX_BUCKET, record=p)

    def reply(self, root, house, device, payload):
        # Only the physical node gets retained schedules (last-known-good on reconnect)
        self.mqtt.publish(topics.schedule_topic(root, house, device), json.dumps(payload),
                          qos=1, retain=(root == "home"))

    # ---------- lifecycle ----------
    def run(self):
        self.mqtt.connect_async(MQTT_HOST, MQTT_PORT, 60)
        self.mqtt.loop_forever()

    def close(self):
        self.write.close()
        self.influx.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    svc = SchedulerService()
    signal.signal(signal.SIGTERM, lambda *_: svc.mqtt.disconnect())
    try:
        svc.run()
    finally:
        svc.close()


if __name__ == "__main__":
    main()