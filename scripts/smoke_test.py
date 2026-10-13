"""Smoke test for the Docker stack. From the repo root:
    python scripts/smoke_test.py
"""
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
INFLUX_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
TOKEN = os.environ["INFLUXDB_TOKEN"]
ORG = os.environ["INFLUXDB_ORG"]
BUCKET = os.environ["INFLUXDB_BUCKET"]


def test_mqtt():
    topic = "test/smoke"
    payload = f"hello-{time.time()}"
    received = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(topic, qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        client.publish(topic, payload, qos=1)      # publish only once subscribed

    def on_message(client, userdata, msg):
        if msg.payload.decode() == payload:
            received.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()
    ok = received.wait(timeout=5)
    client.loop_stop()
    client.disconnect()
    return ok


def test_influx():
    with InfluxDBClient(url=INFLUX_URL, token=TOKEN, org=ORG) as client:
        point = Point("smoke").tag("source", "test").field("watts", 123.0)
        client.write_api(write_options=SYNCHRONOUS).write(bucket=BUCKET, record=point)
        query = (f'from(bucket: "{BUCKET}") |> range(start: -5m) '
                 f'|> filter(fn: (r) => r._measurement == "smoke")')
        tables = client.query_api().query(query)
        return any(len(t.records) > 0 for t in tables)


if __name__ == "__main__":
    results = {}
    for name, fn in [("MQTT round trip", test_mqtt), ("InfluxDB write/read", test_influx)]:
        try:
            results[name] = fn()
        except Exception as e:
            print(f"{name}: ERROR - {e}")
            results[name] = False
        else:
            print(f"{name}: {'OK' if results[name] else 'FAILED'}")
    sys.exit(0 if all(results.values()) else 1)