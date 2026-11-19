# Decisions

The choices that shape the program, each with its reason. Before undoing one, read why
it was made. A new decision gets an entry here in the same change that makes it.

Each entry: the decision, then **Why**, and the date when it was taken.

## Purpose and scope

**A web UI and supervisor around hddfancontrol and hd-idle, written from scratch.**
Why: the existing image (fightforlife/docker_hddfancontrol) has no web UI and no
license; only the names of its environment variables are kept, so a container set up
for it keeps working (`env_document`). Main target TrueNAS SCALE; bare metal tarballs
with systemd too, so it is not Docker only. (2026-11-16)

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

**Read only when `/sys` is read only.** Nothing starts, and a red banner says so. Why:
an unprivileged container cannot write the fans; pretending it controls them would be
dangerous. (2026-11-16)

## Data

**History and SMART summaries in SQLite.** Why: one file, no server, queries that
average into a fixed number of points so a chart of a year costs the same as one of an
hour. (2026-11-16)

**Drives are recorded under a stable `/dev/disk/by-id` name (`wwn-` first), never
`sdX`.** Why: `sdX` names can change at a reboot and would mix two drives' histories.
(2026-11-16)

**SMART with `-n standby`.** Why: reading SMART must never be the reason a drive spins
up. A sleeping drive keeps its previous data. (2026-11-16)

**The temperature limits a drive reports (SCT) are a hint, never applied.** Why: some
drives (Seagate Exos) report 40 °C, far under their datasheet; applying it would run the
fans flat out. (0.7.0, 2026-11-17)

**Every schedule is a cron expression in local time.** Why: one way to say "when" for
SMART reads, self-tests, summaries and quiet hours, with a preview and a composer in
the page for those who do not write cron. (0.7.0, 2026-11-17)

**Files of `/config` are written atomically, mode 0600.** Why: a power cut must never
leave an empty settings file, and only root needs to read them. A corrupt
`history.db` is set aside instead of stopping the start. (2026-11-17)

## Security

**Privilege separation: a root core on a Unix socket, an unprivileged gateway on the
port.** Why: the container must be privileged, so the process the network reaches
must not be root. Never weakened; see [security.md](security.md). (0.8.0, 2026-11-17)

**No open first start.** Without credentials only the setup works, with a one-time code
printed in the log; credentials can be changed, never removed. Why: someone who merely
reaches the port must not be able to claim the installation. (0.8.0, 2026-11-17)

**Private networks and LAN host names only, by default** (`WEBUI_ALLOW`,
`WEBUI_HOSTS`). Why: a NAS web UI is a LAN service; the host check stops DNS rebinding
from a web page. (0.8.0–0.9.0, 2026-11-17)

**Past 100 failed logins overall, slow down instead of refusing.** Why: a refusal for
everyone would let an attacker lock the owner out. (0.9.0, 2026-11-17)

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

**Seven languages, translated by meaning.** English, Italian, Spanish, French, Chinese,
Hindi and Arabic (right to left). Why: the most spoken languages; a literal translation
reads badly. (0.10.0, 2026-11-18)

## Support

**Hardware we do not own is supported from what its owner sends, through a guided
issue form that the web UI can fill in.** The form says which commands to run and where
(TrueNAS without installing anything, a throw-away container for `ipmitool`, a live
Linux); the web UI's button sends the same report. Neither reads a serial number, and
the user sees everything before it leaves the host. Why: requests such as "support my
Dell R730xd" cannot be answered without the machine's own data, and that data must not
expose the owner. (2026-11-19)
