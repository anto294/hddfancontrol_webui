# History, SMART and alerts

What the web UI records, how it reads SMART data without waking drives, when it
raises and sends an alert, and how it helps to ask for support of new hardware. Setup from a user's point of view is in the
[README](../README.md#alerts).

## History

`monitor.py` records into `/config/history.db` (SQLite in WAL mode, `history.py`):

- every new temperature sample, once per sample, each drive under a stable
  `/dev/disk/by-id` name (`wwn-` first, never `sdX`, which can change at a reboot);
- the PWM value and RPM of every output every `RECORD_PERIOD` (30 s).

The tables are `series(kind, name)` and `samples(series, ts, value)`. A query averages
into at most `HISTORY_MAX_POINTS` (600) buckets per series, so a chart of a year costs
the same as one of an hour. Every hour `purge` drops samples older than `history_days`
and empty series, and `forget_smart` drops the SMART data of drives no longer
monitored. A corrupt database is set aside and a new one started, so the start never
fails on it.

The same database keeps the last SMART summary per drive, the notes on alerts and a
small `meta` table (last summary sent, fan wear baselines, self-test times).

A `[temps]` line goes to the log when the temperatures change, and at least every
`TEMPS_HEARTBEAT` (10 min).

`GET /metrics` gives the same values in the Prometheus text format, behind the login.

## SMART

`smart.py` runs `smartctl -j -a -l scttemp -n standby` on each monitored drive
(`monitor_drives`, which can differ from the drives that drive the fans) at the runs
of `smart_schedule` (every 6 hours by default), and on demand from the **Drives** tab.

- `-n standby` never wakes a sleeping drive: it keeps its previous data, marked
  `asleep`.
- `summarize` keeps what the page shows and the attribute table; warnings are codes
  translated by the page (`smart.warn.<code>`).
- `-l scttemp` gives the temperature limits the drive itself recommends, shown as a
  hint, never applied by itself (some drives report limits well under their
  datasheet).
- `drivedb.py` names the brand, product line, use and recording (CMR/SMR) from model
  numbers. Only unambiguous patterns go there, never temperature limits.

**Self-tests.** Every minute the SMART loop starts the short or long tests that are due
(`selftest_short`, `selftest_long`, cron), with `smartctl -n standby -t short|long`, and
keeps the times in `meta` so a restart does not run them twice. A due test is tried for
`SELFTEST_GRACE` (3 h): a sleeping drive is tried again every minute, and a web UI
started late still runs it. No test starts on a drive whose long test began less than a
day ago (`LONG_TEST_GUARD`), since it would abort it.

## Alerts

`Monitor.conditions()` returns what is wrong now as `{key: (kind, params)}`.
`Alerts.update` compares it with what was wrong before: each start and end is logged
(`[alerts]`) and sent.

| Kind | When |
| --- | --- |
| `drive_hot` | A drive at its maximum, or at the 65/80 °C ceiling; it ends 2 °C lower |
| `fan_stalled` | 0 RPM for `FAN_STALL_SECONDS` (120 s) at a speed where the fan surely turns (halfway between stop and start) |
| `fan_wear` | At the same PWM, the RPM of a chosen fan over the last week dropped by `WEAR_DROP` (15 %) from its baseline (the first week of its history, taken once that history is two weeks old and then kept, so a slow drop over months still shows); checked daily |
| `smart_failing`, `smart_warning`, `smart_error` | The SMART verdict, worrying attributes, or SMART that cannot be read |
| `drive_missing` | A monitored drive gone from `/dev` |
| `fans_missing` | The chip of a chosen fan is gone (often a kernel update) |
| `safety` | A safety rule of the fan controller holds the fans at full speed, or the control loop failed |

**Taking note.** A user can take note of an alert (`Alerts.ack`). The note stores the
alert's parameters without the ones that move all the time (temperature, percent),
and SMART parameters carry the error counts: an alert taken note of shows again when it
gets worse. A note ends with its alert.

## Notifications

`notify.send` POSTs to `notify_url` in the ntfy, Gotify or plain JSON format, with a
10 s timeout and no redirects followed. Texts are the `alert.<kind>` keys of the page's
translations, in `notify_language`.

- `Alerts` queues the messages (at most `QUEUE_SIZE`, 100) for one sender task, so the
  monitor never waits for the notification server. A failed message is logged, never
  raised.
- **Reminders:** an alert still on is sent again every `notify_repeat_h` hours, unless
  someone took note of it.
- **Quiet hours** (`notify_quiet`, local time): only the urgent kinds (`drive_hot`,
  `safety`, `smart_failing`, `fans_missing`, `drive_missing`) go out; the rest is held
  and sent as one message when the quiet hours end.
- **Summary** (`notify_summary`): the drives and fans in one message. The time of the
  last one is kept in `meta`; it is never sent at the very first look.

## Schedules

Every schedule (`smart_schedule`, `selftest_short`, `selftest_long`, `notify_summary`,
`notify_quiet`) is a five-field cron expression in local time, empty for never.
`cron.py` parses them without a dependency (Alpine packages no `croniter`), raising
errors with a code and parameters like the other settings. `GET /api/cron?expr=` gives
the next runs as the server's clock sees them, for the preview under each field; the
**Compose** button of each field builds the expression from a small form.

## Hardware support request

Support for hardware we do not own (a fan chip, a server whose fans belong to its BMC)
can only be built from what its owner sends. The issue form
[`hardware_support.yml`](../.github/ISSUE_TEMPLATE/hardware_support.yml) walks them
through it: what is missing and the machine, one shell block to paste, `ipmitool` for
servers (in a throw-away privileged `alpine` container on TrueNAS SCALE and other
systems where nothing is installed, or from a live Linux), `smartctl` for drives
without a temperature.

The web UI fills the same form in by itself (**Fans → Hardware not supported?**, and a
button in the "No PWM output" banner):

- `Hardware.report()` prints the lines of the form's shell block: board and BIOS names
  (only the DMI fields named in `_REPORT_DMI`, never a `*_serial` or `product_uuid`),
  the kernel, every hwmon chip with its `pwm`, `fan` and `temp` files, the usual sensor
  modules, the drives (name, bus, model) and whether `/dev/ipmi0` is there.
- `GET /api/support` adds the system (Docker image or bare metal, version, kernel) and
  the last `SUPPORT_LOG_LINES` (30) log lines of `SUPPORT_SOURCES` (sensors,
  hddfancontrol, fan-control, webui; never the gateway's or the audit's, which name
  users and addresses). Addresses are masked anyway (`mask_addresses`).
- EXPERIMENTAL ([bmc.md](bmc.md)): with a BMC, `Bmc.report()` fills the form's
  `ipmitool` field (`bmc`): the BMC and its firmware, the server's product name (never
  a serial number), the model in use and the one found, `bmc.json`, who drives the
  fans now and why, and every sensor with its status and limit. It is not masked: it
  holds no address, and an iDRAC firmware (`2.83.83.83`) would look like one.
- The page shows it all first. **Open on GitHub** opens the form with its `system`,
  `host`, `bmc` and `logs` fields filled in by their ids; past `SUPPORT_MAX_URL` (7500
  characters) the link drops the oldest log lines, then the end of the host part, then
  the last BMC sensors, and the page says so. **Copy** copies the whole report.

Nothing leaves the host until the user opens the link.
