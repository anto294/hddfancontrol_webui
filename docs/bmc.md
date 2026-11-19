# Server fans through the BMC (experimental)

[Documentation](README.md) · the choices behind this page are in
[decisions.md](decisions.md#server-fans-through-a-bmc-experimental).

Status: **experimental**, on the `experimental` branch only. Not tested on real
hardware yet: the code has been checked against a fake `ipmitool` that answers like
an iDRAC 8 and an iLO 4. Read this page before trying it.

Rack and tower servers such as the Dell PowerEdge R730 / R730xd or the HPE ProLiant
ML350 drive their fans from the BMC (iDRAC, iLO), not from a chip that Linux sees:
`/sys/class/hwmon` has no PWM output for them. The web UI can now talk to the BMC of
the host it runs on, through `/dev/ipmi0` and `ipmitool`. It never connects to the
BMC over the network, and stores no BMC password.

## What it does

| Server | BMC | Fans | Sensors |
| --- | --- | --- | --- |
| Dell PowerEdge 12th and 13th generation (R620, R720, R630, R730, R730xd…) | iDRAC 7 / 8 | Left to the iDRAC, or driven by the web UI | Shown |
| HPE ProLiant Gen8 / Gen9 / Gen10 (ML350, DL380…) | iLO 4 / 5 | Left to the iLO | Shown |
| Any other server with IPMI | | Left to the BMC | Shown |

Everything is in the **IPMI** tab, which shows only on a host with a BMC:

- **Server**: the BMC and its firmware, and the server model, found in the FRU data
  (`ipmitool fru print 0`) or chosen by brand and model. Choosing a model sets the
  minimum speed to start from (20 % for an R730xd, 15 % for an R730…).
- **Server fans** (Dell only): who drives them, the iDRAC (the default) or the web UI.
  The web UI runs them at the highest of:
  - the **minimum speed**;
  - the **CPU profile**: silent (full speed from 85 °C), normal (80 °C), turbo
    (75 °C) or custom (2 to 8 points of your own). It follows the processor
    temperature of the host (coretemp, k10temp), else the processor sensors of the
    BMC; it goes up at once and comes down by 1 % a second;
  - the **drives**, when **iDRAC · Fans** belongs to a fan group in **Settings**: the
    curve or auto mode of the web UI, with all its safety rules.
- **Now**: what drives the fans and why, and what the processors and the drives ask.
- **BMC sensors**: every temperature and fan, with its limit.

The configuration lives in `/config/bmc.json`, apart from the settings: saving it
never restarts fan control. The overview has a short card with the state of the BMC.

### Dell

The iDRAC has undocumented OEM commands, used by many homelab scripts:

```sh
ipmitool raw 0x30 0x30 0x01 0x00          # the web UI takes the fans (manual mode)
ipmitool raw 0x30 0x30 0x02 0xff 0x14     # every fan at 0x14 = 20 %
ipmitool raw 0x30 0x30 0x01 0x01          # the iDRAC drives them again
```

The fans of the iDRAC show as one PWM output, **iDRAC · Fans**, which can join a fan
group like any other in **Settings** or in the guided setup. They never stop, so they
get no PWM test: their start and stop value is 38 (15 %) in the guided setup. The
minimum speed of the IPMI tab applies whatever the drives ask.

Dell says nothing about these commands and may remove them: reports say they stopped
working on 13th generation servers from iDRAC 8 firmware 3.30.30.30, and on 14th
generation servers (iDRAC 9) from 3.34.34.34. Check the firmware of the iDRAC.

### HPE

HPE gives no way to set a fan speed on iLO 4 or iLO 5. The only one known is a
modified iLO 4 firmware made by the community (it needs iLO 4 2.77 or older, since
HPE removed the tools in 2.78): flashing it can make the iLO unusable, and the web UI
will not use it. What HPE supports is the thermal profile of the iLO/BIOS (optimal,
increased or maximum cooling).

When an ML350 is loud, the cause is often one the iLO reacts to: a disk or a PCIe card
that is not HPE's (no temperature sensor the iLO knows), or a hardware fault (a failed
Smart Storage battery, for example). The iLO's own logs say which.

## Safety

While the web UI drives them, the iDRAC no longer cools the processors by itself, and
the fans keep the last speed set until someone changes it. So the fans go back to the
iDRAC:

- at every start, since a run that was killed may have left them in manual mode, and
  at exit (also with `RESTORE_FAN_SETTINGS=false`);
- while **iDRAC · Fans** belongs to a fan group and fan control has let them go (stop,
  apply, settings not complete): the drives would not be followed;
- while a temperature of the BMC is within 5 °C of its upper non-critical threshold
  (80 °C when the BMC gives none), or the BMC reports it as not ok; the web UI takes
  them again 3 °C lower;
- when the sensors of the BMC cannot be read for 90 seconds, or no processor
  temperature can be read.
- on any unexpected error of the web UI's BMC loop, which then goes on.

A command interrupted at exit is allowed to finish first, so the iDRAC always gets the
last word.

Full speed is never given back: the safety rules of the web UI (a drive at its
maximum, temperatures that stop coming) put the fans of the iDRAC at 100 %, which
cools everything.

What it cannot cover: if the whole container is killed while the fans are in manual
mode, they stay at their speed until the web UI starts again (Docker's restart policy
restarts it) or the server is power cycled. Keep the minimum speed reasonable.

## Trying it on a Dell

1. On the host (TrueNAS: **System → Shell**), check that the BMC answers:
   ```sh
   ipmitool mc info        # Manufacturer ID : 674 (Dell), the firmware revision
   ipmitool sensor         # temperatures, fans and their thresholds
   ```
   Without `/dev/ipmi0`, load the modules: `modprobe ipmi_si ipmi_devintf`.
2. Try the commands by hand, and look at the fans:
   ```sh
   ipmitool raw 0x30 0x30 0x01 0x00 && ipmitool raw 0x30 0x30 0x02 0xff 0x1e   # 30 %
   ipmitool sensor | grep -i fan
   ipmitool raw 0x30 0x30 0x01 0x01                                             # back to the iDRAC
   ```
   If the first command answers *Invalid command* or *Insufficient privilege*, the
   firmware does not allow it, and the web UI will show the BMC read only in practice.
3. Run the image of the `experimental` branch, privileged as usual (a privileged
   container sees `/dev/ipmi0`). The log shows `[bmc] BMC found: iDRAC …`.
4. Open the **IPMI** tab: check the model, choose **Web UI** and a CPU profile, save,
   and watch **Now** and the fans. To make them follow the drives too, add
   **iDRAC · Fans** to a fan group in **Settings**.

`BMC=false` turns the whole thing off; `IPMITOOL_BIN` names another `ipmitool`.

### Third-party PCIe cards on a Dell

A PCIe card that is not Dell's (an HBA, a NIC) often makes a 13th generation server
run its fans high. Dell documents a command that turns that response off and leaves
everything else to the iDRAC, which may be all that is needed
([Dell KB 000135682](https://www.dell.com/support/kbdoc/en-us/000135682)):

```sh
ipmitool raw 0x30 0xce 0x00 0x16 0x05 0x00 0x00 0x00 0x05 0x00 0x01 0x00 0x00   # off
ipmitool raw 0x30 0xce 0x01 0x16 0x05 0x00 0x00 0x00                              # its state
```

The web UI does not send it: it changes a setting of the iDRAC that stays.

## Sources

- [Dell KB 000135682: third-party PCIe card cooling response on 13G servers](https://www.dell.com/support/kbdoc/en-us/000135682)
- [White-Raven/PowerEdge-shutup](https://github.com/White-Raven/PowerEdge-shutup) and [PowerEdge-IPMItools](https://github.com/White-Raven/PowerEdge-IPMItools)
- [Dell fan control commands](https://gist.github.com/slykar/f90ad596b18d5ab1eb1c66b2ccf51c54)
- [Unraid forum: HPE iLO 4 fan speed](https://forums.unraid.net/topic/141249-how-to-control-hpe-ilo-fan-speed-ilo-4-gen-8~9)
- [HPE community: ML350 Gen9 fans at 100 % after boot](https://community.hpe.com/t5/proliant-servers-ml-dl-sl/ml350-gen9-fan-at-100-after-boot/m-p/7175517)
