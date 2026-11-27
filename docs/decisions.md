# Decisions

The choices that shape the program, each with its reason. Before undoing one, read why
it was made. A new decision gets an entry here in the same change that makes it.

Each entry: the decision, then **Why**, and the date when it was taken.

## Purpose and scope

**A web UI and supervisor around hddfancontrol and hd-idle, written from scratch.**
Why: the existing image (fightforlife/docker_hddfancontrol) has no web UI and no
license; only the names of its environment variables are kept, so a container set up
for it keeps working (`env_document`). (2026-11-16)

**For any Linux host, not one system.** TrueNAS SCALE, Proxmox VE, Unraid,
OpenMediaVault and any distribution with systemd: a Docker image and bare metal
tarballs, both for x86_64 and arm64. What is specific to one system (the TrueNAS Init
Script for a kernel module, its Custom App) is a note in the README, never a
requirement. Why: the fans and drives are Linux's (hwmon, `/dev`), whatever runs on top;
TrueNAS SCALE came first only because it is where the project started. It must run on
the host itself: a VM does not see the fan chip. (2026-11-16, widened 2026-11-19)

**Low resource use.** No polling of a hidden tab, no full read of growing files (the
temperature log is read from its tail), no long-running process without a reason, no
build step. Why: it runs on a NAS all the time. (2026-11-16)

**No dependency that Alpine does not package.** The image uses Alpine's `python3` and
`py3-aiohttp`. Why: a small image built from the distribution's packages, updated with
it. Consequence: own cron parser (no `croniter`), and the image keeps Alpine's aiohttp
until Alpine ships 3.14.4 (see [SECURITY.md](../SECURITY.md#known-issue)).
(2026-11-17)

## Fan control

**hddfancontrol only measures; the web UI writes the PWM files in every mode.** It
runs with `--fan-cmd /bin/true`. Why: one place decides the fans, so fan groups, limits
per drive, the auto mode, manual control and the safety rules all apply the same way,
and switching modes never leaves two writers. hddfancontrol still does what it is best
at: probing drives without waking them. (curve mode moved over in 0.7.0, 2026-11-17)

**Auto mode is a PI controller, and is described as such.** Why: honest naming
(feedback control, not "AI"); drive temperatures move over minutes, so small gains are
calm without tuning. (2026-11-16)

**Slow down gently, speed up at once.** At most 10 % less speed per step; the curve
asks for less only 2 °C under the temperature it last asked for. Why: fans that keep
speeding up and slowing down are louder and wear faster than steady ones; heat must
never wait. (2026-11-16)

**Hard ceilings: 65 °C for SATA/SAS, 80 °C for NVMe, whatever the settings.** Settings
are capped too (80 °C, 70 °C for one SATA drive). Why: a typo or a careless limit must
not cook a drive. (2026-11-17)

**No temperatures means full speed.** Stale samples, hddfancontrol stopping, an error
in the control loop: every fan at 255. A drive whose reading turns null keeps its last
temperature for five intervals. Why: when the web UI cannot see, the only safe guess is
the loud one; and hddfancontrol writes null for a failed probe too, which must not let
a hot drive's fans slow down. (0.6.0, 2026-11-16)

**`RESTORE_FAN_SETTINGS` defaults to true.** When control ends the fans get the mode
they had before, not full speed (hddfancontrol's own default is off). Why: a stop
happens at every settings change, container stop and update, and leaving a NAS loud
after each one is not what most users expect. `RESTORE_FAN_SETTINGS=false` keeps full
speed for those who prefer it. (2026-11-16)

**Curve and auto write only the fans chosen in Settings; manual control can move any
PWM output.** Why: manual control is how a user finds out which fan is which before
choosing them. Each output is restored when manual control ends, and manual control
ends by itself 2 minutes after the last page closes. (2026-11-16)

**Drives have two separate choices: the ones that drive the fans and the monitored
ones.** Why: an SSD or a boot drive may deserve SMART alerts without having a say on
the case fans. (2026-11-16)

**The extra hwmon sensors with a range raise the speed too.** Why: an HBA or a chipset
can be the hottest thing in a NAS case. (2026-11-16)

**Fans are kept with the path of their chip and found again when the hwmon number
changes.** Why: `hwmonN` numbers can change at every boot, and writing to the wrong
output is worse than writing to none. (2026-11-16)

**Writing `/config` is never a condition for controlling the fans.** When a fan is found
at a new hwmon number, saving the corrected settings at start and moving its label are
logged as errors when they fail, and the start goes on; an apply fails only when the
settings themselves cannot be saved. Why: after a reboot with a full or read-only
`/config`, an `OSError` used to stop the whole start (no fan control, no hddfancontrol,
mode "stopped") with no retry; the next start finds the chip again anyway. (2026-11-20)

**The temperature samples of an earlier run of hddfancontrol are fenced off.** Before
each start of hddfancontrol, `TempLog.fence` records the time of the newest sample, and
nothing reads a sample that old again. Why: drives and extra sensors are told apart by
their place in the line, which depends on the settings; and after a restart fan control
counted the old sample as fresh. A few seconds without a sample cost full speed; a
sensor read as a drive costs a false alarm, a drive read as a sensor a missed one.
(2026-11-20)

**Work that can outlast the gateway's wait runs in the background.** A SMART reading
asked by the page and a sensor scan answer 202 at once, and the page follows them in the
status it asks for every few seconds anyway. Why: one drive may take
`SMARTCTL_TIMEOUT`, several drives are read one after the other and sensors-detect may
take `RUN_TIMEOUT`, all at or past `CORE_TIMEOUT_S`; the page got 502
`core_unavailable` while the core worked normally. A longer timeout for some routes
would only move the limit, and keep a connection open for minutes. (2026-11-20)

**The starts of alerts kept for the summary are capped.** At most `RAISED_MAX` (1000),
the newest. Why: the list is rewritten in `meta` at every new start; an alert that comes
and goes once a minute grew it to 20 000 entries (560 KB per write) in two weeks, on the
flash many NAS boot from. Beyond a thousand starts the summary's count is only a floor,
and the alert itself has long been noticed. (2026-11-20)

**A missing fan chip leaves only its fans alone.** The other fans are controlled,
hddfancontrol measures the drives, and the supervisor looks for the chip every
`PLACE_PERIOD` (60 s) and after a sensor scan, starting the fan when it is back. The
chip of a fan sent through the API is ignored: the host's (`Hardware.chip_of`) counts.
Why: before, one missing chip stopped everything: every fan stayed where the BIOS left
it and, with no sample, neither the ceiling nor the stale failsafe nor the hot alert
could act, until a restart. That happens after a reboot when a module is loaded late
(the TrueNAS Init Script runs after the app), on the hosts that need the most care.
Writing the missing fan's old path is still never done: it may belong to another chip.
(2026-11-20)

**Read only when `/sys` is read only.** Nothing starts, and a red banner says so. Why:
an unprivileged container cannot write the fans; pretending it controls them would be
dangerous. (2026-11-16)

**The startup checks never hold up the start.** They run beside the sensor scan and
`Supervisor.start()` (`startup`, `checks_task`), in a daemon thread of their own
(`in_daemon_thread`). Why: they open every drive of `/dev` and every chosen PWM file,
and an `open()` on a dying drive can hang in the kernel for good; the fans come first,
and the checks only report. A daemon thread rather than `asyncio.to_thread`: a call
that never returns would otherwise hold a thread of the shared pool, which
`asyncio.run` waits for at exit. (2026-11-22)

## Data

**History and SMART summaries in SQLite.** Why: one file, no server, queries that
average into a fixed number of points so a chart of a year costs the same as one of an
hour. (2026-11-16)

**Drives are recorded under a stable `/dev/disk/by-id` name (`wwn-` first), never
`sdX`.** Why: `sdX` names can change at a reboot and would mix two drives' histories.
(2026-11-16)

**SMART with `-n standby`.** Why: reading SMART must never be the reason a drive spins
up. A sleeping drive keeps its previous data. (2026-11-16)

**The temperature of a drivetemp chip is listed, never read.** `hwmons()` lists the
inputs of a `drivetemp` chip (`_NO_READ_CHIPS`) with their path and label, and no
value. Why: the kernel answers each read of its `temp*_input` with a SMART command to
the drive, without looking whether it sleeps; its documentation says this may reset
the spin down timer. The page and the monitor read the hwmon chips every few seconds,
so a drive could never spin down. The drive temperatures come from hddfancontrol, which
checks first whether a drive sleeps. The inputs stay in the list, so a setting that
names one is still shown. (2026-11-21)

**The temperature limits a drive reports (SCT) are a hint, never applied.** Why: some
drives (Seagate Exos) report 40 °C, far under their datasheet; applying it would run the
fans flat out. (0.7.0, 2026-11-17)

**Every schedule is a cron expression in local time.** Why: one way to say "when" for
SMART reads, self-tests, summaries and quiet hours, with a preview and a composer in
the page for those who do not write cron. (0.7.0, 2026-11-17)

**Files of `/config` are written atomically, mode 0600.** Why: a power cut must never
leave an empty settings file, and only root needs to read them. A corrupt
`history.db` is set aside instead of stopping the start. (2026-11-17)

**A drive that is gone is archived or deleted from the Drives tab, never by itself.**
Archiving keeps its last SMART data and its history and stops its reads and alerts;
deleting drops all it left (SMART data, series under all its names, notes, labels, bay).
Why: a drive can be gone for a while (a cable, a controller reset) or for good, and
only the user knows which; keeping the data of a dead drive is worth something (its
last SMART, for a warranty claim), as in Scrutiny. A drive that drives the fans is
refused: if it came back while archived, the fans would not follow its temperature.
The change restarts nothing, since only the monitor reads `monitor_drives`. (2026-11-21)

**The permissions and devices are checked once at start, and only reported.** Why: a
container without a device, a capability or a writable volume starts anyway and fails
one feature at a time, far from the cause ("permission denied" from smartctl). Each
problem is named in the log and in a banner with what to do. Nothing is fixed by
itself: the fixes (privileged, a device, a volume) are the user's, and `/config`
already gets its owner and mode at start (`tighten_config`). (2026-11-21)

**The SMART counters are kept once a day, for the life of the drive.** Why: a growing
count (pending sectors, reallocations) says more than any single value, as Scrutiny
shows; one row per counter per day stays small for years, so it is not cut at
`history_days`. Only the counters are kept, not the whole attribute table. (2026-11-21)

**Risk from Backblaze's failure rates, life as a labelled estimate.** Why: a raw count
says little to most users ("2 pending sectors"); the annual failure rate of drives
with that count (Backblaze's data) says how serious it is. The table and limits
(tabulated by the Scrutiny project, MIT License) are taken as they are (MIT License) rather than tuned by
hand. Remaining life is only measurable for SSD/NVMe (their own wear); for hard drives
it is a range from a 5–7 year service life, cut by the risk, and the page says it is
an estimate. A growing critical counter counts more than its size. (2026-11-21)

**Nothing is deleted while `settings.json` is broken.** The hourly `tidy` skips `purge`
and `forget_smart` while the store reports an error (`Monitor(settings_ok=...)`) and
keeps the by-id names only. Why: the environment that stands in for a broken file may
monitor other drives and keep fewer days; a mistake in a file can be fixed, but the
SMART trend of a drive deleted in the meantime cannot come back. (2026-11-22)

**A change of the monitored drives is refused while `settings.json` is broken.**
`Supervisor.set_monitor_drives` raises `SettingsBroken` and the API answers 409
`settings_broken`. Why: the settings in force are then the environment's; saving them
with one drive more or less would replace the user's file, which only needed a fix.
Settings → Save (or Reset) is the way out, and it shows what will be saved. (2026-11-22)

## Security

**Privilege separation: a root core on a Unix socket, an unprivileged gateway on the
port.** Why: the container must be privileged, so the process the network reaches
must not be root. Never weakened; see [security.md](security.md). (0.8.0, 2026-11-17)

**The log echo never waits.** `LogBuffer` writes stdout from a thread with a queue of
`ECHO_QUEUE` lines, dropping lines from stdout (never from the buffer) when it is
full, and the fan controller writes the fans before it logs and ignores a log that
fails. Why: `print` ran on the event loop, next to the failsafe: a stdout that blocks
(a log FIFO nobody reads) froze every safety rule, and one that fails
(`BrokenPipeError`) killed the control loop between a failure and full speed. A line
missing in `docker logs` is a small price; it stays in the **Logs** tab.
(2026-11-20)

**A `/config` that cannot be closed to the gateway means no gateway.** Why: the core
runs as root on what it finds there; a gateway taken over that could write
`pwm-state.json` would make root write any file (the path was not checked), and one
that could swap `templog/` would feed cold samples and turn every safety rule off.
Before, only the gateway's own folder moved to a temporary one and root kept using the
open `/config`. The fans stay controlled; only the web UI is lost until the folder is
fixed. The sticky bit does not make a folder closed: it does not stop new names.
(2026-11-20)

**No open first start.** Without credentials only the setup works, with a one-time code
printed in the log; credentials can be changed, never removed. Why: someone who merely
reaches the port must not be able to claim the installation. (0.8.0, 2026-11-17)

**Private networks and LAN host names only, by default** (`WEBUI_ALLOW`,
`WEBUI_HOSTS`). Why: a NAS web UI is a LAN service; the host check stops DNS rebinding
from a web page. (0.8.0–0.9.0, 2026-11-17)

**Past 100 failed logins overall, slow down instead of refusing.** Why: a refusal for
everyone would let an attacker lock the owner out. (0.9.0, 2026-11-17)

**`notify_url` leaves the core masked, and comes back masked.** Why: it carries a Gotify
token or an ntfy topic that anyone who knows it can read, and the gateway (and anyone
with a session) got it in clear from `GET /api/settings`, although the gateway's
environment was kept free of `NOTIFY_URL` for that reason. Sending the mask back to keep
the address means the page and the backups never need the secret. In the path, ntfy's
topic is masked and so is the whole path of a JSON webhook, where Slack and Discord
keep their token; Gotify's path (`/message`) stays visible, its token is in the query.
(2026-11-20)

**Notifications never go to the NAS itself or to link-local addresses; private networks
stay allowed.** Loopback, link-local, unspecified and multicast addresses are refused,
checked on the address the connection itself uses. Why: the POST leaves from root's
process on the NAS's network, and `POST /api/notify/test` answered 200 or 502 with a
telling delay: a logged-in user (or a gateway taken over) could probe the services that
listen only on the NAS, or a cloud's metadata service, through it. Private networks stay
allowed because ntfy and Gotify very often run on the LAN, and refusing them would break
the main use; a service on the NAS itself is reached by its LAN address. (2026-11-20)

**`WEBUI_ALLOW` is applied at accept, and connections are limited.** One listener
(`DualListener`) serves both with and without TLS. Why: as a middleware, the filter
came after the first byte, the TLS handshake and the headers, and aiohttp sets no
deadline for headers: a client outside the allowed networks could hold thousands of
one-byte connections until the gateway ran out of file descriptors and the container
turned unhealthy. (2026-11-20)

**Refuse an address, only slow down its IPv6 /64.** Why: the /64 was the client before,
so ten wrong passwords from one host of a SLAAC or ULA LAN (and every `fe80::` address)
locked out every other host of it, the owner too; and a right password of the owner
reset the count of the guesser. Counting the /64 still keeps a host from taking a new
address for every ten tries at full speed. (2026-11-20)

**HTTPS on by default, self-signed when no certificate is given, no HSTS.** HTTP and
HTTPS share one port. Why: encrypted by default without any setup; HSTS would lock users
out of a self-signed certificate for good. (0.7.0/0.9.0, 2026-11-17)

**Default port 28462.** Why: 8080 collides with half the apps of a NAS. (0.9.0,
2026-11-17)

## Page

**No build step, no framework, no external resource; DOM from `textContent` only.**
Why: nothing to install or audit beyond the files served; a strict CSP; drive models
and log lines cannot inject HTML. See [web-ui.md](web-ui.md). (2026-11-16)

**Rates are computed by the page from counters.** Why: the server keeps no state per
client, and the network delay does not skew the rates. (0.10.0, 2026-11-18)

**The page asks for the log lines only while the Logs tab is open.** Opening the tab
catches up with `after=` and then draws it. Why: every open page asked for them every 2
s (`POLL_LOGS_MS`) on every tab, though only the Logs tab shows them; the server keeps
the same 2000 lines the page would, so catching up loses nothing. (2026-11-21)

**Requests for the hardware snapshot that come together share it, with no cache.** A
`GET /api/hardware` that arrives while a snapshot is being read waits for that one
(`asyncio.shield`, so a client that leaves never cancels it for the others); the first
request after it reads again, and an error reaches every request that waited. Why:
every open page asks for it every few seconds, and two tabs or two users at once read
the same hundreds of sysfs files twice for the same answer. A cache with a lifetime
would hand out older counters, and the page turns two snapshots into rates by their
`clock`. (2026-11-21)

**A newer release is named in the footer, from a daily check by the core.** Once a
day (`CHECK_PERIOD`) the core asks GitHub's API for the latest release; the status
carries a newer one and the page links to it. Why: most installs are a container or
a tarball left running for months, and a fix (a safety rule, a security one) does
not reach them unless someone looks. The core asks, not the browser: the page keeps
its CSP with no outside address, and one request a day serves every page. Only
releases are checked (`dev`, `edge-…` and `experimental-…` builds never ask), only
the latest non-beta release is offered, nothing about the host is sent, and
`CHECK_UPDATES=false` turns it off for a host that must not reach the Internet. It
only tells: nothing is downloaded or installed. (2026-11-22)

**Seven languages, translated by meaning.** English, Italian, Spanish, French, Chinese,
Hindi and Arabic (right to left). Why: the most spoken languages; a literal translation
reads badly. (0.10.0, 2026-11-18)

**More languages for the people who run home servers.** German, translated by meaning
like the others and reviewed as a native speaker would read it. Why: the communities
that build their own NAS and home servers are large where it is spoken and often read no
English. Each language more is one more file for every new key, so the list stops where
few users are left. (2026-11-27)

## Branches and images

**Features under test live on the `experimental` branch, which is checked like `main`
and publishes its own image tag.** Every push to it runs the same CI (lint, tests on
Python 3.11 and 3.14, Bandit, hadolint, actionlint, the image test) and CodeQL, and
publishes `:experimental`; `edge` and `latest` never come from it, and a manual run on
any other branch publishes nothing. Why: a feature that needs real hardware we do not
have (a Dell server) must be tried by others from an image, without reaching the users
of `edge` or of a release; and what they run must pass the same checks. Merging into
`main` stays a separate decision. (2026-11-19)

**`experimental` is `main` plus the hardware under test.** `main` is merged into
`experimental` whenever it changes, never the other way until a feature has been
confirmed on real hardware; then `experimental` is merged into `main` in one go. No
rebase: both branches are published. Why: the testers of `:experimental` must get every
fix and function of `main` too (the support request first of all), and a branch that
drifts apart costs more to bring back the longer it waits. (2026-11-19)

**The release builds from hashed tools, and only its upload step holds the token.**
Why: a new release of `build` or `setuptools` (taken unpinned before) ran in the job
that holds `contents: write`; it could change the wheel that `install.sh` runs as root,
and the provenance attestation would have signed the changed tarball all the same.
Dependabot proposes the updates of the pinned tools. (2026-11-20)

**The bare metal tarball carries its Python dependencies, hashed, as pure Python
wheels.** Why: `install.sh` ran `pip install` as root against PyPI, taking the latest
release of every transitive dependency (and building a source package when no wheel
fitted). The pure wheels of aiohttp and its dependencies serve every architecture and
Python version at once; their C speedups are not needed for one page on a LAN.
(2026-11-20)

## Support

**Hardware we do not own is supported from what its owner sends, through a guided
issue form that the web UI can fill in.** The form says which commands to run and where
(TrueNAS without installing anything, a throw-away container for `ipmitool`, a live
Linux); the web UI's button sends the same report. Neither reads a serial number, and
the user sees everything before it leaves the host. Why: requests such as "support my
Dell R730xd" cannot be answered without the machine's own data, and that data must not
expose the owner. (2026-11-19)
