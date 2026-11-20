# Fan control

How the temperatures are measured, how the fan speeds are chosen and which safety
rules always hold. The modes as a user sees them are in the
[README](../README.md#fan-control-modes); the reasons are in
[decisions.md](decisions.md).

## Measuring

hddfancontrol does the measuring and only that. The web UI starts it as

```
hddfancontrol -v INFO daemon --drives … --fan-cmd /bin/true --interval 20s
              --temp-log /config/templog/temps.jsonl …
```

`--fan-cmd /bin/true` keeps it away from the fans; it still probes the drives the way
it always has, without waking a sleeping drive, and writes one JSON line per interval
to the temperature log. It runs whenever drives are chosen, even with no fan chosen yet
or with the chip of every fan missing: the alerts and the history need the
temperatures, and a fan that comes back finds them.

`templog.py` reads only the last line of that log, and only when its size or mtime
changed; it keeps the last sample across the daily rotation. A line with a temperature
that is no finite number (`NaN`, an infinity) is no sample at all (`parse_line`): `NaN`
would pass every comparison with a maximum, so the line ends in the stale failsafe
rather than in a drive left out. In a line hddfancontrol
writes every drive first, then every extra hwmon input; an interface name such as
`ata` gives one entry per drive, so the drives are every value but the last
`len(hwmons)`.

Since that split depends on the settings a line was written with, the samples of an
earlier run are never read with new ones: before each start of hddfancontrol the
supervisor calls `TempLog.fence`, and `latest()` gives nothing until a newer sample
comes. Fan control treats that as waiting (full speed), and the alerts, the `[temps]`
line, `/metrics` and the page see no sample either. Without the fence, dropping an
extra sensor made the processor a drive (full speed, a false "drive hot" alert), and
adding one made the last drive a sensor for up to an interval.

hddfancontrol writes `null` for a sleeping drive and for a probe that failed alike (it
accepts 4 failed probes in a row, common during a scrub, and exits at the 5th). So
`DriveTemps` keeps the last temperature of a drive that turns `null` for
`NULL_HOLD_INTERVALS` (5) intervals: a failed probe of a hot drive never lets its fans
slow down, and a drive that really sleeps drops out afterwards. It also averages the
last `average` samples of each drive (the `--average` of hddfancontrol has no effect,
since hddfancontrol drives no fan).

When hddfancontrol stops (it crashed, or it is being restarted), the `on_exit` hook of
its `Daemon` tells the fan controller (`measuring_lost`): full speed until a newer
sample arrives. In manual control too: `_manual_step` takes it as stale temperatures
(`safety_reason` "stale"), so every fan goes to full speed at once instead of after the
stale limit, and stays there until manual control ends, like any safety of manual
control.

## Writing the PWM files

`fancontrol.PwmWriter` is the only code that writes a PWM file.

- The first time it writes an output, it saves the output's `pwmN_enable` and value, in
  memory and in `/config/pwm-state.json` together with the boot id. A run after a
  SIGKILL of the same boot then takes the chip's real state from the file, not its own
  leftovers ("manual, 255"). Only entries that are PWM outputs (`is_pwm_path`) with a
  mode and a value of 0 to 255 are read back; the rest is logged and ignored, and
  `_give_back` checks the path again. At start, before anything is taken, `restore_leftovers`
  gives every output of that file back its mode (logged): one this run no longer
  drives (a fan taken out of the settings, one moved by a slider in manual control)
  would otherwise stay in manual mode at the killed run's last value until a reboot.
  The outputs this run drives are taken again right after.
- It sets `pwmN_enable` to 1 (manual), then the value. It writes only paths that pass
  `is_pwm_path` (`/sys/class/hwmon/hwmonN/pwmN`).
- `restore()` gives every output its mode back. An output that was already in manual
  mode gets 255: its old value may be one a killed run left, and full speed is the safe
  one. So does an output whose `pwmN_enable` exists but cannot be read: its mode is
  unknown, and its old value alone would leave it in manual mode at the speed of that
  moment.
- At the final stop with `RESTORE_FAN_SETTINGS=false`, the fans stay at full speed
  instead. Every other stop (apply, a mode change) always restores; otherwise the next
  run would save "manual, 255" as the chip's own state and the fans would never go back
  to the chip.

A fan speed (0–1) becomes a PWM value like in hddfancontrol: from the stop value to 255,
and at least the start value for a fan that stands still (`speed_to_pwm`).

## Modes

Every `interval` seconds, `FanController._control_step` works out a speed per fan.

**Fan groups.** Each fan follows the drives of its group (`Pwm.drives`), or every
drive when its group is empty. A drive outside every group belongs to every fan. A fan
whose drives are all gone follows every drive.

**Limits.** Each drive has the common minimum, target and maximum, or its own
(`Settings.limits_of`).

### Curve

For each drive, `curve_speed`: the minimum speed up to the minimum temperature, full
speed from the maximum, a straight line in between. A fan takes the highest speed its
drives ask. Two margins keep it calm:

- a drive asks for less only once it is `CURVE_COOLDOWN` (2 °C) cooler than the
  temperature it last asked for; warming counts at once;
- a fan slows down by at most `MAX_DECREASE_PER_STEP` (10 %) per step, and speeds up
  without a limit.

### Auto

One PI controller per fan, on the largest error among its drives (temperature minus
target). `KP` 0.08 adds 8 % of speed per °C above the target; `KI` 0.0015 adds about
9 % a minute for each °C that stays above it. Drive temperatures move over minutes, so
these gains stay calm. Slowing down is limited to 10 % per step, speeding up is not. A
drive at its maximum puts its fans at full speed whatever the controller says.

### Extra sensors

An hwmon input of **Settings** with a range asks for a speed in proportion between its
minimum and maximum; a fan runs at the higher of that and what its drives ask. Without
a range it is only shown and recorded.

The path must hold no `:`. hddfancontrol (2.1.2, `HwmonSettings::from_str` in its
`cl.rs`) cuts `--hwmons` at the first `:` (`splitn(3, ':')`), so a PCI path such as
`/sys/devices/pci0000:00/0000:00:18.3/hwmon/hwmon2/temp1_input` would reach it
broken; `from_dict` refuses it (`temp_path_colon`). The same input is under
`/sys/class/hwmon/hwmonN/tempN_input`, which is what the page offers. In `HWMONS`
the range is read from the end, and only when it is numbers, so such a path is
reported as itself rather than cut into a wrong path and a wrong range.

### Manual

A slider per PWM output, for finding out which fan is which. Any output of the host can
be set, not only the configured ones. Manual control is never saved, and it ends by
itself after `MANUAL_IDLE_TIMEOUT` (120 s) without a sign of the page: every open page
asks for the status every few seconds, also from a background tab while manual control
is on. A safety loop watches the drives meanwhile.

## Safety rules

These hold in every mode, whatever the settings. They must never be weakened.

| Rule | What happens |
| --- | --- |
| A drive at its maximum (its last reading, never the average) | Its fans at 255; in manual control, every fan |
| A drive at `DRIVE_CEILING` 65 °C (`NVME_CEILING` 80 °C for NVMe), whatever its maximum | The same; the hot alert uses the same ceiling |
| No sample newer than `max(3 × interval, 120 s)` | Every fan at 255 (manual control: only when drives are chosen) |
| hddfancontrol stops | Full speed until a newer sample; in manual control, every fan at 255 until it ends |
| An unexpected error in the control or manual loop | Every fan at 255, `error` in the status, the loop goes on |
| Control ends (stop, apply, exit) | Every output gets its previous mode back |
| The chip of a chosen fan is missing | Only that fan is left alone (its old path may belong to another chip); the others are controlled and the drives watched |

The fans are always written before the line that says so is logged, and a log that
fails is dropped (`FanController._log`): a full or broken stdout must never come
between a failure and full speed. `stop()` gives the outputs back in a `finally`, even
when the loop it cancels ended with an error. `LogBuffer` echoes to stdout from a
thread of its own, through a queue of `ECHO_QUEUE` (1000) lines that drops what does
not fit, so `add()` never waits on the event loop.

The settings themselves are capped: drive temperatures up to 80 °C, the limits of one
SATA/SAS drive up to 70 °C.

## The PWM test

`hddfancontrol pwm-test` finds the start and stop values of each fan. The supervisor
runs it per output, with the outputs taken in manual mode first (`PwmWriter.take`:
some drivers, such as nct6775, refuse a value while the chip drives the output), and
reads the result from its output (`_THRESHOLDS`). Meanwhile hddfancontrol keeps
measuring and `_watch_test` cancels the test when a drive reaches its maximum or the
temperatures stop coming (`test.stopped_by`: `hot`, `stale`). An unexpected error of the
watch itself stops the test too (`error`, `PWM_TEST_STOPPED_ERROR`): without the watch a
hot drive would go unseen. Whatever ends the test, `_after_test` always runs: an error of
the watch is logged in the `finally` of `_run_pwm_test`, never raised past it, so the
outputs are given back, the test stops counting as running and the daemons start again.
Each output is cut at `PWM_TEST_MAX_S` (600 s). Other changes answer `Busy` during a test.

## Spin down

hd-idle gets `-i 0` (it watches every disk of the system, so its default must be
"never") and one `-a <drive> -i <seconds> -c ata|scsi` per chosen drive with a spin
down time; a drive with 0 never spins down. Drives given as an interface name are no
files, and hd-idle cannot use them.

## Server fans through a BMC

EXPERIMENTAL: the fans of a Dell server show as one output, `ipmi:fans`, written
through the iDRAC instead of a file, with a CPU curve of their own. See
[bmc.md](bmc.md).
