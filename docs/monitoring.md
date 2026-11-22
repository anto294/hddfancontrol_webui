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
monitored; while `settings.json` is broken (the environment stands in) neither runs,
and the log says so once. A corrupt database is set aside and a new one started, so the start never
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

On demand, `POST /api/smart` answers 202 at once and the reading goes on in the
background (`Monitor.refresh_smart_soon`; a reading under way is not started twice).
The drives are read one after the other and each may take `SMARTCTL_TIMEOUT`
(120 s), longer than the gateway waits for the core (`CORE_TIMEOUT_S`, 120 s). The
status carries `smart_refreshing` (also during a scheduled reading); the page keeps
the button off meanwhile and reads `GET /api/smart` when it is over.

- `-n standby` never wakes a sleeping drive: it keeps its previous data, marked
  `asleep`.
- `summarize` keeps what the page shows and the attribute table; warnings are codes
  translated by the page (`smart.warn.<code>`).
- `-l scttemp` gives the temperature limits the drive itself recommends, shown as a
  hint, never applied by itself (some drives report limits well under their
  datasheet).
- `drivedb.py` names the brand, product line, use and recording (CMR/SMR) from model
  numbers. Only unambiguous patterns go there, never temperature limits.

**Trend.** Each fresh reading (not asleep, not an error) keeps its counters
(`TREND_KEYS` of `smart.py`: reallocated, pending and uncorrectable sectors,
reallocation events, CRC errors, spin retries, NVMe media errors, wear, spare and
power-on hours) in the `smart_history` table of `history.db`, one reading per drive per
UTC day (the newest of the day wins). `history_days` does not purge it: a counter
growing over months is the point, and it costs a few rows a day. It goes with the
drive: `forget_smart` (neither monitored nor archived) and `forget_drive`.
`GET /api/smart/history?device=` gives it to the **Trend** section of the card's
details, which draws the counters that were ever above 0 and the wear with the chart
of the History tab.

**Risk and remaining life** (`health.py`, `assess`, added to each drive of
`GET /api/smart` as `health`). An estimate, and the page says so: the card itself,
not only its details, says that the life left is a statistical estimate that may not
match reality (`health.estimate`):

- **Risk per counter**, from Backblaze's data: the raw count of reallocated sectors (5),
  spin retries (10), reported uncorrectable (187), reallocation events (196), pending
  (197) and offline uncorrectable (198) sectors, and CRC errors (199, not critical) is
  looked up in the annual failure rates Backblaze observed for drives with that count
  (the table as tabulated by the Scrutiny project, MIT License, rounded to four
  digits). A critical counter at
  `AFR_FAILED` (10 %) or more fails; CRC errors warn there and fail at
  `AFR_FAILED_OTHER` (20 %). A count past the last bucket takes the highest rate it
  passed.
- **Risk of the drive**: high when SMART fails or a counter fails; medium when a
  counter warns, the worst rate is `AFR_MEDIUM` (5 %) or more, a critical counter grew
  in the last `GROWTH_DAYS` (30) of the trend, or an NVMe drive has a warning; low
  otherwise; unknown without any counter.
- **Life left**: high risk is "replace soon". SSD/NVMe: the wear left
  (`percentage_used`) at the pace of the drive's own hours, and of its trend when it
  covers `WEAR_PACE_DAYS` and moved; the two give the range. Hard drives (a rotation
  rate) have no wear counter: `HDD_LIFE_YEARS` (5–7 years of power-on time) less their
  age, halved at medium risk. A SATA SSD gets a risk but no life (its wear is in vendor
  attributes).

**Self-tests.** Every minute the SMART loop starts the short or long tests that are due
(`selftest_short`, `selftest_long`, cron), with `smartctl -n standby -t short|long`, and
keeps the times in `meta` so a restart does not run them twice. A due test is tried for
`SELFTEST_GRACE` (3 h): a sleeping drive is tried again every minute, and a web UI
started late still runs it. No test starts on a drive whose long test began less than a
day ago (`LONG_TEST_GUARD`), since it would abort it.

**Drives that are gone.** A monitored drive no longer in `/dev` shows on its card as gone
(`GET /api/smart` marks it `missing`, by the same rule as the `drive_missing` alert:
without any drive listed, `/dev` is not readable and says nothing). The card offers:

- **Archive** (`POST /api/drives/archive`): the drive leaves `monitor_drives` (no
  SMART read, no self-test, no alert) and is marked in the `archived` table of
  `history.db`. Its last SMART data and its history are kept: the hourly `tidy` keeps
  the SMART data of every drive monitored or archived. The **Archived drives** section
  of the tab lists them (`GET /api/drives/archived`, with `present` when the drive is
  back) and can monitor one again (`POST /api/drives/restore`, checked by `from_dict`).
  A drive the settings monitor again (Settings → Save, or a reset to the environment)
  loses its mark too (`_unarchive_monitored`), so it is never monitored and archived
  at once; restoring one that is monitored already only takes the mark off.
- **Delete** (`POST /api/drives/delete`, the button asks twice), for a gone monitored
  drive or an archived one: everything it left goes, its SMART data, its series under
  every name it had, the notes on its alerts, its self-test times, its labels and its
  bay.

A drive that drives the fans (under any of its names) is refused (`drive_controlled`):
it leaves fan control in the settings first. A drive still on the host is refused too
(`drive_present`). Changing `monitor_drives` this way saves the settings and restarts
nothing (`Supervisor.set_monitor_drives`): only the monitor reads it. While
`settings.json` is broken it is refused (409 `settings_broken`): the settings in force
are then the environment's, and saving them would overwrite the file.

The history keeps a drive under one of its by-id names (`wwn-` first), maybe not the one
of the settings, and once the drive is gone `/dev` no longer says which they were: the
hourly `tidy` keeps the names of each monitored drive that is here in `meta`
(`drive_ids:<path>`, `Monitor.remember_ids`), and deleting uses them.

## Alerts

`Monitor.conditions()` returns what is wrong now as `{key: (kind, params)}`.
`Alerts.update` compares it with what was wrong before: each start and end is logged
(`[alerts]`) and sent.

| Kind | When |
| --- | --- |
| `drive_hot` | A drive at its maximum, or at the 65/80 °C ceiling; it ends 2 °C lower. A null reading keeps the last one for `NULL_HOLD_INTERVALS` intervals (`DriveTemps`, as for the fans), so a failed probe does not end it |
| `fan_stalled` | 0 RPM for `FAN_STALL_SECONDS` (120 s) at a speed where the fan surely turns (halfway between stop and start) |
| `fan_wear` | At the same PWM, the RPM of a chosen fan over the last week dropped by `WEAR_DROP` (15 %) from its baseline (the first week of its history, taken once that history is two weeks old (`WEAR_BASELINE_AGE`) and then kept, so a slow drop over months still shows); checked daily. With `history_days` under 14 the purge never lets a history get that old, so the check never starts: the settings page says so under the field |
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
translations, in `notify_language`. The log names the server by host and port only
(`_where`): the user, password, path and query of the address (a Gotify token, an ntfy
topic) never reach it, nor an error's copy of the address (`_without_query`).

A message never goes to the NAS itself or the cloud's metadata service: `forbidden_ip`
refuses loopback, link-local (`169.254.0.0/16`, `fe80::/10`), unspecified
(`0.0.0.0/8`, `::`) and multicast addresses, and IPv4 addresses mapped on them.
`settings.from_dict` refuses such a host written as an IP (or `localhost`) at once
(`notify_address_forbidden`). At each send, a literal is checked again, and a name is
resolved by `_GuardedResolver`, the resolver of the connection itself, which drops the
forbidden addresses: the check and the connection use the same DNS answer, so DNS
rebinding cannot slip past it. A send refused this way raises `NotifyError` with the
code `NOTIFY_FORBIDDEN`, and `POST /api/notify/test` answers 400 with it. Private
networks (`10/8`, `192.168/16`, `fc00::/7`…) stay allowed.

- `Alerts` queues the messages (at most `QUEUE_SIZE`, 100) for one sender task, so the
  monitor never waits for the notification server. A failed message is logged, never
  raised.
- **Reminders:** an alert still on is sent again every `notify_repeat_h` hours, unless
  someone took note of it.
- **Quiet hours** (`notify_quiet`, local time): only the urgent kinds (`drive_hot`,
  `safety`, `smart_failing`, `fans_missing`, `drive_missing`) go out; the rest is held
  and sent as one message when the quiet hours end.
- **Summary** (`notify_summary`): the drives and fans in one message. The time of the
  last one is kept in `meta`; it is never sent at the very first look. It counts the
  alerts started in its period from the starts kept in `meta` (`RAISED_KEEP_S`, 32
  days), at most the newest `RAISED_MAX` (1000): an alert that comes and goes would
  otherwise grow that list, rewritten at every start, by over a megabyte a month.

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
  users and addresses). Addresses are masked anyway (`mask_addresses`), and so are the
  `/dev/disk/by-id` names of the drives, which hold their serial numbers (the
  `hddfancontrol` command line, the own limits of the fan control): each becomes its
  kernel name (`sda`), or `<drive>` when it is not on the host (`mask_drives`).
- The page shows it all first. **Open on GitHub** opens the form with its `system`,
  `host` and `logs` fields filled in by their ids; past `SUPPORT_MAX_URL` (7500
  characters) the link drops the oldest log lines, then the end of the host part, and
  the page says so. **Copy** copies the whole report.

Nothing leaves the host until the user opens the link.
