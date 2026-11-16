# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- The environment variables of fightforlife/docker_hddfancontrol.
  `RESTORE_FAN_SETTINGS` is on by default: when hddfancontrol stops, the fans
  get their previous settings back instead of running at full speed.

[Unreleased]: https://github.com/anto294/hddfancontrol_webui/compare/v0.5.1...main
[0.5.1]: https://github.com/anto294/hddfancontrol_webui/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/anto294/hddfancontrol_webui/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/anto294/hddfancontrol_webui/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/anto294/hddfancontrol_webui/releases/tag/v0.1.0
