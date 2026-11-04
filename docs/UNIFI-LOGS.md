# UniFi logs in Grafana

## Data path and scope

The Cloud Gateway sends SIEM/syslog events to Vector on the MicroServer.
Vector parses CEF, keeps the original event and forwards it to local Loki.
Grafana's Loki datasource and the **UniFi events** dashboard provide search,
event counts and severity/application views. Prometheus monitors both services.

UniFi's SIEM export covers system, security, connectivity and administrative
events. It complements performance polling; it does not replace continuous
CPU, memory, temperature and bandwidth measurements. UnPoller remains the
planned collector for those measurements after controller/API qualification.
See [UniFi system logs and SIEM integration](https://help.ui.com/hc/en-us/articles/33349041044119-UniFi-System-Logs-SIEM-Integration)
and [UnPoller's controller support](https://unpoller.com/docs/install/controllerlogin/).

Vector is the required protocol receiver and CEF parser, not another log
database. Its stable socket source accepts raw or syslog-wrapped CEF. The
native CEF parser handles escaped fields. Malformed/non-CEF messages remain
searchable with an unknown severity instead of disappearing. Only the exact
configured sender is forwarded. See [Vector's socket source](https://vector.dev/docs/reference/configuration/sources/socket/)
and [CEF parser](https://vector.dev/docs/reference/vrl/functions/#parse_cef).

## Resource and storage policy

Loki uses a single process, filesystem storage on the SSD and TSDB schema v13.
Its container has 1 GiB RAM and a 0.2 CPU ceiling. Caches total 160 MiB,
WAL replay has a 128 MB ceiling and query concurrency is one. Vector has
256 MiB RAM, one worker and a 0.1 CPU ceiling. Both use non-root users,
read-only roots, dropped capabilities and bounded container logs.
Grafana and Prometheus each have 2 GiB ceilings; all five container memory
limits total 5.5 GiB. These limits provide headroom rather than reserving RAM.
Measure actual usage alongside OS and ZFS cache requirements.

Loki retains seven days through the compactor. The initial ingestion limit is
0.02 MB/s with a 1 MB burst. This corresponds to roughly 12 GB of raw events
per week before compression, indexes and deletion delay. It is an ingestion
budget, not a disk quota. Existing filesystem-space alerts still apply.
See [Loki filesystem storage and retention](https://grafana.com/docs/loki/latest/operations/storage/filesystem/).

Vector's 512 MiB disk buffer helps bridge downstream interruptions. UDP has no
acknowledgement or retransmission, and a full receiver/buffer can lose events.
This is useful home monitoring with bounded storage, not a lossless audit archive.
Start with security, system and Internet/outage events. Add client events after
measuring volume; per-flow traffic export needs a separate capacity review.

Vector supplies only the bounded `job`, `product` and `severity` labels. Loki
can add a bounded `service_name` label and detected log-level structured metadata.
Client
addresses, MAC addresses, usernames, event IDs and messages stay in the record
body. Grafana can inspect them at query time without creating a stream for each
client or event. Log records can contain private activity; retain them within
the existing owner-controlled Grafana and NAS access boundaries.

## Owner configuration

The profile stages Loki and Vector during monitoring setup. With
`microserver_unifi_syslog_enabled: false`, Vector exposes only loopback metrics;
there is no LAN syslog socket. Loki HTTP/gRPC and Vector's metrics endpoint
always bind to loopback. Loki uses single-tenant mode behind that local boundary.
Grafana provides the authenticated user interface.

In the ignored private variable file, enable the source and supply the exact
gateway IPv4 address and the NAS's private IPv4 address:

```yaml
microserver_unifi_syslog_enabled: true
microserver_unifi_gateway_ip: ''       # Fill with the verified gateway sender.
microserver_unifi_syslog_listen_ip: ''  # Fill with an actual NAS LAN address.
```

Empty, wildcard, subnet, loopback and public addresses are rejected. The bind
address must exist on the target. Keep these addresses stable through the
owner's network configuration. Scope UDP 1514 in the host/network firewall to
the exact gateway on the trusted LAN. The playbook preserves firewall rules.
Syslog transport needs no controller credentials; API credentials remain
separate for future performance polling.

Run the reviewed owner monitoring apply with `microserver_monitoring_enabled:
true`. Production remains an owner-run operation through `owner-install.yml`.
It preserves the Grafana credential and service data, restarts monitoring for
the new configuration and checks service/plugin readiness.

On the gateway's SIEM/system logging page, set the destination to the NAS
address, UDP port **1514**, and select the event categories. Use the gateway's
test-event control when available. No gateway settings are changed by Ansible.

To stop LAN collection later, set the syslog flag to false and repeat the owner
monitoring apply. Vector restarts without the UDP source; retained Loki data
remains available until its retention policy removes it.

## Acceptance checks

1. Confirm Loki and Vector readiness, their configured limits and the expected
   loopback listeners. UDP 1514 must bind only to the selected NAS address.
2. Send a gateway test event. Open **Infrastructure / UniFi events** in Grafana,
   or query Loki in Explore with `{job="unifi"}`. Expand a record to inspect
   `cef`, `cef_parsed`, `product` and `severity`.
3. Verify both expected CEF records and unparsed records where applicable.
   Empty results alone cannot distinguish no activity from a broken sender.
4. Confirm that records from a different sender are rejected. Monitor receiver
   error/drop counters and actual CPU, RAM and disk use before adding categories.
5. Recheck service/data persistence after a later planned reboot. Inspect
   retention after the compactor has run; configured retention is not evidence
   that expired data has already been deleted.

The controller suite validates the pinned Loki/Vector configuration, executes
CEF/filter fixtures and sends synthetic UDP events through isolated loopback
processes into Loki. It also executes the actual dashboard queries. Those
fixtures do not configure a gateway or prove real NAS delivery.

Loki is an unmodified AGPL-3.0 runtime dependency; Vector is an unmodified
MPL-2.0 receiver dependency. Their image digests and upstream release links are
recorded in `observability/versions.json`. No extra database or gateway-side
agent is installed.
