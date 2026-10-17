"""Create broker/passwd from .env, generating any missing passwords.
Run from the repo root:  python scripts/make_mqtt_users.py
Needs Docker running (it uses the mosquitto image only to hash the passwords).
"""
import secrets
import subprocess
from pathlib import Path

from dotenv import dotenv_values

ENV = Path(".env")
USERS = {                      # broker username -> .env variable holding its password
    "admin": "MQTT_ADMIN_PASSWORD",
    "scheduler": "MQTT_SCHEDULER_PASSWORD",
    "simulator": "MQTT_SIMULATOR_PASSWORD",
    "H01-water_heater_01": "MQTT_H01_WATER_HEATER_01_PASSWORD",
}


def main():
    env = dotenv_values(ENV)
    missing = [v for v in USERS.values() if not env.get(v)]
    if missing:
        text = ENV.read_text() if ENV.exists() else ""
        # drop empty placeholder lines like MQTT_ADMIN_PASSWORD= before appending real ones
        kept = [ln for ln in text.splitlines() if ln.split("=")[0].strip() not in missing]
        for var in missing:
            env[var] = secrets.token_hex(12)
            kept.append(f"{var}={env[var]}")
        ENV.write_text("\n".join(kept) + "\n")
        print("Generated passwords for:", ", ".join(missing))

    lines = []
    for user, var in USERS.items():
        cmd = f"mosquitto_passwd -c -b /tmp/p {user} {env[var]} >/dev/null 2>&1; cat /tmp/p"
        out = subprocess.run(["docker", "run", "--rm", "eclipse-mosquitto:2", "sh", "-c", cmd],
                             capture_output=True, text=True, check=True).stdout.strip()
        if not out.startswith(f"{user}:"):
            raise SystemExit(f"Could not hash the password for {user}")
        lines.append(out)

    Path("broker/passwd").write_text("\n".join(lines) + "\n", newline="\n")   # LF endings required
    print("Wrote broker/passwd with users:", ", ".join(USERS))


if __name__ == "__main__":
    main()