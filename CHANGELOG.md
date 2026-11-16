# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

- Manual control has a slider for every PWM output again, chosen in the
  settings or not, so that you can find out which fan each one drives before
  choosing them. Every output you move gets its previous setting back when
  you leave manual control; the curve and auto modes still drive only the
  chosen fans.
- HTTP requests are no longer logged: the page polls the API every few
  seconds, and those lines buried everything else in `docker logs`. Set
  `WEBUI_ACCESS_LOG=true` to log them again.

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

[Unreleased]: https://github.com/anto294/hddfancontrol_webui/compare/v0.1.0...main
[0.1.0]: https://github.com/anto294/hddfancontrol_webui/releases/tag/v0.1.0
