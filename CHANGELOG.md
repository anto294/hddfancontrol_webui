# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **The web side can no longer reach the terminal it was started from.**
  When the web UI was started by hand from a root shell, its unprivileged
  web side shared that terminal. It now gets no input and a session of
  its own.
- **The notification token is no longer shown.** The settings returned the
  notification address in full, with its Gotify token or ntfy topic, to
  the page and in the settings backup. It is now shown with those parts
  hidden (`•••`); saving without touching the field, or importing a
  backup, keeps the saved address, and typing a new one replaces it.
- **Notifications can no longer be aimed at the NAS itself.** The
  notification address accepted `localhost`, `127.0.0.1` or the cloud's
  metadata address `169.254.169.254`, and **Send a test message** told
  whether something answered there: a way to probe services that only
  the NAS can reach. Such addresses are now refused, also when a name
  leads to them. Servers on your LAN are still fine; for one running on
  the NAS itself, use its LAN address.
- **The password of a notification address no longer reaches the log.**
  An address such as `https://user:password@host/` was logged whole when
  a message failed. Only the host and port are logged now.
- **Pages of other sites can no longer reach the API, even to read.**
  Another web app on the same NAS (another port is the same "site" for
  cookies) could keep manual control from ending by asking for the
  status in the background. The browser marks such requests, and the
  web UI now refuses them.
- **The hardware support request no longer carries serial numbers.** Its
  log lines named the drives by their `/dev/disk/by-id` names, which hold
  the serial number, although the form promises none. They now show the
  kernel name (`sda`) instead.
- **Changing the password always ends a stolen session.** The sessions
  were cleared in the background thread that saves the new password,
  while the web server kept using them: a session used at that very
  moment could survive the change. They are now ended once the change is
  saved, in step with the requests.
- **Connections from outside the allowed networks are closed at once.**
  Addresses outside `WEBUI_ALLOW` were refused only after their request
  was read, and an incomplete request was waited for without end: a
  client could hold thousands of silent connections until the web UI
  answered nobody, its health check included. Now such an address is
  closed before anything is read, each address may keep 64 connections
  open (512 in all), and a request must arrive complete within 30
  seconds. The fans were never affected.
- **The bare metal tarball is built only from pinned tools.** The release
  workflow installed its build tools at their latest version without
  checking them; they now come at an exact version with their checksums,
  and the token that can change a release is given only to the step that
  uploads the files.
- **A `/config` the gateway could write no longer stays in use.** When the
  folder could not be closed to the web UI's own user (an ACL, NFS with
  root squash), the web side only moved its login data elsewhere, while
  the root side kept its files there. A web side taken over could then
  make root write any file through the saved fan state. Now the web UI
  does not start at all in that case (the fans are still controlled, and
  the log says why); a folder with the sticky bit (mode 1777) is closed
  too; and the saved fan state only ever names PWM outputs and numbers.
- **The log can no longer be flooded through the login page.** A login
  request whose body held a huge number answered 500 and logged a long
  traceback, before any login and without a limit: some forty requests
  pushed the audit and every other line out of the **Security** tab, and
  filled the Docker log. It is now refused as invalid. A change that
  fails with an unexpected error is now written to the audit log too.

### Fixed

- **A host of the LAN can no longer lock you out of the login.** With
  IPv6, ten wrong passwords from any host of the same network refused
  every other host of it, yours too, for 15 minutes. Now only the
  address that sent them is refused; the rest of its network is just
  slowed down a little.
- **A broken or full log output can no longer stop the fans' safety.**
  The lines of the log were written to the container's output on the
  same thread as fan control. When that output was gone, an error at the
  wrong moment ended fan control with the fans left at their last speed;
  when it was full, everything waited, the safety rules too. The lines
  now go out from a separate thread (dropped from the container's output,
  never from the **Logs** tab, when it cannot keep up), the fans are set
  to full speed before the reason is logged, and they are given back at
  a stop even after such an error.
- **A missing fan chip no longer stops all fan control.** When the chip of
  one chosen fan was missing at start (its module loaded late by an Init
  Script, or renamed by a kernel update), no fan was controlled and the
  drive temperatures were not even measured, so neither the safety rules
  nor the hot alert could act, until a restart. Now only the fans of that
  chip are left alone: the others are controlled and the drives watched.
  The web UI looks for the chip every minute and after **Scan again**, and
  controls its fans as soon as it is back. The drive temperatures are also
  measured when no fan is chosen yet.

- **A PWM test always ends.** If the watch of the drive temperatures
  failed during a PWM test, the test stayed "running" for ever: every
  change answered Busy, the tested output stayed at the last value of the
  test and hddfancontrol did not start again. Now an error of the watch
  stops the test ("the drive temperatures could not be watched"), and the
  outputs are given back and everything starts again in every case.
- **A full or read-only `/config` no longer stops fan control at start.**
  When the hwmon number of a fan chip changed at a reboot and the settings
  or the fan name could not be written, nothing started. Now the error is
  logged and the fans are controlled at their new place anyway.
- **No old temperatures after new settings.** After an apply that added
  or removed an extra sensor (or changed the drives), the last sample of
  the run before was read with the new settings: the processor could count
  as a drive (fans at full speed, a false "drive hot" alert), or a hot
  drive be ignored for one interval. Samples of an earlier run of
  hddfancontrol are now ignored until it writes a new one.
- **A failed sensor scan no longer blocks the next ones.** An unexpected
  error during **Scan again** left the scan "running" for ever, and every
  later scan was refused as busy. It now ends with the error shown.
- **`HWMONS` with a path that holds `:`.** Such a path (a PCI one under
  `/sys/devices/`) was cut at its first `:` into a wrong path and a wrong
  range. It is now read whole and refused with a clear message, since
  hddfancontrol itself cannot read it: give the same input under
  `/sys/class/hwmon/`.
- **Reading SMART again and scanning the sensors no longer fail on big
  servers.** With several drives (or a slow sensors-detect) the page got
  "core unavailable" while the work went on. Both now start at once and
  run in the background: the button stays off until the new data are in.
- **Manual control reacts at once when hddfancontrol stops.** In manual
  control a crash of hddfancontrol put the fans at full speed only after
  the stale limit (2 minutes or more); now it does at once, as in the
  automatic modes.
- **The history keeps recording after a busy database.** When saving a
  sample failed at its last step, every later sample failed too, until a
  restart.
- **After the web UI was killed, every fan goes back to the chip.** When
  the container was killed (out of memory, `docker kill`) a fan it had
  set but no longer drives stayed at its last speed until a reboot. At
  the next start it now gets its own mode back.
- **A fan whose mode cannot be read is given back at full speed.** On a
  driver whose `pwmN_enable` cannot be read, a fan was left at a fixed
  speed when fan control ended; it now goes to full speed, the safe side.

### Changed

- **Settings warn when the history is too short for the fan wear check.**
  Under 14 days of history the check never starts, since it compares
  each fan with its first week once its history is two weeks old; the
  field now says so.
- **The Control card of the Overview in three groups.** Fan control (state,
  mode, the hottest drive and the speed each fan was given), Services
  (hddfancontrol and hd-idle, with the **Restart** button under them) and
  Host (processor time, load and temperature), instead of one long list.

## [0.11.0] - 2026-11-19

### Added

- **Move the bays like blocks.** In **Drives → Bay map**, drag a bay by
  its grip onto another and the two swap their drives, with the mouse or
  a finger; pressing one grip and then the other does the same, also from
  the keyboard. Nothing is saved until **Save the map**.
- **Ask for support of your hardware from the web UI.** When fans,
  sensors or a server are not supported, **Fans → Hardware not
  supported?** (also a button in the "No PWM output" banner) prepares a
  [hardware support request](https://github.com/anto294/hddfancontrol_webui/issues/new?template=hardware_support.yml):
  board and BIOS names, kernel, every hwmon chip with its files, the
  sensor modules, the drives and the last hardware log lines. You see it
  all first; **Open on GitHub** opens the issue form filled in, **Copy**
  copies it. It has no serial number, and addresses in the log are
  masked.

### Changed

- **For any Linux host.** The README now shows the way to install on
  TrueNAS SCALE, Proxmox VE, Unraid, OpenMediaVault and any distribution
  with systemd, and that the web UI must run on the host that has the
  fans (not in a virtual machine or an unprivileged LXC container). Each
  image tag on ghcr.io says what it is: a release, `edge` or
  `experimental`.

## [0.10.1] - 2026-11-18

### Fixed

- **A bay holding a drive that is only monitored no longer says "Not
  found".** Such a drive is not measured by hddfancontrol, so the bay map
  took it for a drive gone from the host, with the red frame. It now
  shows its last SMART temperature, marked SMART and without the colors
  of the limits (those of the drives that drive the fans), or "—", and
  "Not found" only for a drive that is not on the host. The overview
  loads the SMART data the server keeps when it shows the bays, so the
  SMART temperature and problems show without opening the Drives tab.

## [0.10.0] - 2026-11-18

### Added

- **Five more languages:** Spanish, French, Chinese (simplified), Hindi
  and Arabic, for the web UI and for the alerts (`NOTIFY_LANGUAGE`). The
  page follows the browser or the language picked in the header; in
  Arabic it reads from right to left.
- **The bays show their drives moving.** Each bay of the overview draws
  its drive: a hard drive whose platter turns while it spins (still in
  standby) and whose head moves while it reads or writes, an SSD or an
  NVMe stick whose electrons run between controller and flash. An
  activity light flickers with the real I/O of the drive, read from the
  kernel's counters (`/sys/class/block/*/stat`), which never wakes a
  drive; the tooltip gives the share of busy time and the read and write
  rates. Nothing moves for those who asked their system for less motion.
- **Processor use, load and temperature** in the **Control** card of the
  overview: the share of busy time of every processor, the load average
  and the package temperature (`coretemp`, `k10temp`, `zenpower` or the
  `cpu_thermal` of ARM boards).
- **A composer for the schedules.** Next to each cron field of
  **Settings → Schedules**, **Compose** opens a small form that writes
  the expression: every few minutes or hours, at a time on chosen days
  of the week or of the month, or, for the quiet hours, from one hour
  to another on chosen days. It reads back the expressions it can write;
  any other stays as typed until the form is used.

### Changed

- **Other temperature sensors** in **Settings** start folded, with the
  number in use next to the title: a board can have dozens of inputs. So
  do **Limits per drive** (with the number of drives that have their
  own), **Advanced** (with the interval and the days of history) and
  **Backup**. A problem with a field of a folded section opens it.
- **Texts of the web UI** are shorter or more precise: the option to
  give the fans back their settings says that it acts when the web UI
  exits, the drives tile counts the drives awake (SSDs too), and the
  read-only banner, the manual control help and the backup help say the
  same in fewer words.

## [0.9.0] - 2026-11-17

### Changed

- **The web UI moves to port 28462** (it was 8080, a port many other
  apps want). After the update, an app installed from the old example
  publishes `8080:8080`, where nothing listens any more: change the
  line to `"28462:28462"` (on TrueNAS: **Apps → hddfancontrol → Edit**)
  and open `http://<nas>:28462`, or keep the old port with
  `WEBUI_PORT=8080`. Bare metal installs keep the port of their
  `/etc/hddfancontrol-webui/env`.
- The web UI answers only host names of the LAN: IP addresses,
  `localhost`, names without dots and names ending in `.local`, `.lan`,
  `.home.arpa`, `.internal`, `.localdomain`, `.fritz.box`, `.home` or
  `.ts.net`; any other name gets 403, against DNS rebinding. **Behind a
  reverse proxy with a public domain, set the new `WEBUI_HOSTS`** (for
  example `WEBUI_HOSTS=nas.example.com`, `*.example.com`, or `any`); the
  log names the value to set. `/healthz` answers any name.
- HTTPS is on by default (`ENABLE_TLS` defaults to `true`), with a
  self-signed certificate made at the first start unless `TLS_CERT` and
  `TLS_KEY` are set. The same port still answers plain HTTP, so
  bookmarks and the health check keep working; `FORCE_HTTPS` stays off.
  `ENABLE_TLS=false` turns HTTPS off. Without `openssl` (bare metal) the
  web UI stays on HTTP and says why in the log. The login page, opened
  over plain HTTP, warns that the password crosses the network in clear
  text and links to the HTTPS address.

### Security

- A fan's PWM path must be `/sys/class/hwmon/hwmonN/pwmN`, a sensor's
  path a `/sys` path ending in `/tempN_input`, and a fan's chip a path
  under `/sys/devices`. Any `/sys` file was accepted, and the fan
  controller, which runs as root, wrote fan speeds into it: a saved
  setting with `/sys/block/sda/device/delete` as a fan removed a drive.
  Settings with other paths are refused (the environment's too).
- A drive must be `/dev/disk/by-id/…`, `/dev/disk/by-path/…`, `/dev/sdX`
  or `/dev/nvmeNnN` (or an interface name such as `ata`, for the fans):
  any `/dev` node was accepted and handed to smartctl, hdparm and hd-idle
  as root.
- A fixed ceiling, whatever the settings say: a SATA/SAS drive at 65 °C,
  or an NVMe drive at 80 °C, puts its fans at full speed in every mode,
  manual control included, and raises the hot drive alert. Drive
  temperatures in the settings go up to 80 °C (they took 120), and a
  drive's own limits up to 70 °C unless it is NVMe; sensors keep 120.
- A `/config` that the web server's user could write (its own, its
  group's, or everyone's) is given to root and closed to writes by
  others at start, with an error in the log; when that fails, the login
  data are not kept there. The files of `/config` are written through a
  new temporary file with a random name, never a fixed `.tmp` name that
  could be a planted link.
- When `WEBUI_USER` does not exist (or is root, or in root's group), the
  web server no longer runs as `nobody`, which other services may share:
  it does not start, the log says why, and the fans are still controlled.
  The installer stops when it cannot create its user.
- Notifications never follow a redirect (the message would go wherever
  the server says), and a failure shows a short reason in the web UI's
  log; the detail, with the server's host but never the address's query
  (a Gotify token), goes to the container's log.
- With `LOAD_SENSOR_MODULES=false` the **Scan again** button (and
  `POST /api/sensors/scan`) no longer runs `sensors-detect` and
  `modprobe` anyway: it answers that loading modules is off.
- The web server's Python runs isolated (`python -I`): no Python
  variable of the environment, and no module from its working folder,
  can change what it imports. Its `HOME` is no longer a folder it can
  write.
- A temperature sample timed more than a minute in the future (a clock
  that jumped back) counts as old: the fans go to full speed instead of
  trusting it for as long as the clock is off.
- The history CSV writes a name or label that starts like a formula
  (`=`, `+`, `-`, `@`) with a quote in front, so a spreadsheet opens it
  as text instead of running it.
- The systemd unit keeps only the capabilities the web UI needs
  (`CapabilityBoundingSet`), and adds `ProtectKernelLogs` and
  `ProtectClock`.
- At start, the files of `/config` that older versions left readable by
  every user (the history, the temperature log) become readable by root
  only; the web server's folders keep their modes.
- Saving settings with a drive name that leads to something other than a
  block device (a link to `/dev/null`, say) answers 400
  `not_block_device`; a name that leads nowhere (an unplugged drive) is
  still accepted.
- Saving settings with a fan that is no PWM output of the host answers
  400 `unknown_pwm`. Such a fan in a saved file is not controlled, like
  one whose chip is gone, and manual control never writes it either.
- The bare metal package needs aiohttp 3.14.4 or later, the first
  release without known security advisories.
- The bare metal tarballs come with a signed provenance attestation:
  `gh attestation verify <tarball> --repo anto294/hddfancontrol_webui`
  checks that the release workflow built them from the tagged source.
- Known issue: the Docker image still uses Alpine's aiohttp (3.13.5 in
  Alpine 3.24), affected by advisories fixed in aiohttp 3.14.4; it moves
  to it when Alpine ships it. Keep the port to the LAN meanwhile.
- The limit on wrong passwords counts an IPv6 /64 as one client, and
  after 100 wrong passwords from all clients together it slows down the
  clients that sent one (2 s per check, doubling up to 30 s, one at a
  time): a host with many addresses could guess without limit. Nobody is
  refused for it, and a client without a wrong password is not slowed.
- A guess at `WEBUI_PASSWORD` now costs a scrypt hash, like one at a
  stored password, and the password itself is not kept in memory.
- A path with a `..` part, such as `/static/i18n/../app.js`, could pass
  the login as a public file of the login page and then be served as a
  file of the web UI (only from its own folder). Such paths now get 404.
- A wrong current password, when changing the credentials, counts
  against the login limit like a wrong login.

### Fixed

- The PWM test is watched: hddfancontrol keeps measuring the drives
  during it, and the test stops by itself, with the fans given back to
  their control, when a drive reaches its maximum or the temperatures
  stop coming in. The test of one fan stops after 10 minutes. The page
  says why a test was stopped.
- Odd numbers in the settings or a schedule (a `²` in a cron field, a
  temperature of `1e400`, an interval with thousands of digits) are
  reported as invalid values instead of stopping the start or answering
  500.
- A `settings.json` that is not UTF-8 or is nested too deep, or any other
  error reading it, no longer stops the start: the environment is used,
  with the error shown, as for a file with invalid values.
- A `labels.json` or `bays.json` nested too deep no longer stops the
  start: it is logged and the web UI starts without labels or bay map.
- A corrupt `history.db` no longer stops the start: it is renamed
  `history.db.corrupt-<time>` and a new history begins, with an error in
  the log.
- At stop the fans go back to the chip (or stay at full speed, with
  `RESTORE_FAN_SETTINGS=false`) before anything else stops; a PWM test
  running then is cancelled and nothing starts again. They used to wait
  for the web server and the daemons, up to 30 s, and Docker kills a
  container after 10 s: the fans could stay at a manual value. The
  compose example sets `stop_grace_period: 60s` (add it to an existing
  TrueNAS app), and the systemd unit `TimeoutStopSec=60`.
- After the web UI was killed (out of memory, `docker kill`), the next
  start saved the fans' manual leftovers as the chip's own state and gave
  them back at every stop: the fans stayed fixed for good. The chip's
  state is now kept in `/config/pwm-state.json` for the current boot and
  used again after such a kill; an output found in manual mode with no
  saved state gets full speed when given back.
- A JSON body nested too deep gets 400 `invalid_json` instead of an
  internal error with a traceback in the log.

## [0.8.0] - 2026-11-17

### Security

- Privilege separation: the web server no longer runs as root. The core,
  which drives the fans and the drives, runs as root and listens only on
  a Unix socket; a new gateway process, run as the unprivileged user
  `hddfancontrol-webui` (`WEBUI_USER`), holds the port, HTTPS, the login,
  the CSRF check and the pages, and passes on only the requests of a
  logged-in user, with who sent them. The core checks the user of every
  connection, and the gateway gets only the environment it needs.
- No open first start: without credentials only the setup page answers,
  and creating them needs a one-time code written in the container's log
  (a new one at every start). Credentials can be changed but no longer
  removed.
- The port answers only private networks by default (LAN, loopback,
  link-local, carrier-grade NAT and unique local IPv6, so VPNs such as
  Tailscale work); `WEBUI_ALLOW` sets others, `private` for the default
  ones (`private 2a02:…::/64`), or `all`. A refused global IPv6 address,
  which a LAN with IPv6 also uses, is logged with the value to set.
- Sessions end after 12 hours without use (and 7 days after the login).
- Audit log: every change is logged with the user and the client address;
  logins, wrong passwords and setup codes, and refused addresses too.
  The same change repeated within a minute (a fan slider) is logged once
  with a count.
- More security headers (`Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy`, `Permissions-Policy`), and no server
  version in `Server`.
- The compose example runs the container with `no-new-privileges`, a
  process limit and a small `/tmp` without exec; the systemd unit gets
  more sandboxing. Everything the core writes in `/config` is mode 0600.

### Changed

- The credentials and the TLS files move from `/config` to `/config/web`
  (done by itself at the first start). `WEBUI_PASSWORD` is no longer in
  the compose example: the first visit creates the credentials.
- A `TLS_CERT`/`TLS_KEY` pair is copied at start to `/config/web-tls`,
  where the gateway can read it; only one of the two set is reported as
  an error instead of falling back to a self-signed certificate.

## [0.7.1] - 2026-11-17

### Security

- `FORCE_HTTPS` no longer answers plain HTTP with a redirect built from
  the `Host` header, which a client can forge: a page asked over HTTP
  now gets a small page that moves the browser to HTTPS from its own
  address, and the API, `/metrics` and changes refuse plain HTTP (403).

## [0.7.0] - 2026-11-17

### Added

- Fan groups: in the settings each fan can follow only some drives (by
  default all of them), so the case fans can cool the hot enterprise
  drives while another fan follows the rest.
- Limits per drive: each drive can have its own minimum, target and
  maximum temperature over the common ones; the hot drive alert uses the
  drive's own maximum.
- Settings: "Fan groups" (the drives each fan follows) and "Limits per
  drive", with the highest temperature the drive recommends as a hint to
  apply in one click. The overview colors each drive against its own
  limits and lists the speed of each fan.
- Notifications: an alert still on (and not taken note of) can be sent
  again every few hours; quiet hours send only urgent alerts and the rest
  together when they end; a scheduled summary of temperatures, fans,
  alerts and SMART. Environment: `NOTIFY_REPEAT_H`, `NOTIFY_QUIET`,
  `NOTIFY_SUMMARY`, and `TZ` for local time.
- Scheduled SMART self-tests, short and long (`SELFTEST_SHORT`,
  `SELFTEST_LONG`), started without waking a sleeping drive. The drive
  card shows the last self-test, and a failed one is a SMART warning.
- HTTPS: `ENABLE_TLS=true` serves HTTPS on the web UI port, with
  `TLS_CERT` and `TLS_KEY` or a self-signed certificate made in
  `/config/tls`. The same port still answers plain HTTP, and
  `FORCE_HTTPS=true` redirects it to HTTPS (also behind a reverse proxy,
  from `X-Forwarded-Proto`); `/healthz` stays on HTTP for the health
  check. The image now includes `openssl`.
- Schedules are cron expressions, written by hand or picked from a list,
  with the next runs shown as you type: SMART reads, self-tests, summary
  and quiet hours, in a "Schedules" card of the settings.
- Fan wear: once a chosen fan has two weeks of history, its speed at a
  given PWM in its first week is kept as its baseline; a fan that turns 15 % slower
  than that over the last week raises an alert (worn bearings show this
  way well before the fan stops).
- `/metrics` in the Prometheus text format: drive temperatures, fan PWM
  and RPM, the speed asked of each fan, SMART state, alerts and safety.
- History: "Export CSV" downloads the window shown (time, kind, name,
  label, value), at most 20000 points per line.
- The web UI can go on the home screen of a phone as an app (web app
  manifest, icons, full screen on iPhone; Chrome installs it over HTTPS).
- Bay map: draw the bays of the case and put each drive in its bay; the
  overview shows the drives as in the case, with temperature and SMART
  problems (`/config/bays.json`).

- A theme switch next to the language picker, on every page: automatic
  (as the system), light or dark, kept in the browser.

### Changed

- SMART reads follow a cron expression, `smart_schedule` (default
  `0 */6 * * *`, environment `SMART_SCHEDULE`), instead of a number of
  hours. A saved `smart_interval_h` and `SMART_INTERVAL_H` still work and
  are turned into the matching expression.
- The curve mode now runs in the web UI too: hddfancontrol only measures,
  in every mode. The curve is the same, with a margin against pumping: a
  drive asks for more speed at once, but for less only once it is 2 °C
  cooler, and a fan slows down by at most 10 % per check. `AVERAGE` now
  averages the samples of each drive in the web UI. When hddfancontrol
  stops, every fan goes to full speed until it measures again.
- Overview: each drive takes one line, with the name given to it (or its
  model without the brand prefix) and its kernel name and size under it;
  an arrow shows a drive that got 1 °C warmer or cooler in the last 10
  minutes. On wide screens the drives are in two columns, in a card of
  their own above the fans.
- History: point at a chart (or touch it) to read the time and the value
  of every line; click a name in the legend to hide or show its line. The
  temperature chart has dashed lines at the maximum and at the minimum (or
  the target in the auto mode). Drives are named as on the overview, and
  fans that are not chosen, not named and never turned are hidden until
  switched on.
- Fans: the chosen fans come first; outputs that are not chosen, have no
  name and do not turn are folded under "Other outputs". The long
  explanation is folded under "How it works".
- Drives (SMART): each card is titled with the name given to the drive,
  or its family and serial number, instead of the long by-id path; the
  temperature, the highest ever, the recommended maximum, the age and any
  error counter are shown as tiles, the rest under "Details". Drives with
  a problem come first, and their card has a colored border.
- The page uses more of a wide screen (up to 1440 px instead of 1200 px).
- Phones: the header always fits (the title gives way to the buttons), the
  three tiles sit side by side, the tab chosen stays in sight in the tab
  bar, and the tables of the settings become stacked blocks with the
  title of each field; no page scrolls sideways at 375 px.

### Removed

- 32-bit ARM (armv7): no Docker image and no bare metal tarball any
  more. The last release for it is 0.6.0. amd64 and arm64 go on.

### Fixed

- History: with the 1 hour window, lines of samples taken a minute apart
  were cut into invisible pieces. A line now breaks only at a gap of three
  usual steps of its own samples.

## [0.6.0] - 2026-11-16

### Added

- Alerts for a monitored drive that is not on the host any more
  (unplugged, dead, dropped by its controller) and for a drive whose SMART
  data cannot be read.

### Changed

- The temperature limit of a drive is called the highest *recommended*
  temperature, which is what the drive reports ("Min/Max recommended
  Temperature" in smartctl): some drives give a value well under the
  operating limit of their datasheet, such as 40 °C for a Seagate Exos X16.

### Fixed

- An unexpected error in the auto mode or in manual control could stop the
  control loop and leave the fans at their last speed, with nothing on the
  page. Now every fan goes to full speed, a red banner and an alert say so,
  and the loop goes on: control resumes as soon as a step works again.
- An error while recording (a full disk, a locked database) or reading
  SMART data stopped the history and the alerts until the next restart,
  silently. The error is now logged and the loops go on; the alerts are
  checked even when recording fails.
- In the curve mode, when hddfancontrol ended by itself (a crash), the
  fans kept their last speed for up to a minute while it was started
  again. They now run at full speed until it runs again, and get their
  mode back just before it starts.
- A SMART attribute that went under its threshold only in the past (smartctl
  "In_the_past", often the airflow temperature of a drive that once ran
  hot) raised a lasting "attribute went below its threshold" warning. It
  is now listed on the drive card as information, in orange in the
  attribute table; only an attribute failing now is a warning.
- Notifications were sent one by one inside the recording loop, with a
  10 s timeout each: with the server down and several alerts at once, the
  history and the stalled-fan check paused for a minute or more. They now
  go through a queue with their own sender, in order.
- A fan value set in the very moment manual control ended could be
  written after the fans were given back, leaving that output in manual
  mode at that value. The value is now refused once manual control ends.
- New settings or a restart sent in the instant a PWM test ended could
  start a second hddfancontrol next to the first. The end of the test now
  holds the same lock as every other change.
- Moving a fan slider while manual control was starting gave an
  unexplained server error; it is now refused as "not in manual control".
- Manual control ended after 2 minutes when its page was only in a
  background tab, since hidden pages stop asking for the status. In manual
  control the page now keeps asking, so only closing it (or a sleeping
  computer) ends manual control.
- hddfancontrol logs a drive whose temperature probe failed (common under
  heavy I/O such as a scrub) the same way as a sleeping drive. In the auto
  mode a failed probe of every drive dropped the fans to the minimum
  speed. The last temperature of a drive now keeps counting for five
  intervals after it turns empty, in the auto mode and in manual control.
- A new SMART interval only applied after the old wait (up to a week). The
  interval is now read again every minute.

### Security

- After 10 wrong passwords within 15 minutes, an address is refused (HTTP
  429) until the 15 minutes are over, at the login page and for HTTP Basic
  credentials, without running the password check. A right password
  clears the count.
- Behind a reverse proxy that serves HTTPS (X-Forwarded-Proto: https),
  the session cookie is now marked Secure.

## [0.5.1] - 2026-11-16

### Fixed

- The highest operating temperature a drive is made for stayed empty: it
  is in the SCT temperature history, which smartctl now reads too
  (`-l scttemp`).

## [0.5.0] - 2026-11-16

### Added

- What each drive is, on its card in the Drives tab: brand, product line,
  use (NAS, enterprise, desktop, surveillance, SSD) and recording (CMR or
  SMR, with a warning for SMR) from a small catalog of model numbers; the
  family from the drive database of smartctl; speed, form factor; the
  connection (SATA, SAS, NVMe or USB, link speed, and whether it hangs on a
  board SATA port or an HBA such as mpt3sas); and, from the drive itself,
  the highest operating temperature it is made for and the highest it ever
  reached. The card warns when the maximum of the settings is above what
  the drive is made for.
- "Noted, hide" on the alert banners and on the warning about the missing
  password: the banner goes, the note is kept in the database for every
  browser, and a line says how many are hidden, with a button to show them
  again. An alert shows again when it gets worse (one more SMART error) or
  when it ends and comes back; a drive or fan that is gone takes its notes
  with it.
- The database cleans itself: the hourly purge also deletes the series left
  without samples (drives and fans gone for longer than the history) and
  the SMART data of drives no longer monitored.

### Changed

- A SMART alert says what the problem is (for example "Interface CRC
  errors: check the cable") instead of only "SMART reports a problem".

## [0.4.0] - 2026-11-16

### Added

- Alerts: a drive at the maximum temperature, a chosen fan that stops
  (0 RPM for 2 minutes with its PWM value well over its stop value, only for
  fans seen turning), SMART reporting a failing drive or a problem, drive
  temperatures that stop coming in, and fan chips that are not found any
  more. Each one shows as a banner, is logged under `[alerts]` and, with an
  address in Settings → Notifications (or `NOTIFY_URL`), is sent to ntfy,
  Gotify or a JSON webhook, in English or Italian; so is its end. A button
  sends a test message.
- A chart of the fan curve in Settings: speed for each drive temperature as
  the form stands, redrawn while you type, with the hottest drive as a dot
  (in auto mode, the target and the maximum).
- Export and import of the settings and the names of drives and fans, as one
  JSON file (Settings → Backup). An import is checked and applied like a
  save.

### Changed

- Manual control ends by itself when no page of the web UI has been open
  for 2 minutes, so that a page closed in manual control (or during
  Identify in the guided setup) never leaves the fans where they were set.
  A page that is open but hidden in the browser counts as closed.

## [0.3.1] - 2026-11-16

### Changed

- The language picker shows a flag next to each language (drawn as SVG,
  so it looks the same on Windows, which has no flag emoji).

### Fixed

- The footer of the Docker image showed the version with two v's (vv0.2.0):
  the image gets the release tag, and the page added another v.

## [0.3.0] - 2026-11-16

### Added

- A guided setup for first-time users. It opens by itself on the first
  start (no drive and no fan chosen yet), and from the header at any time.
  One question per step: the drives to keep cool, the fans that cool them
  (with an Identify button that runs one fan at full speed for 10 seconds),
  an optional PWM test, a temperature preset (quiet, balanced, cool),
  sleep for idle drives, and a password. Nothing is saved until the last
  step.
- Icons on the tabs, card titles, buttons and banners.

### Changed

- The overview shows tiles with the hottest drive, the average fan speed and
  the drives spinning, and a colored bar for each drive temperature (green,
  amber, red along the configured range) and for each fan, whose icon turns
  with its speed.

## [0.2.0] - 2026-11-16

### Added

- A language selector in the header and on the login page: Automatic (the
  language of the browser), English or Italiano, kept in the browser.
- A `[temps]` log line with the temperatures of the drives that drive the
  fans, the speed of the chosen fans and the SMART temperature of the drives
  that are only monitored, written when a value changes and at least every
  10 minutes.

### Fixed

- A chosen fan was kept only by its `/sys/class/hwmon/hwmonN` path, and that
  number can change at a reboot: hddfancontrol then failed on a missing file,
  or could drive an output of another chip. Each fan now records its chip;
  at start the web UI finds it again under the new number, corrects the
  settings and the fan's name, and logs it. When the chip is gone, those fans
  are not controlled and a banner says which ones.
- The history kept each drive temperature under the kernel name of the
  drive (sda), which can change at every boot: after a reboot, a line of
  the chart could go on with another drive of the same model. Temperatures
  are now kept under a `/dev/disk/by-id` name (`wwn-` when there is one).
  Samples recorded by 0.1.0 keep their old name.
- The PWM test failed at once with "Resource busy" on an output that the
  chip itself drove (nct6775 with `pwmN_enable` other than 1). The output is
  now put in manual mode for the test, and given back the mode and value it
  had when the test ends, however it ends.

### Changed

- "Back to environment variables" asks for a second click within 5 seconds
  before it deletes the saved settings, which cannot be undone.
- Manual control has a slider for every PWM output again, chosen in the
  settings or not, so that you can find out which fan each one drives before
  choosing them. Every output you move gets its previous setting back when
  you leave manual control; the curve and auto modes still drive only the
  chosen fans.
- HTTP requests are no longer logged: the page polls the API every few
  seconds, and those lines buried everything else in `docker logs`. Set
  `WEBUI_ACCESS_LOG=true` to log them again.
- The Italian translation was revised: plainer wording, the usual Italian
  names of the SMART fields, and the same term for the same thing everywhere.

## [0.1.0] - 2026-11-16

First release.

### Added

- A web UI for hddfancontrol 2.1.2 and hd-idle 1.21, in English and Italian.
- Three fan control modes: the curve of hddfancontrol between a minimum and a
  maximum temperature; an auto mode that keeps the hottest drive near a
  target; and manual control with a slider per chosen fan, to find which fan
  is which. In the auto mode, the extra temperature sensors with a range can
  raise the speed too. Only the fans chosen in the settings are ever written;
  the other PWM outputs are only shown. Hot drives put every fan at full speed
  in any mode, and so do temperatures that stop coming in, in the auto mode
  and in manual control with drives chosen.
- The PWM test of hddfancontrol, from the web UI, with its values copied into
  the settings.
- A spin down time per drive.
- History of drive temperatures, fan PWM and fan speed in SQLite, with charts
  from one hour to a year.
- SMART data of the drives you choose to monitor, which need not be the ones
  that drive the fans, read without waking sleeping drives.
- Names for drives and fans.
- A read-only mode, with a warning, when `/sys` is mounted read only (a
  container that is not privileged): nothing is started and no fan is
  written.
- Username and password, set with environment variables or in the web UI.
- Detection of the sensor chip modules, with the steps to load them on a
  TrueNAS SCALE host when the container cannot.
- A multi-architecture Docker image (amd64, arm64, armv7) and, for every
  release, bare metal tarballs with static hddfancontrol, hd-idle and sdparm,
  a systemd unit and `install.sh`.
- Settings from environment variables (`DRIVES`, `PWM`, `DRIVE_TEMP_RANGE`...).
  `RESTORE_FAN_SETTINGS` is on by default: when hddfancontrol stops, the fans
  get their previous settings back instead of running at full speed.

[Unreleased]: https://github.com/anto294/hddfancontrol_webui/compare/v0.11.0...main
[0.11.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.10.1...v0.11.0
[0.10.1]: https://github.com/anto294/hddfancontrol_webui/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/anto294/hddfancontrol_webui/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/anto294/hddfancontrol_webui/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/anto294/hddfancontrol_webui/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/anto294/hddfancontrol_webui/releases/tag/v0.1.0
