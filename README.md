# bme-tracker

Logs temperature, humidity and pressure from a BME680 or BME280 on a Raspberry
Pi (plus gas resistance and a relative air quality score on the BME680).
Serves a dashboard on your local network and can push every reading to
InfluxDB for Grafana. One process, one SQLite file, no Python dependencies
beyond the Adafruit sensor libraries.

## Wiring (I2C)

| Sensor      | Pi header           |
|-------------|---------------------|
| VIN / VCC   | 3V3 (pin 1)         |
| GND         | GND (pin 6)         |
| SCK / SCL   | SCL / GPIO3 (pin 5) |
| SDI / SDA   | SDA / GPIO2 (pin 3) |

## Install

On the Pi:

    sudo apt-get install -y git
    git clone https://github.com/nathanbent/bme-tracker
    cd bme-tracker
    bash install.sh

`install.sh` enables I2C, builds a virtualenv, creates `config.ini`, and sets
up a systemd service (`bme-tracker`) that starts on boot. The dashboard is at
`http://<pi-address>:8680/`.

To update later: `bash update.sh` (git pull + restart).

## Settings

Edit `config.ini`, then `sudo systemctl restart bme-tracker`. The file is
git-ignored, so tokens stay on the Pi. See `config.example.ini` for every
option. The ones you are most likely to want:

- `[tracker] name` - label for this Pi (default: hostname)
- `[tracker] temperature_unit` - `C` (default) or `F`; with `F` the dashboard
  defaults to Fahrenheit and InfluxDB gets a `temp_f` field instead of `temp_c`
- `[sensor] type` - `auto` (default), `bme680` or `bme280`; `address` likewise
  auto-detects 0x77 / 0x76
- `[sensor] temp_offset` - the sensors warm themselves and sit near the Pi, so
  they read high; compare with a thermometer and set the difference, e.g. `-2.0`

Try it without hardware: `python3 tracker.py --simulate --db /tmp/demo.db`

## InfluxDB and Grafana

Uncomment one `[influxdb]` block in `config.ini`:

- **InfluxDB 2.x / 3.x**: `url`, `org`, `bucket`, and a `token` with write
  access to that bucket
- **InfluxDB 1.x**: `url`, `database`, and `username` / `password` if auth is on

Each minute's row is written as:

    environment,location=<name>,sensor=bme680 temp_c=..,humidity=..,pressure_hpa=..,gas_ohms=..,aq_score=.. <time>

(a BME280 sends only the first three fields). The measurement name, the
`location` tag name and every field name can be changed to match data you
already have: see `measurement`, `location_tag` and `[influxdb_fields]` in
`config.example.ini`. Rows are pushed from the local
database, so anything logged while InfluxDB was down, or before you switched
this on, is delivered when the connection works. Check with
`journalctl -u bme-tracker -f`; you should see `influxdb: connected`.

In Grafana, use your existing InfluxDB data source. Example panel queries:

Flux (InfluxDB 2.x):

    from(bucket: "sensors")
      |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
      |> filter(fn: (r) => r._measurement == "environment" and r._field == "temp_c")
      |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)

InfluxQL (1.x):

    SELECT mean("temp_c") FROM "environment" WHERE $timeFilter GROUP BY time($__interval), "location" fill(null)

With several Pis, each one's `name` becomes the `location` tag, so one panel
shows a line per room.

## Local data

`readings.db` (SQLite), table `readings`, one row per minute (about 5 MB per
month). `/export.csv` downloads everything; `/api/latest` and
`/api/history?hours=24` return JSON.

## About "air quality" (BME680 only)

The BME680 measures gas *resistance*, which falls as VOCs rise. The score
(0-100, higher is better) compares the current resistance with this sensor's
own 90th-percentile level over the last 24 hours (75%) plus distance from 40%
humidity (25%). It shows relative change in your room; it is not a calibrated
IAQ index, and the sensor cannot measure CO2 or particulates. Give a new
sensor a day or two of running before trusting the trend.
