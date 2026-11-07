# Private HTTPS for the monitoring UIs

The NAS can obtain and renew a Let's Encrypt wildcard for its internal UI DNS
suffix through Cloudflare DNS-01. Internal A records continue to point to the
private NAS address. Certificate validation uses public DNS TXT records; it
does not require an inbound internet web listener.
See [Let's Encrypt DNS-01](https://letsencrypt.org/docs/challenge-types/#dns-01-challenge).

For example, `*.lan.example.net` covers `grafana.lan.example.net`,
`prometheus.lan.example.net` and `alertmanager.lan.example.net`. A certificate
for `*.example.net` does not cover those deeper names. The existing UniFi/VPN
certificate and its activation remain separate from the NAS certificate.

## Owner commissioning

Use the reviewed owner launcher for the commissioned NAS. Its first phase runs
`microserver-ui-acme.sh` on that NAS through the pinned SSH identity. It checks
the hostname, Debian version, private address, current services and reviewed
HTTP configuration before installing the signed Debian `certbot` and
`python3-certbot-dns-cloudflare` packages. These packages provide issuance and
renewal; no additional Python package manager or nginx plugin is installed.

Create a Cloudflare API token with `Zone:DNS:Edit`, restricted to the actual
public zone containing the UI suffix. Enter it at the hidden NAS terminal
prompt. Enter the ACME contact email at its separate hidden prompt. The owner
workflow accepts the CA terms during account registration. Do not put the token
in inventory, a command argument, Git or chat. The Certbot plugin documents its
[scoped token requirements](https://certbot-dns-cloudflare.readthedocs.io/en/stable/).

The NAS stores the token in `/etc/letsencrypt/cloudflare.ini` and the contact
setting in `/etc/letsencrypt/microserver-ui-account.ini`, both root-owned mode
0600. Existing files must already have those protections; the helper does not
print or overwrite their contents. Owner credential rotation uses the protected
NAS workflow. The certificate lineage is `/etc/letsencrypt/live/microserver-ui`.
Private keys and the DNS token remain on the NAS.

The second phase runs `ansible/owner-tls.yml` with the protected commissioning
configuration and an explicit profile containing `microserver_ui_tls_enabled:
true`. Certificate validation checks system trust, expiry, all three hostnames,
the key pair and the expected Certbot archive paths before changing listeners.
Current nginx configuration must match the reviewed HTTP configuration or the
current canonical HTTPS render. The Compose guard permits only external UI
URL settings and secure Grafana cookies to change.

The play retains public configuration backups, starts the HTTPS proxy and
updates only Prometheus, Grafana and Alertmanager for their external URLs. Their
data remains in place. Local checks verify the current certificate is served,
the three HTTPS health endpoints work, HTTP redirects are correct, and Grafana's
user API still returns 401 without authentication. Controller checks verify
trusted HTTPS and normal DNS resolution to the exact NAS address.

If qualification fails, the play restores the previous proxy and Compose
configuration, restores the previous credential drop-in or removes only the
new one, and restores the three UI services. Retain the printed backup directory
and owner log. The issued certificate and protected DNS credential remain on
the NAS for a retry. A rollback failure requires owner recovery from that
retained directory; do not delete service data or volumes.

## Runtime and renewal

nginx continues to run as `www-data` with its existing service limits and
systemd isolation. `LoadCredential` supplies a private per-service copy of the
certificate and key. It grants no access to the Cloudflare credential. Root
configuration validation references the same protected Certbot files and uses
`pid /dev/null;`, so it cannot recreate the earlier root-owned runtime PID bug.

HTTPS binds only the selected private address on TCP 443. TLS 1.2 and 1.3 are
enabled. Unknown SNI hostnames are rejected. Known HTTP hosts redirect to their
fixed HTTPS names only after an actual-peer allowlist check. The proxy retains
its source allowlist, replaces forwarded client headers, and preserves the
existing workstation ingestion endpoint, paths and source restrictions.

The packaged `certbot.timer` renews the lineage using its saved DNS plugin
configuration. A deployment hook runs only for `microserver-ui`. It validates
the renewed files, restarts nginx to refresh its systemd credential snapshot,
then verifies that nginx presents the current leaf certificate. An nginx HUP
alone would retain the previous credential snapshot.

Commissioning runs a renewal dry run with the real deployment hook, using the
active production certificate as documented by
[Certbot](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates).
This validates DNS renewal and briefly restarts the proxy. A successful initial
issuance alone is not treated as renewal acceptance.

Retain the HTTPS flag and full commissioned profile for later monitoring or UI
applies. An older profile that omits the flag is refused before those paths can
render HTTP URLs. A full apply cannot commission TLS for the first time; use
the scoped owner workflow first.

## Preserve the proxy runtime identity during validation

The service runs as `www-data`. Root nginx configuration checks must also
specify `-g 'user www-data; pid /dev/null;'`. A configuration check can change
temporary-directory ownership. Without an explicit user, the reviewed Debian
binary uses `nobody`, which prevents the running service from buffering large
responses to disk. Health endpoints can still return HTTP 200 while JavaScript
downloads stop partway through.

The TLS verifier, telemetry guard and HTTP template validator specify the actual
service identity. An isolated Debian regression test first reproduces the
permission failure, then verifies complete 4 MiB delivery through the same
running worker after validation restores ownership.

For an affected commissioned NAS, the owner workflow
`ansible/owner-ui-runtime-repair.yml` requires reviewed configuration and validator
checksums. It backs up and updates the two installed public validators and
restores only the five known temporary-directory owners and modes. It does not
recurse through buffered files or restart a service. Acceptance compares the
complete Grafana asset length and SHA-256 digest through the backend and trusted
HTTPS, checks the existing UI health and authentication restrictions, and
requires the proxy master PID to remain unchanged.
