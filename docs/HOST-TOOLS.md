# MicroServer host tools and power qualification

## Installed tools and service policy

Use Debian's signed repositories with recommended packages disabled. The base
image includes AMD microcode, onboard Broadcom and AMD graphics firmware, NIC
diagnostics and persistent time sync. The Ansible `packages` stage adds the
storage/monitoring stack and these host tools:

| Package | Purpose and initial policy |
| --- | --- |
| `lm-sensors` | Read supported temperature, voltage and fan sensors; no automatic sensor-bus probing |
| `dmidecode` | Qualify DIMM models before enabling hardware-specific temperature limits |
| `iperf3` | On-demand network testing; daemon selection is false and its unit is disabled/stopped |
| `nftables` | Firewall tooling; no rules or service enablement changes |
| `systemd-timesyncd` | Persistent clock sync; enabled/started during configure |
| `irqbalance` | Balance hardware interrupts; enabled/started during configure by default |
| `linux-cpupower` | Inspect CPU frequency and idle states; TuneD owns governor selection |
| `tuned` | Persist the CPU `performance` governor with the local `microserver-performance` profile |
| `powertop` | Inspect wakeups and power tunables; its auto-tuning unit is disabled/stopped |
| `firmware-amd-graphics` | Support the onboard AMD display without a desktop environment |

The container packages explicitly include `docker-cli` as well as `docker.io`
and `docker-compose`. Debian lists the client as a
[recommended package](https://packages.debian.org/trixie/docker.io), so the
minimal install must select it explicitly for owner diagnostics. It adds no
resident service or container build tooling.

The package transaction suppresses service starts. It then explicitly keeps
the benchmark and PowerTOP units inactive. Configure starts the selected host
services after the existing storage guards; monitoring activation remains a
separate choice. Existing firewall rules are preserved. Debian's
[nftables packaging](https://sources.debian.org/src/nftables/1.1.3-1/debian/rules/)
does not enable/start its unit on initial installation.

The node exporter keeps its loopback binding and adds hwmon, netdev and netclass
collectors for available hardware sensors and network counters. This does not
guarantee that every fan or motherboard sensor is exposed by this board.

## Interrupt balancing

`irqbalance` is a reasonable initial policy for this dual-core X3216 storage/10GbE
host. It distributes eligible hardware interrupts across processors; it does
not guarantee higher throughput for every driver or workload. The profile
does not also assign manual IRQ masks or isolate CPUs.
See the [Debian irqbalance manual](https://manpages.debian.org/trixie/irqbalance/irqbalance.1.en.html).

Measure per-core load and interrupt placement during a network test. If the
default policy regresses measured results, set
`microserver_irqbalance_enabled: false` in the private configuration and repeat
configure. This stops/disables
the daemon; it does not restore a previous IRQ mask. Reboot or restore separately
recorded masks before comparing an unbalanced baseline.

## Docker statistics

The monitoring stage installs a root-owned collector, a systemd service and a
60-second timer. The service and timer start only when
`microserver_monitoring_enabled: true`. The collector reads Docker's version,
container list and one-shot resource statistics through `/run/docker.sock`.
It issues only GET requests and does not inspect container environments.
Root socket access stays in this short host job; Grafana and Prometheus retain
their existing non-root users and mounts.

The collector uses Python's standard library to parse Docker JSON. Debian's
existing Python interpreter is sufficient. Collection supports at most 32
containers, bounds API response sizes and request time, and has a 20-second
systemd timeout. The service has a 0.1 CPU ceiling, 64 MiB memory limit, no
capabilities and no IP networking. The host filesystem is read-only for the
job, with an explicit write allowance for its metrics directory.

Node exporter's textfile collector reads
`/var/lib/microserver/node-exporter/docker.prom`. Atomic replacement prevents
partial snapshots. The metrics include running state, cumulative CPU seconds,
memory usage/working set/limit and available task counts. Memory working set
subtracts inactive file cache; it is not the process RSS value. Host networking
makes per-container network attribution unsuitable here, so the dashboard shows
the host interface counters separately.

A collection error publishes a failure marker instead of partial container
statistics. A stalled job leaves an older timestamp. Measurements older than
three minutes are stale; failed, missing or stale collection alerts have a
five-minute hold while node exporter is available. These alerts use the existing
local Alertmanager route; external notification delivery remains separate.

After owner activation, inspect the timer and last service result:

```sh
systemctl status microserver-docker-metrics.timer --no-pager
systemctl show microserver-docker-metrics.service -p Result -p ExecMainStatus
sudo /usr/bin/python3 /usr/local/libexec/microserver-docker-metrics.py --stdout
```

The last command reads live statistics without writing a metrics file. Allow
two timer samples for new CPU rate graphs. Recheck service limits and actual
overhead on the NAS after deployment.

## DIMM temperature limits

The installed Micron modules report part numbers `9ASF1G72AZ-2G3B1` (8 GB)
and `18ASF2G72AZ-2G6D1` (16 GB). Their JC42 sensors on the SMBus PIIX4 port 0
adapter initially reported maximum and critical limits of 0 C. The measured
temperatures were about 30 C, with high/critical alarm flags set.

Set `microserver_dimm_limits_enabled: true` in the private configuration to
apply the qualified policy during `host`, `sensors` or full configure. It is
disabled by default. The role requires this exact Micron DIMM pair and both
sensor addresses, 18 and 1a, on the known adapter before changing settings.

The local policy selects a 75 C warning, 85 C critical threshold and 3 C
hysteresis (critical reset at 82 C). These are commissioning choices, not
manufacturer-prescribed alarm settings. Micron's normal operating range ends
at 85 C; the extended range requires increased refresh. See the manufacturer
datasheets for the
[8 GB module](https://www.mouser.com/datasheet/2/671/mict_s_a0011299042_1-2290813.pdf)
and [16 GB module](https://www.mouser.com/datasheet/2/671/asf18c2gx72az-1284607.pdf).
Module-sensor readings do not prove the hottest DRAM package temperature.

[microserver-jc42.conf](../ansible/roles/debian_microserver/files/microserver-jc42.conf)
binds the adapter by name, so I2C bus renumbering does not change the match.
Debian's existing `lm-sensors.service` applies the configuration at boot. The
role parses the file without writing hardware settings, then enables/restarts
the service and verifies the effective limits and alarm flags. Failed readback
stops qualification even if the vendor service ignored a command error.
Do not raise limits further to clear a real high-temperature alarm.

Check mode reads DIMM/sensor metadata but skips service activation and runtime
qualification. Install the qualification tools before that check. Verify
`sensors` and `systemctl is-enabled lm-sensors` after a later reboot. Requalify
the policy if modules or the sensor adapter change. The profile does not
change CPU thermal limits, hide sensor readings or run `sensors-detect`.

## Performance mode

The owner selected TuneD to keep the X3216 in performance mode. The local
`microserver-performance` profile contains only `[main]` and `[cpu]`, with
`governor=performance`. It has no inherited disk, network, sysctl, IRQ or
bootloader tuning. Static tuning is enabled and dynamic tuning is disabled.
The profile leaves firmware boost/thermal limits and the required `iommu=pt`
boot argument in place. The active CPU driver must support `performance`;
there is no fallback to another governor if it cannot be applied.

The source is [tuned-performance.conf](../ansible/roles/debian_microserver/files/tuned-performance.conf).
Debian 13 ships TuneD 2.25.1 with local profiles under `/etc/tuned/profiles`.
See [Debian's TuneD package](https://packages.debian.org/trixie/tuned) and
[TuneD profile documentation](https://tuned-project.org/docs/manual.html#creating-new-tuned-profiles).

Run the independent owner tuning stage from the current reviewed checkout with
the same verified private inventory and host alias:

```sh
ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook \
  -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml --ask-become-pass \
  --tags tuning ansible/microserver.yml
```

This stage can run before the data pool is ready. It installs TuneD and CPU
diagnostics with initial service starts suppressed, stages the profile and
manual selection before daemon startup, and enables TuneD. Installed competing
CPU power-manager services are stopped/disabled, including cpupower, auto-cpufreq,
TLP, power-profiles-daemon and tuned-ppd. PowerTOP's auto-tuning unit also remains
inactive. IRQ balancing retains its separate configuration.

The apply checks the active profile, requires at least one CPU frequency policy,
checks every available policy's effective governor and runs `tuned-adm verify`.
Failures stop the stage. Check mode predicts package/file work; it does not start
TuneD or qualify the effective runtime policy. Repeating an unchanged apply
does not restart TuneD. An outside governor change fails verification and needs
review instead of being reported as successful performance tuning.

Record `tuned-adm active`, `tuned-adm verify` and `cpupower frequency-info` on the
NAS, then verify them again after reboot. `performance` requests the highest
available P-state; it does not promise a fixed turbo clock or sufficient
throughput for every workload. Measure temperatures, throughput and wall power.

The owner confirmed `microserver-performance` active, `tuned-adm verify` exit 0,
and TuneD enabled/active after the manual apply. Both `acpi-cpufreq` policies
reported `performance`; CPU boost was enabled. Persistence after reboot and
performance under load remain to be verified.

For an owner rollback, `tuned-adm off` unloads the tuning. Disable/stop the TuneD
service if it should remain off, then verify the effective governor. The next
`tuning` or full configure apply selects performance again. Keep the existing
working installation USB; this policy change does not require rewriting it.

Do not enable PowerTOP's unit as a monitoring service: Debian's unit runs
`powertop --auto-tune`. The profile does not run auto-tuning, calibration,
`sensors-detect`, disk spindown, forced ASPM or laptop power managers. Review any
existing owner tuning before combining policies.
The [PowerTOP manual](https://manpages.debian.org/trixie/powertop/powertop.8.en.html)
describes its diagnostic and tuning modes.

From an owner terminal on the installed NAS, start with inspection:

```sh
cpupower frequency-info
cpupower idle-info
tuned-adm active
tuned-adm verify
sensors
timedatectl show -p NTPSynchronized
systemctl is-active systemd-timesyncd irqbalance
systemctl is-enabled iperf3 powertop
cat /proc/interrupts
```

Expect synchronized time once NTP is reachable. The two diagnostic units should
be disabled/inactive; `systemctl is-enabled` returns nonzero for that expected
state. IRQ balancing should follow the private configuration. Driver, frequency
and sensor results are hardware evidence; retain them privately.

Run a short iperf3 test on the reviewed private network, using the actual NAS IP
and a temporary firewall allowance. Bind a one-off server to that address;
stop it and remove the temporary allowance after testing. Do not leave a
benchmark listener running as a permanent service.

Inspect PowerTOP from a local administrator terminal without applying its
tunables. Use a wall-power meter for whole-system energy comparisons; estimates
may be unavailable on this AC-powered AMD platform. Compare idle power and a
representative transfer, including duration and link/disk stability. CPU TDP is
not a wall-power measurement.

The controller checks source and configuration only. Physical service startup,
clock sync, sensors, IRQ placement, throughput and energy remain first-boot tests.
