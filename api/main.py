"""REST API and dashboard server. Runs in Docker:  docker compose up -d --build api"""
import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path as FsPath
from typing import Literal

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field

from api import data
from api.bridge import MqttBridge
from common.config import EPOCH

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("api")

ID_RE = r"^[A-Za-z0-9._-]{1,100}$"
API_KEY = os.environ.get("API_KEY", "")
SCHEDULE_TIMEOUT_S = float(os.getenv("SCHEDULE_TIMEOUT_S", "5"))
DEVICE_KW = {"water_heater_01": 1.0}      # kW per device; set to your demo appliance's measured draw
DEFAULT_KW = 1.0

reader = data.Reader(
    url=os.environ.get("INFLUXDB_URL", "http://localhost:8086"),
    token=os.environ["INFLUXDB_TOKEN"], org=os.environ["INFLUXDB_ORG"],
    bucket=os.environ["INFLUXDB_BUCKET"])
bridge = MqttBridge(os.getenv("MQTT_HOST", "localhost"), int(os.getenv("MQTT_PORT", "1883")),
                    os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))


@asynccontextmanager
async def lifespan(app):
    if not API_KEY:
        raise RuntimeError("API_KEY is not set (check .env and docker-compose.yml)")
    bridge.start()
    yield
    bridge.stop()


router = APIRouter(prefix="/api/v1")


def guard(fn, *args):
    """Run a database call; turn failures into a clean 502."""
    try:
        return fn(*args)
    except Exception:
        log.exception("InfluxDB query failed")
        raise HTTPException(502, "database query failed")


def require_key(x_api_key: str = Header(default="")):
    if not secrets.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(401, "invalid or missing API key")


class ScheduleRequest(BaseModel):
    house_id: str = Field(pattern=ID_RE)
    device_id: str = Field(pattern=ID_RE)
    requested_by: Literal["user", "scheduler"] = "user"
    deadline: AwareDatetime                          # must include a UTC offset, e.g. +07:00 or Z
    duration_minutes: int = Field(gt=0, le=1440)
    priority: Literal["normal", "urgent"] = "normal"
    release: AwareDatetime | None = None             # optional: defaults to now
    kw: float | None = Field(default=None, gt=0, le=10)
    source: Literal["home", "sim"] = "home"          # which MQTT tree to use


# ---------- read-only endpoints ----------
@router.get("/health")
def health():
    return {"mqtt": bridge.connected.is_set(), "influxdb": reader.ping()}


@router.get("/runs")
def runs():
    df = guard(reader.decisions)
    if df.empty:
        return []
    return sorted((data.run_metrics(g) for _, g in df.groupby("run_id")), key=lambda m: m["run_id"])


@router.get("/runs/{run_id}/load")
def run_load(run_id: str = Path(pattern=ID_RE), step: int = Query(15, ge=1, le=120)):
    df = guard(reader.decisions, run_id)
    if df.empty:
        raise HTTPException(404, "unknown run_id")
    return data.load_curves(df, step, EPOCH.tzinfo)


@router.get("/runs/{run_id}/decisions")
def run_decisions(run_id: str = Path(pattern=ID_RE), limit: int = Query(50, ge=1, le=500)):
    df = guard(reader.decisions, run_id)
    if df.empty:
        raise HTTPException(404, "unknown run_id")
    return data.decisions_table(df, limit)


@router.get("/devices")
def devices():
    return bridge.snapshot()


@router.get("/live/{house}/{device}")
def live(house: str = Path(pattern=ID_RE), device: str = Path(pattern=ID_RE),
         minutes: int = Query(30, ge=1, le=240)):
    return guard(reader.live_power, house, device, minutes)


# ---------- the one write endpoint (the contract from the paper) ----------
@router.post("/schedule", dependencies=[Depends(require_key)])
def post_schedule(req: ScheduleRequest):
    release = (req.release or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    duration = timedelta(minutes=req.duration_minutes)
    deadline = release + duration if req.priority == "urgent" else req.deadline   # urgent = no slack
    if deadline < release + duration:
        raise HTTPException(422, "deadline is too early for this duration")

    job_id = f"API-{uuid.uuid4().hex[:8]}"
    payload = {
        "job_id": job_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "release": release.isoformat(),
        "duration_min": req.duration_minutes,
        "kw": req.kw or DEVICE_KW.get(req.device_id, DEFAULT_KW),
        "deadline": deadline.isoformat(),
        "priority": req.priority,
    }
    log.info("schedule request %s from %s for %s/%s", job_id, req.requested_by, req.house_id, req.device_id)
    try:
        reply = bridge.request(req.source, req.house_id, req.device_id, payload, SCHEDULE_TIMEOUT_S)
    except ConnectionError as e:
        raise HTTPException(503, str(e))
    if reply is None:
        raise HTTPException(504, "no answer from the scheduler (is it running, and does the api ACL allow publishing?)")
    if reply.get("status") != "scheduled":
        raise HTTPException(422, reply.get("reason", "request rejected by the scheduler"))
    return {
        "schedule_id": reply["schedule_id"],
        "device_id": req.device_id,
        "scheduled_start": reply["start"],
        "scheduled_end": reply["end"],
        "estimated_cost": reply["estimated_cost"],
        "estimated_peak_contribution_kw": reply["estimated_peak_contribution_kw"],
    }


app = FastAPI(title="Edge-Cloud Appliance Scheduler API", version="1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=FsPath(__file__).resolve().parents[1] / "dashboard", html=True),
          name="dashboard")