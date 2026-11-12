# Workstation telemetry receiver

The optional receiver sends workstation metrics and logs to the NAS's existing
Prometheus and Loki services. It adds a pinned Tempo service for traces and a
Tempo datasource in Grafana. Enable the receiver on a commissioned NAS before
changing workstation collectors.

The NAS address and workstation address must be distinct private IPv4 addresses.
The workstation must have a persistent route to the NAS that selects the
configured workstation source address. Keep the existing management and K3s
addresses during this change.

## Enable the NAS component

Retain the existing monitoring and intranet UI variables in the private
configuration. Add these fields, using the reviewed addresses:

```yaml
microserver_telemetry_enabled: true
microserver_telemetry_client_address: '192.168.50.11'
```

The owner runs this component entry point:

```bash
ansible-playbook -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml \
  -e '{"microserver_owner_install": true}' --ask-become-pass \
  ansible/owner-telemetry.yml
```

The apply requires the commissioned monitoring and proxy services. It refuses
changes to existing Compose services or existing UI routes. It fetches only the
pinned Tempo image when absent, validates the Tempo configuration, starts Tempo,
and recreates Grafana to load its datasource. Prometheus, Loki, Vector and
UnPoller continue running. Storage, NFS and workstation configuration tasks are
outside this component.

Keep the telemetry variables and `microserver_ui_enabled: true` in subsequent
full or UI applies. The installed-component checks reject missing flags. An
older frozen UI launcher does not contain this component and must not be reused
after receiver installation.

## Access and capacity

Nginx listens on the selected NAS address at TCP 8080. It permits only the exact
workstation source address. Forwarding headers do not change this check. All
backends retain their loopback listeners. This profile uses HTTP on the trusted
intranet, like the existing UI profile. Do not add WAN forwarding or broaden the
receiver allowlist to untrusted networks.

| Receiver path | Method | Backend |
| --- | --- | --- |
| `/ready` | GET | Tempo readiness |
| `/api/v1/write` | POST | Prometheus remote write |
| `/loki/api/v1/push` | POST | Loki log push |
| `/otlp/v1/logs` | POST | Loki OTLP logs |
| `/v1/traces` | POST | Tempo OTLP HTTP traces |
| `/metrics/prometheus` | GET | Prometheus self metrics |
| `/metrics/loki` | GET | Loki self metrics |
| `/metrics/tempo` | GET | Tempo self metrics |
| `/metrics/grafana` | GET | Grafana self metrics |
| `/push.v1.PusherService/Push` | POST | Pyroscope profile push (profiles option only) |
| `/metrics/pyroscope` | GET | Pyroscope self metrics (profiles option only) |

Other paths return HTTP 404. Disallowed methods on these routes return HTTP 403.
The four metrics routes let workstation Alloy retain its existing dashboard job
labels after the backend move. Request bodies are limited to 4 MiB.

Tempo uses at most 768 MiB RAM and 0.25 CPU. Its state remains on the NAS OS SSD
under `/var/lib/microserver/tempo`, with 72-hour trace retention, a 1 MiB trace
limit and a 1 MiB/s ingestion limit. Retention is not a disk quota. Check free
space and measured use before increasing ingestion or retention. The local
fixture verified trace readback; this Tempo version also logged idle scheduler
messages about missing jobs. Check live errors and trace readback during
qualification instead of treating readiness alone as proof of trace storage.

## Optional profiles backend

The receiver can also store continuous profiles from the workstation's
host-wide eBPF collector in a pinned Pyroscope 2.4.3 service, with a Pyroscope
datasource in Grafana. Enable it on a commissioned receiver by keeping the
telemetry variables and adding:

```yaml
microserver_telemetry_profiles_enabled: true
```

Then rerun the same `ansible/owner-telemetry.yml` command. The component checks
that ports 4040, 9097, 9099 and 7947 are free, fetches only the pinned image,
installs `observability/config/pyroscope.yaml` and the datasource, starts only
Pyroscope, recreates Grafana and adds the two profile routes. Every Pyroscope
listener stays on loopback, and its rings advertise 127.0.0.1. Pyroscope uses at
most 768 MiB RAM and 0.25 CPU, keeps profiles for 72 hours under
`/var/lib/microserver/pyroscope`, and accepts 1 MB/s (2 MB burst). Pyroscope has
no configuration check command, so confirm `http://127.0.0.1:4040/ready` and a
profile query in Grafana after the first apply.

Once installed, keep the flag for later full, monitoring and UI applies; they
refuse to render without it rather than drop the service or routes. The
workstation verifier requires the Pyroscope datasource whenever its
provisioning file is present.

## Qualify and cut over

1. Verify readiness and ingestion from the workstation's configured source
   address. Use synthetic log and trace records without credentials or user
   content. Read the synthetic records back from the NAS.
2. Verify that another LAN client receives HTTP 403 on `/ready`. Confirm that
   the three existing UI names still work.
3. Back up the workstation collector configuration and record backend replica
   counts. Preserve its current privacy filters, labels and collector state.
4. Change collector destinations and the specific egress policy for NAS TCP
   8080. Verify fresh workstation samples, filtered logs and traces on the NAS.
5. After the new path passes, stop the redundant workstation Grafana,
   Prometheus, Loki and Tempo deployments. Retain their persistent volumes and
   rollback configuration. Keep Alloy, node exporter, kube-state-metrics and
   the host collection timers.

Receiver installation alone does not migrate historical data, dashboards or
alerts. Qualify these separately before retiring their workstation copies.
Resolve collector startup at boot before declaring the workstation ready for
a reboot; an active but disabled K3s service will not start its collectors.

## Load workstation alerts and verify dashboards

After receiver commissioning, retain its private variables and set:

```yaml
microserver_workstation_rules_enabled: true
```

The owner runs the NAS presentation component:

```bash
ansible-playbook -i /path/to/private/inventory.ini --limit nas \
  -e @/path/to/private/microserver.yml \
  -e '{"microserver_owner_install": true}' --ask-become-pass \
  ansible/owner-workstation.yml
```

The component adds 41 workstation recording and alert rules, including build-pipeline, serving, gateway,
assistant-tool and host-maintenance alerts. Host queries use
`job="node"`; NAS host metrics use `job="microserver-node"`. This separation
lets a missing workstation trigger its availability alert even while the NAS
continues reporting healthy memory metrics. Rule evaluation failures are
observed through the NAS's own Prometheus scrape.

A configuration guard permits only the new rule file reference and one
read-only Prometheus bind mount. Existing services and other Prometheus settings
must match the reviewed source. The component recreates only NAS Prometheus,
retaining its database. Collectors can buffer samples during the brief restart.

The eight workstation dashboards retain their UIDs. This component installs
them from `observability/dashboards/`, keeping a backup of any file it
replaces, so a dashboard change needs only this component, not a full apply.
Installed files must be regular root-owned `0644` files. Verification uses
a read-only Grafana SQLite connection to select active dashboard UID/title
and datasource UID/type/URL metadata for the expected objects. Grafana 13 uses
its unified `resource` store for dashboards; its legacy `dashboard` table can
be empty. A temporary view extracts only the selected public fields inside
SQLite. The view excludes deleted or trashed dashboards and other organizations.
A SQLite authorizer rejects direct resource payload reads, credential columns
and writes. Grafana login remains required for its UI and authenticated API.

The component verifies all workstation rules have evaluated successfully.
Firing availability alerts are expected while the workstation is offline.
Existing Alertmanager routing receives the new alerts. Retain this flag and
the receiver flags for subsequent full or UI applies.

After this component passes, confirm fresh workstation data again before
retiring the old servers. This procedure leaves their services and volumes
in place. Workstation reachability and collector boot startup remain separate
acceptance checks.

Retire the redundant servers in a separate owner operation by setting the
existing Grafana, Prometheus, Loki and Tempo Deployment replica counts to zero.
Retain all five PVCs, Services, configuration and the confirmed collector
transaction. Keep Alloy, node exporter, kube-state-metrics and host timers
running. Verify new metrics, filtered logs and traces on the NAS after shutdown
before confirming retirement. Preserve the original replica counts for recovery.

Zero desired counts persist in K3s. Earlier local `full` or `metrics` renders
would restore local servers and collector destinations; review future applies
against the accepted NAS topology. Retirement retains historical workstation
data on its original volumes and does not migrate that history or reclaim disk
space. Collector startup at boot requires separate verification.

If this workstation-rules component fails, retain its log and printed
configuration backup filenames. Configuration drift needs review before retry.
If Prometheus fails to start, the owner can restore the Compose and Prometheus
configuration backups, validate them, and recreate only Prometheus. Keep its
database, the rule file and the existing dashboard files. A metadata-only
verification failure requires diagnosis of that check; it does not imply that
the receiver or storage installation failed.

After correcting a verification failure, the owner can add
`--tags workstation_verify` to the same workstation playbook command. This mode
retains the target, path and dashboard file checks, updates only the two validator
scripts, and verifies the database metadata and evaluated rules. It does not
install rules, change Compose or Prometheus configuration, or restart services.

## Assistant tool metrics

Workstation Alloy scrapes the assistant `service-tools` process at
`service-tools.ai-home-lab.svc:8000/metrics` as `job="assistant-tools"` and
writes the samples through the receiver like other workstation metrics. Every
label comes from a fixed vocabulary, so request text, URLs, passages and user,
project or session identifiers never become labels. The families count tool
outcomes; a successful tool call is not evidence that the user's task
succeeded, and no panel or rule presents it that way.

The **Workstation / Workloads & serving** dashboard (`workstation-workloads`)
has an **Assistant tools** row: the serving contract the clients were rendered
for, reranker state, the retrieval error ratio, retrieval outcomes and latency,
web/handoff/memory operations, and retrieval without the configured reranker.
The workstation repository keeps a byte-identical copy of this dashboard for its
local Grafana; change both together.

The workstation rules add five assistant entries:

| Rule | Severity | Fires when |
| --- | --- | --- |
| `workstation:assistant_retrieval_error_ratio:rate15m` | recording | Share of retrieval calls ending in `error` or `unavailable` |
| `WorkstationAssistantRetrievalErrors` | warning | That share exceeds 20% with at least 5 calls in 15 minutes, for 10 minutes |
| `WorkstationAssistantRerankerNotActive` | warning | The reranker reports `missing_assets`, `incompatible` or `unknown` for 15 minutes; `disabled` is a profile choice and does not fire |
| `WorkstationAssistantHandoffConflicts` | info | More than 5 handoff saves were refused as stale in 30 minutes |
| `WorkstationAssistantServingContractIncomplete` | info | Clients have been rendered for an incomplete serving contract for 10 minutes |

The rules see data only after the workstation renders service-tools with the
metrics route and the collector policy that admits Alloy to port 8000. Until
then the panels show their no-value text and the rules stay inactive. Load the
rules and dashboard with the `ansible/owner-workstation.yml` component above.

## Host maintenance metrics

The workstation's host maintenance jobs (its `docs/HOST-MAINTENANCE.md`) write
node-exporter textfile series, so they arrive with `job="node"`: NVMe SMART
health (`workstation_nvme_*`, `workstation_smart_*`), rows recorded by
rasdaemon (`workstation_ras_events_recorded`), the full security-advisory scan
(`workstation_advisory_*`, `workstation_update_pending_packages`), the
report-only retention inventory (`workstation_retention_*`) and each job's last
result and resource use (`workstation_maintenance_job_*`, labelled `task`, so the
scrape's `job` label stays `node`). Labels are fixed vocabularies, drive
serials, unit names and installed package names; no paths, CVE text or command
output. Host Alloy also forwards classified smartd, rasdaemon and job-result
lines as `job="workstation-maintenance"` streams with templated messages.

The **Workstation / Host & storage** dashboard (`workstation-host`) has a
**Maintenance & hardware health** row. Backup freshness reads
`workstation_maintenance_backup_configured` as well as the backup success
time, so a workstation without backups shows **Not configured** rather than an
empty or healthy panel. The workstation repository keeps byte-identical copies
of `host.json` and `monitoring.json`; change both together.

The workstation rules add twelve maintenance entries:

| Rule | Severity | Fires when |
| --- | --- | --- |
| `WorkstationSSDHealthFailing` | critical | A drive fails its SMART self-assessment or reports a critical warning |
| `WorkstationSSDErrorsIncreasing` | warning | Media errors or error-log entries grew within an hour |
| `WorkstationSSDTemperatureHigh` | warning | A drive is at or above 70 C for 15 minutes |
| `WorkstationSSDTemperatureCritical` | critical | A drive is at or above 80 C for 5 minutes (its own warning threshold is 84 C) |
| `WorkstationSSDWearHigh` | warning | 80% of rated endurance used, or spare within 10 points of its threshold |
| `WorkstationHardwareHealthSampleStale` | warning | The SMART/RAS sampler is missing, older than 30 minutes or cannot read a drive |
| `WorkstationHardwareErrorsRecorded` | warning | rasdaemon recorded a new event, or EDAC counted a corrected error |
| `WorkstationHardwareErrorsUncorrected` | critical | An uncorrected or fatal event was recorded |
| `WorkstationMonitoringDaemonInactive` | warning | smartd, rasdaemon or a maintenance timer is inactive, or the RAS database is unreadable, for 30 minutes |
| `WorkstationAdvisoryScanFailing` | warning | The last advisory scan failed, or none succeeded for 3 days |
| `WorkstationAdvisoryFixAvailable` | info | An installed package's advisory records a fixed version |
| `WorkstationMaintenanceJobFailed` | warning | A maintenance job's last run failed |

Alerts reach the existing Alertmanager, whose default receiver is local-only:
they are visible in its UI but no notification is sent until the owner adds a
reviewed receiver. Load the rules and dashboards with
`ansible/owner-workstation.yml` as above.

## Recover from a failed receiver apply

Keep the apply log and inspect the failed task before retrying. Drift refusal
requires review of the existing configuration. Do not replace that configuration
to force validation to pass. A failed proxy reload can leave the previous
configuration running; compare readiness with the task result.

Ansible creates timestamped backups beside existing configuration files before
replacing them. For owner rollback, stop only Tempo using the installed Compose
file, restore the recorded Compose and Nginx backups, and move the new Tempo
datasource file into a protected backup directory outside Grafana provisioning.
Validate restored Compose and Nginx configuration before recreating only Grafana
and reloading `microserver-ui.service`. Keep the Tempo state directory. Do not
run Compose `down`, delete volumes or rerun OS/storage installation.
