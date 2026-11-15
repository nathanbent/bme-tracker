#!/usr/bin/env python3
"""BME680 / BME280 tracker for a Raspberry Pi.

Samples the sensor every few seconds, stores one averaged row per minute in
SQLite, serves a small dashboard on the local network, and optionally pushes
every row to InfluxDB.

    python3 tracker.py                 # real sensor on I2C, settings from config.ini
    python3 tracker.py --simulate      # no hardware: fake data, for trying it out
"""
import argparse
import base64
import configparser
import json
import math
import random
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

HERE = Path(__file__).resolve().parent
FIELDS = ("temp_c", "humidity", "pressure_hpa", "gas_ohms", "aq_score")
MAX_POINTS = 600          # points per chart returned by /api/history
IDEAL_HUMIDITY = 40.0     # %RH treated as "best" by the air quality score
MIN_BASELINE_SAMPLES = 30  # minutes of data before a score is reported
INFLUX_BATCH = 5000       # rows per write when catching up


# ---------------------------------------------------------------- sensors

class RealSensor:
    """A BME680 or BME280 on I2C. read() -> (temp C, %RH, hPa, gas ohms|None)."""

    def __init__(self, kind="auto", address="auto", temp_offset=0.0):
        import board
        i2c = board.I2C()
        kinds = ("bme680", "bme280") if kind == "auto" else (kind,)
        addresses = (0x77, 0x76) if address == "auto" else (int(str(address), 0),)
        errors = []
        for addr in addresses:
            for k in kinds:
                try:
                    if k == "bme680":
                        import adafruit_bme680
                        self.dev = adafruit_bme680.Adafruit_BME680_I2C(i2c, address=addr)
                    elif k == "bme280":
                        from adafruit_bme280 import basic as adafruit_bme280
                        self.dev = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
                    else:
                        raise SystemExit(f"unknown sensor type {k!r} (use auto, bme680 or bme280)")
                    self.kind, self.address = k, addr
                    self.temp_offset = temp_offset
                    return
                except (RuntimeError, ValueError, OSError) as exc:
                    errors.append(f"  {k} at {addr:#x}: {exc}")
        raise SystemExit("No sensor found. Check wiring and `i2cdetect -y 1`.\n"
                         + "\n".join(errors + raw_probe(addresses)))

    def read(self):
        d = self.dev
        gas = float(d.gas) if self.kind == "bme680" else None
        return (d.temperature + self.temp_offset, d.relative_humidity,
                d.pressure, gas)


CHIP_IDS = {0x61: "BME680", 0x60: "BME280",
            0x58: "BMP280 - no humidity sensor, not supported"}


def raw_probe(addresses, bus_num=1):
    """Read the chip ID register directly, to show the real error when the
    Adafruit drivers only say 'no device'."""
    try:
        from Adafruit_PureIO.smbus import SMBus
        bus = SMBus(bus_num)
    except Exception as exc:
        return [f"  cannot open /dev/i2c-{bus_num}: {exc!r}"]
    out = []
    for addr in addresses:
        try:
            chip = bus.read_byte_data(addr, 0xD0)
            out.append(f"  raw read at {addr:#x}: chip ID {chip:#x} "
                       f"({CHIP_IDS.get(chip, 'unknown chip')})")
        except Exception as exc:
            out.append(f"  raw read at {addr:#x}: {exc!r}")
    return out


class FakeSensor:
    """Plausible-looking readings for running without hardware."""

    def __init__(self, kind="bme680"):
        self.kind = "bme680" if kind == "auto" else kind

    def read(self, now=None):
        t = now if now is not None else time.time()
        day = 2 * math.pi * (t % 86400) / 86400
        temp = 21 + 2.5 * math.sin(day - 2.2) + random.gauss(0, 0.05)
        hum = 45 - 8 * math.sin(day - 2.2) + random.gauss(0, 0.3)
        pres = 1013 + 6 * math.sin(t / 200000) + random.gauss(0, 0.05)
        if self.kind != "bme680":
            return temp, hum, pres, None
        # an "event" (cooking, say) every ~7 hours knocks gas resistance down
        dip = max(0.0, 1 - ((t % 25000) / 2500)) ** 2
        gas = 180000 * (1 - 0.7 * dip) * (1 + random.gauss(0, 0.01))
        return temp, hum, pres, gas


# ---------------------------------------------------------------- storage

def connect(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def init_db(con):
    con.execute("""CREATE TABLE IF NOT EXISTS readings (
        ts INTEGER PRIMARY KEY,   -- unix seconds, start of the minute
        temp_c REAL, humidity REAL, pressure_hpa REAL,
        gas_ohms REAL, aq_score REAL)""")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()


def gas_baseline(con, now):
    """'Clean air' reference: 90th percentile of gas resistance, last 24 h."""
    rows = [r[0] for r in con.execute(
        "SELECT gas_ohms FROM readings WHERE ts > ? AND gas_ohms IS NOT NULL "
        "ORDER BY gas_ohms", (now - 86400,))]
    if len(rows) < MIN_BASELINE_SAMPLES:
        return None
    return rows[int(0.9 * (len(rows) - 1))]


def air_quality_score(gas, humidity, baseline):
    """0-100, higher is better. 75% from gas resistance relative to the
    recent clean-air baseline, 25% from distance to ideal humidity.
    This is a relative indicator, not a calibrated IAQ index."""
    if baseline is None or gas is None:
        return None
    gas_part = 75.0 * min(1.0, gas / baseline)
    if humidity >= IDEAL_HUMIDITY:
        hum_part = 25.0 * (100 - humidity) / (100 - IDEAL_HUMIDITY)
    else:
        hum_part = 25.0 * humidity / IDEAL_HUMIDITY
    return round(gas_part + max(0.0, hum_part), 1)


def rnd(v, nd=2):
    return None if v is None else round(v, nd)


def seed_demo_history(con, sensor, hours=72):
    now = int(time.time()) // 60 * 60
    rows = [(ts, *sensor.read(ts)) for ts in range(now - hours * 3600, now, 60)]
    gases = sorted(r[4] for r in rows if r[4] is not None)
    base = gases[int(0.9 * (len(gases) - 1))] if gases else None
    con.executemany(
        "INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?)",
        [(ts, rnd(t), rnd(h), rnd(p), rnd(g, 0), air_quality_score(g, h, base))
         for ts, t, h, p, g in rows])
    con.commit()


# ---------------------------------------------------------------- sampler

def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def sampler(db_path, sensor, sample_every, stop):
    con = connect(db_path)
    baseline, baseline_at = None, 0
    bucket, samples, failures = None, [], 0
    while not stop.is_set():
        now = time.time()
        minute = int(now) // 60 * 60
        if bucket is not None and minute != bucket and samples:
            t, h, p, g = (mean(col) for col in zip(*samples))
            if g is not None and now - baseline_at > 600:
                baseline, baseline_at = gas_baseline(con, now), now
            con.execute(
                "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)",
                (bucket, rnd(t), rnd(h), rnd(p), rnd(g, 0),
                 air_quality_score(g, h, baseline)))
            con.commit()
            samples = []
        bucket = minute
        try:
            samples.append(sensor.read())
            failures = 0
        except Exception as exc:  # I2C glitches happen; keep going
            failures += 1
            if failures in (1, 10) or failures % 100 == 0:
                print(f"sensor read failed ({failures}x): {exc!r}", flush=True)
        stop.wait(sample_every)


# ---------------------------------------------------------------- influxdb

def lp_escape(s):
    return str(s).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


class Influx:
    """Pushes rows to InfluxDB over HTTP (line protocol, no client library).

    Remembers the last row it delivered, so rows logged while InfluxDB was
    unreachable - or before this was switched on - are sent when it is back.
    """

    def __init__(self, cfg, name, sensor_kind):
        self.measurement = lp_escape(cfg.get("measurement", "environment"))
        self.tags = f"location={lp_escape(name)},sensor={lp_escape(sensor_kind)}"
        url = cfg["url"].rstrip("/")
        self.headers = {"Content-Type": "text/plain; charset=utf-8"}
        if cfg.get("bucket"):                     # InfluxDB 2.x / 3.x
            q = {"bucket": cfg["bucket"], "precision": "s"}
            if cfg.get("org"):
                q["org"] = cfg["org"]
            self.url = f"{url}/api/v2/write?{urlencode(q)}"
            if cfg.get("token"):
                self.headers["Authorization"] = "Token " + cfg["token"]
        elif cfg.get("database"):                 # InfluxDB 1.x
            self.url = f"{url}/write?{urlencode({'db': cfg['database'], 'precision': 's'})}"
            if cfg.get("username"):
                cred = f"{cfg['username']}:{cfg.get('password', '')}".encode()
                self.headers["Authorization"] = "Basic " + base64.b64encode(cred).decode()
        else:
            raise SystemExit("[influxdb] needs `bucket` (v2/v3) or `database` (v1)")
        self.key = "influx_last_ts:" + self.url   # resend everything if the target changes

    def line(self, row):
        fields = ",".join(f"{f}={v}" for f, v in zip(FIELDS, row[1:]) if v is not None)
        return f"{self.measurement},{self.tags} {fields} {row[0]}" if fields else None

    def push_pending(self, con):
        """Send rows not yet delivered. Returns the number of rows sent."""
        sent = 0
        while True:
            last = con.execute("SELECT value FROM meta WHERE key=?", (self.key,)).fetchone()
            rows = con.execute(
                "SELECT ts, " + ",".join(FIELDS) + " FROM readings WHERE ts > ? "
                "ORDER BY ts LIMIT ?", (int(last[0]) if last else 0, INFLUX_BATCH)).fetchall()
            if not rows:
                return sent
            body = "\n".join(filter(None, map(self.line, rows))).encode()
            req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
            with urllib.request.urlopen(req, timeout=20):
                pass
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (self.key, str(rows[-1][0])))
            con.commit()
            sent += len(rows)

    def run(self, db_path, stop):
        con = connect(db_path)
        ok = None
        while not stop.is_set():
            try:
                n = self.push_pending(con)
                if ok is not True or n > 1:
                    print(f"influxdb: connected, sent {n} row(s)", flush=True)
                ok = True
            except urllib.error.HTTPError as exc:
                if ok is not False:
                    detail = exc.read(300).decode(errors="replace").strip()
                    print(f"influxdb: write rejected, HTTP {exc.code}: {detail}", flush=True)
                ok = False
            except Exception as exc:
                if ok is not False:
                    print(f"influxdb: unreachable, will keep retrying: {exc!r}", flush=True)
                ok = False
            stop.wait(15)


# ---------------------------------------------------------------- web

def make_handler(db_path, info):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, ctype, status=200, extra=()):
            if isinstance(body, str):
                body = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                hours = min(24 * 366, max(1, float(q.get("hours", ["24"])[0])))
            except ValueError:
                hours = 24
            if url.path == "/":
                self.send((HERE / "dashboard.html").read_bytes(),
                          "text/html; charset=utf-8")
            elif url.path == "/api/latest":
                con = connect(db_path)
                row = con.execute(
                    "SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
                con.close()
                data = dict(zip(("ts",) + FIELDS, row)) if row else {}
                self.send(json.dumps({**data, **info}), "application/json")
            elif url.path == "/api/history":
                now = int(time.time())
                step = max(60, int(hours * 3600 / MAX_POINTS) // 60 * 60)
                con = connect(db_path)
                rows = con.execute(
                    f"SELECT (ts / {step}) * {step} AS b, "
                    + ", ".join(f"AVG({f})" for f in FIELDS)
                    + " FROM readings WHERE ts >= ? GROUP BY b ORDER BY b",
                    (now - int(hours * 3600),)).fetchall()
                con.close()
                out = {"step": step, "from": now - int(hours * 3600), "to": now,
                       "ts": [r[0] for r in rows]}
                for i, f in enumerate(FIELDS, 1):
                    out[f] = [None if r[i] is None else round(r[i], 2) for r in rows]
                self.send(json.dumps(out), "application/json")
            elif url.path == "/export.csv":
                con = connect(db_path)
                lines = ["time_utc," + ",".join(FIELDS)]
                for r in con.execute(
                        "SELECT datetime(ts,'unixepoch'), "
                        + ",".join(FIELDS) + " FROM readings ORDER BY ts"):
                    lines.append(",".join("" if v is None else str(v) for v in r))
                con.close()
                self.send("\n".join(lines) + "\n", "text/csv", extra=[
                    ("Content-Disposition", 'attachment; filename="readings.csv"')])
            else:
                self.send("not found", "text/plain", 404)
    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(HERE / "config.ini"),
                    help="settings file (default: config.ini next to this script)")
    ap.add_argument("--simulate", action="store_true",
                    help="fake sensor + 3 days of demo history, no hardware needed")
    ap.add_argument("--probe", action="store_true",
                    help="look for a sensor on the I2C bus, print what is found, and exit")
    ap.add_argument("--db", help="override the database path")
    ap.add_argument("--port", type=int, help="override the dashboard port")
    args = ap.parse_args()

    if args.probe:
        print("\n".join(raw_probe((0x76, 0x77))))
        return

    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(args.config)
    tracker = cfg["tracker"] if cfg.has_section("tracker") else {}
    sens = cfg["sensor"] if cfg.has_section("sensor") else {}
    name = tracker.get("name") or socket.gethostname()
    db_path = args.db or tracker.get("db") or str(HERE / "readings.db")
    port = args.port or int(tracker.get("port", 8680))
    kind = sens.get("type", "auto").lower()

    con = connect(db_path)
    init_db(con)
    if args.simulate:
        sensor = FakeSensor(kind)
        if con.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0:
            seed_demo_history(con, sensor)
    else:
        sensor = RealSensor(kind, sens.get("address", "auto").lower(),
                            float(sens.get("temp_offset", 0)))
        print(f"sensor: {sensor.kind} at {sensor.address:#x}", flush=True)
    con.close()

    stop = threading.Event()
    threading.Thread(target=sampler, daemon=True, args=(
        db_path, sensor, float(sens.get("sample_every", 5)), stop)).start()
    if cfg.has_section("influxdb") and cfg["influxdb"].get("url"):
        influx = Influx(cfg["influxdb"], name, sensor.kind)
        threading.Thread(target=influx.run, daemon=True, args=(db_path, stop)).start()
    info = {"name": name, "sensor": sensor.kind.upper()}
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(db_path, info))
    print(f"dashboard on http://0.0.0.0:{port}/  db: {db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
