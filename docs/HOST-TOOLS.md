# MicroServer host tools and power qualification

## Installed tools and service policy

Use Debian's signed repositories with recommended packages disabled. The base
image includes AMD microcode, onboard Broadcom and AMD graphics firmware, NIC
diagnostics and persistent time sync. The Ansible `packages` stage adds the
storage/monitoring stack and these host tools:

| Package | Purpose and initial policy |
| --- | --- |
| `lm-sensors` | Read supported temperature, voltage and fan sensors; no automatic sensor-bus probing |
| `iperf3` | On-demand network testing; daemon selection is false and its unit is disabled/stopped |
| `nftables` | Firewall tooling; no rules or service enablement changes |
| `systemd-timesyncd` | Persistent clock sync; enabled/started during configure |
| `irqbalance` | Balance hardware interrupts; enabled/started during configure by default |
| `linux-cpupower` | Inspect CPU frequency and idle states; no governor changes |
| `powertop` | Inspect wakeups and power tunables; its auto-tuning unit is disabled/stopped |
| `firmware-amd-graphics` | Support the onboard AMD display without a desktop environment |

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

## Power policy

Keep the kernel/firmware power defaults until the physical system is measured.
The pinned media's kernel builds `schedutil` as its default governor and includes
`acpi-cpufreq`; confirm the active driver and policy on the actual Opteron.
Modern Ryzen `amd-pstate` settings are not a profile for this older processor.

Do not enable PowerTOP's unit as a monitoring service: Debian's unit runs
`powertop --auto-tune`. The profile does not run auto-tuning, calibration,
`sensors-detect`, disk spindown, forced ASPM, governor changes or laptop power
managers. Review any existing owner tuning before combining policies.
The [PowerTOP manual](https://manpages.debian.org/trixie/powertop/powertop.8.en.html)
describes its diagnostic and tuning modes.

From an owner terminal on the installed NAS, start with inspection:

```sh
cpupower frequency-info
cpupower idle-info
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
