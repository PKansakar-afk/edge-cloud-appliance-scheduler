"""Single source of truth for MQTT topic names (the frozen contract)."""

CONTROL_RESET = "control/reset"
CONTROL_ACK = "control/ack"


def telemetry_topic(root, house, device):
    return f"{root}/{house}/device/{device}/telemetry/power"


def job_request_topic(root, house, device):
    return f"{root}/{house}/device/{device}/job/request"


def schedule_topic(root, house, device):
    return f"{root}/{house}/device/{device}/command/schedule"


def aggregate_topic(neighborhood):
    return f"neighborhood/{neighborhood}/aggregate/demand"


def parse_device_topic(topic):
    """'sim/S01/device/dishwasher/job/request' -> ('sim', 'S01', 'dishwasher', 'job/request')"""
    parts = topic.split("/")
    if len(parts) < 5 or parts[2] != "device":
        raise ValueError(f"unexpected topic: {topic}")
    return parts[0], parts[1], parts[3], "/".join(parts[4:])