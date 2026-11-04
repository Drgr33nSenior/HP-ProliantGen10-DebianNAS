# Network monitoring

Grafana has sibling **Infrastructure** and **Network** folders. Infrastructure
contains the host, storage, workload and monitoring dashboards. Network contains
**Network overview** and **UniFi events**.

The overview shows WAN download/upload, latency and status, device CPU, memory,
temperature and uptime, and NAS interface speed, traffic, errors and drops.
NAS panels use the existing node exporter. WAN/device panels require UnPoller
API polling; [UniFi event logs](UNIFI-LOGS.md) alone do not supply those metrics.
Missing measurements stay **No data**. Not every gateway firmware exposes every
measurement, especially temperature and WAN latency.

## Provisioning and migration

The existing dashboard provider retains its name and dashboard UIDs. Grafana
maps the `Infrastructure` and `Network` directories to sibling folders. The
role stages these below `/etc/microserver/monitoring/dashboard-folders` and
mounts that tree read-only. The previous flat directory is left intact and is
no longer scanned, avoiding duplicate UIDs without deleting dashboard files.
Dashboard URLs, Grafana credentials and database state are preserved.
See [Grafana folder provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/#provision-folders-structure-from-filesystem-to-grafana).

## Gateway performance collection

UnPoller 5.9.3 is an MIT-licensed collector with a digest-pinned image. It polls
every 60 seconds and exposes cached metrics only on `127.0.0.1:9130`. Its
container uses a non-root UID, a read-only root, dropped capabilities, a 256 MiB
memory ceiling and 0.1 CPU. With polling enabled, all six container ceilings
total 5.75 GiB and 1.5 CPUs. These are ceilings, not reservations.

Prometheus stores only the controller-health, cache-age, device-health and WAN
series used by the overview. Per-client and DPI series are excluded. UnPoller
still fetches its normal client data from the controller; the metric filter
reduces stored data, not all API work. DPI, traffic detail, device-tag lookups,
extra event polling and Protect collection are disabled. Loki keeps receiving
events through Vector independently of performance polling.

### Authentication and TLS prerequisites

Use a local API key supported by the installed Network version, or a separate
local Limited Admin account with read-only Network access. Do not use an owner
or cloud-account password. See [UnPoller controller authentication](https://unpoller.com/docs/install/controllerlogin/).

The controller URL must use HTTPS on port 443. The URL hostname must match the
certificate and the certificate chain must be trusted. TLS verification stays
enabled. For a private CA or self-signed certificate, verify the public trust
material and fingerprint through the owner-controlled gateway before installing
it as `/etc/microserver/private/unifi-ca.pem`, root:65534 mode 0440. Include no
private key. Set `microserver_unifi_custom_ca: true` to mount that public trust
file and load it through Go's `SSL_CERT_FILE` trust mechanism. This retains
certificate-chain, validity-date and hostname checks. UnPoller's separate
`ssl_cert_paths` pinning option is not used. Do not trust a certificate merely
because an unauthenticated endpoint returned it.

If the certificate names `unifi.local` and DNS does not resolve that name,
use `https://unifi.local` as the URL and set `microserver_unifi_controller_ip`
to the verified gateway address. Compose adds the hostname alias only inside
UnPoller's container. This preserves hostname verification without changing
the NAS resolver or gateway configuration.

The owner prepares the API key or read-only password directly on the NAS in
`/etc/microserver/private/unifi-credential`, root:65534 mode 0440. Enter the value
through a hidden local prompt; do not put it in chat, shell history, arguments,
environment variables or inventory. The playbook checks file metadata only.
UnPoller reads it with its `file://` credential support. Configured credential
values are never rendered into Compose or the public polling configuration.

After the folder-only monitoring apply, run this in an owner terminal on the
NAS. It refuses to overwrite an existing credential:

```bash
sudo bash <<'SETUP'
set -euo pipefail
private_dir=/etc/microserver/private
credential="$private_dir/unifi-credential"
[[ -d /etc/microserver && ! -L /etc/microserver ]]
[[ $(stat -c '%u:%a' /etc/microserver) == '0:755' ]]
[[ -d $private_dir && ! -L $private_dir ]]
[[ $(stat -c '%u:%g:%a' "$private_dir") == '0:0:700' ]]
[[ ! -e $credential && ! -L $credential ]]
umask 077
trap 'unset microserver_unifi_secret' EXIT
read -r -s -p 'UniFi API key or read-only password: ' microserver_unifi_secret </dev/tty
printf '\n' >/dev/tty
[[ -n $microserver_unifi_secret ]]
(set -o noclobber; printf '%s' "$microserver_unifi_secret" >"$credential")
unset microserver_unifi_secret
chown root:65534 "$credential"
chmod 0440 "$credential"
printf 'UniFi credential prepared privately.\n'
SETUP
```

In the ignored controller configuration, set:

```yaml
microserver_unifi_metrics_enabled: true
microserver_unifi_controller_url: ''  # Fill with the verified HTTPS URL.
microserver_unifi_controller_ip: ''   # Optional exact private IPv4 hostname alias.
microserver_unifi_auth_method: api_key
microserver_unifi_username: ''       # Required only with auth_method: password.
microserver_unifi_custom_ca: false   # True only after the verified CA file exists.
```

Run the reviewed owner monitoring apply. Production deployment remains
owner-run through `ansible/owner-install.yml`; the agent does not run it.
Existing syslog settings and service data remain in place. Polling is disabled
by default, so folder and NAS-link panels can be installed before credentials
are ready.

To disable performance polling, set the metrics flag to false and repeat the
owner apply. The role stops only the Compose project’s UnPoller service and
removes its scrape target. It preserves the credential and existing data.

## Acceptance

1. Confirm **Network** appears beside **Infrastructure**, and UniFi events keeps
   its original dashboard URL. Refresh the dashboard after the apply.
2. Confirm the NAS link panels show the actual interface. Negotiated speed is
   not a throughput benchmark; the current 1GbE link does not qualify the ordered
   10GbE adapter.
3. After enabling polling, require `up{job="microserver-unifi"}=1`,
   `unifipoller_controller_up=1`, and cache age below 180 seconds. HTTP readiness
   alone does not prove successful controller authentication or fresh data.
4. Check WAN rates after several polling intervals. Confirm CPU/memory ratios
   display as percentages, WAN byte counters as bits per second and latency as
   a duration. Compare device readings with UniFi.
5. Confirm no per-client/DPI series are stored, no polling error/staleness alert
   is active, and actual CPU/RAM use remains acceptable. Retest after a planned
   reboot and verify a real UniFi event remains searchable.

The source suite renders both enabled and disabled profiles, checks credential
metadata/TLS boundaries and folder paths, and executes every overview PromQL
query against synthetic data. Gateway API/firmware compatibility and live
readings require owner credentials and hardware qualification.
