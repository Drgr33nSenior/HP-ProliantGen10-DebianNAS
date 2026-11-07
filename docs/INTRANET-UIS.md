# Intranet monitoring UIs

Enable the optional Nginx proxy to share Grafana, Prometheus and Alertmanager
through internal DNS. The proxy listens on one private NAS IPv4 address and
allows only the configured client networks. Unknown hostnames return HTTP 404.
The three backends, Loki and the exporters retain their loopback listeners.

This profile uses HTTP on the trusted intranet. Grafana retains its login and
disabled anonymous access and signup. Create individual Grafana Viewer accounts
for dashboard users. Prometheus and Alertmanager retain their native access
model, so include only trusted administrator networks in the proxy allowlist.
Use the VPN for access from outside the home network.

## Configure access

Set these public fields in the private MicroServer variable file. Replace the
example address, DNS suffix and networks with the reviewed deployment values.
Include the NAS address in the allowlist for local proxy readiness checks.

```yaml
microserver_monitoring_enabled: true
microserver_ui_enabled: true
microserver_ui_address: '192.168.50.10'
microserver_ui_domain: 'lan.example.test'
microserver_ui_allowed_networks:
  - '192.168.50.0/24'
  - '192.168.2.0/24'
  - '192.168.4.0/24'
```

Keep the DMZ and other untrusted networks outside this list. Preserve the
gateway's DMZ-to-Infrastructure block. Do not add WAN port forwarding or public
DNS records for these services. Clients must use the internal DNS resolver,
including through the VPN.

Create these internal A records, all pointing to the selected NAS address:

| Name | UI |
| --- | --- |
| `grafana.lan.example.test` | Grafana dashboards and log queries |
| `prometheus.lan.example.test` | Prometheus queries and target status |
| `alertmanager.lan.example.test` | Alertmanager alerts and administration |

## Apply on the commissioned NAS

The owner runs the component entry point from the controller. It does not
select storage, disk, NFS, tuning or telemetry configuration tasks:

```bash
ansible-playbook -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml \
  -e '{"microserver_owner_install": true}' --ask-become-pass \
  ansible/owner-ui.yml
```

The role validates the private address, DNS suffix and canonical IPv4 CIDRs
before rendering. It installs Debian's signed `nginx` package with automatic
service startup blocked. An active distribution Nginx service causes refusal.
The distribution service stays stopped, disabled and masked to prevent a second
Nginx instance from binding port 80. `microserver-ui.service` runs the
dedicated proxy as `www-data` with only the capability to bind port 80.
Its ceilings are 128 MiB and 0.2 CPU, with one Nginx worker.

Root template validation uses `pid /dev/null;`. The service supplies its own PID
path under `/run/microserver-ui`. This prevents validation from leaving a
root-owned PID file that blocks the first unprivileged start. One master and
one worker under `microserver-ui.service` is the expected process layout.

Nginx validates its configuration before installation. A separate Compose
validator refuses changes outside the three UI external URL settings. The
update recreates only Grafana, Prometheus and Alertmanager when needed, using
the installed images. Their persistent data stays in the existing directories.
Loki, Vector and UnPoller are not recreated by this component update.

Open the three names with `http://` after DNS resolves them. Verify Grafana
login, a dashboard, a Loki query, Prometheus readiness and Alertmanager status.
Check access from an allowed LAN client and the VPN. Verify that a DMZ client
cannot reach the UIs; DNS knowledge alone does not grant network access.
Source-address filtering uses the actual connection peer and ignores supplied
forwarding headers. Proxy access logs omit query strings, cookies and credentials.

## Disable access

Set `microserver_ui_enabled: false` and repeat the component apply. It removes
the custom backend URLs and stops and disables the managed proxy. The existing
loopback services remain available through owner SSH forwarding. Internal DNS
records are left for the owner to manage.
