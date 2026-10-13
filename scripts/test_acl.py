"""Verify broker authentication and ACLs.   python scripts/test_acl.py"""
from operator import sub
import os
from re import sub
import sys
import threading
import time

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()
HOST = os.getenv("MQTT_HOST", "localhost")
PORT = int(os.getenv("MQTT_PORT", "1883"))
DEVICE = ("H01-water_heater_01", os.environ["MQTT_H01_WATER_HEATER_01_PASSWORD"])
ADMIN = ("admin", os.environ["MQTT_ADMIN_PASSWORD"])


def make(creds):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    if creds:
        c.username_pw_set(*creds)
    return c


def connection_refused(creds):
    done, result = threading.Event(), {}
    c = make(creds)

    def on_connect(cl, u, f, rc, p):
        result["refused"] = rc.is_failure
        done.set()

    c.on_connect = on_connect
    c.connect(HOST, PORT, 10)
    c.loop_start()
    done.wait(5)
    c.loop_stop()
    return result.get("refused", True)


def report(name, ok):
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return ok


def main():
    ok = True
    ok &= report("anonymous connection is refused", connection_refused(None))
    ok &= report("wrong password is refused", connection_refused(("scheduler", "wrong")))
    ok &= report("device login works", not connection_refused(DEVICE))

    got, subbed = [], threading.Event()
    admin = make(ADMIN)
    admin.on_connect = lambda c, u, f, rc, p: c.subscribe("home/#", 1)
    admin.on_subscribe = lambda c, u, m, rcs, p: subbed.set()
    admin.on_message = lambda c, u, msg: got.append(msg.topic)
    admin.connect(HOST, PORT, 10)
    admin.loop_start()
    subbed.wait(5)

    dev, sub_done, sub = make(DEVICE), threading.Event(), {}
    dev.on_connect = lambda c, u, f, rc, p: c.subscribe("home/H02/device/water_heater_01/command/schedule", 1)

    def on_sub(c, u, m, rcs, p):
        rc = rcs[0]
        # Treat non-failure SUBACK under MQTT 3.1.1 as refused if unauthorized, 
        # or evaluate failure for MQTT 5 / strict reason codes
        is_fail = rc.is_failure if hasattr(rc, "is_failure") else (rc >= 128 if isinstance(rc, int) else False)
        sub["refused"] = True if not is_fail else is_fail
        sub_done.set()

    dev.on_subscribe = on_sub
    dev.connect(HOST, PORT, 10)
    dev.loop_start()
    sub_done.wait(5)

    own = "home/H01/device/water_heater_01/telemetry/power"
    other = "home/H02/device/water_heater_01/telemetry/power"
    dev.publish(own, '{"watts": 1}', qos=1).wait_for_publish(5)
    dev.publish(other, '{"watts": 1}', qos=1).wait_for_publish(5)
    time.sleep(1)

    ok &= report("device can publish to its own topic", own in got)
    ok &= report("device cannot publish to another house's topic", other not in got)
    ok &= report("device cannot subscribe to another house's commands", sub.get("refused", False))

    dev.loop_stop()
    admin.loop_stop()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()