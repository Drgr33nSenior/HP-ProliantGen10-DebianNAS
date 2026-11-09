# Container registry

The optional registry component runs [Zot](https://zotregistry.dev) on the NAS.
It is a pull-through mirror for public registries and a private store for images
you build. Clients use `https://registry.<ui-domain>`, for example
`registry.lan.thespry.family`.

| Path | Content | Access |
| --- | --- | --- |
| `docker.io/…`, `ghcr.io/…`, `registry.k8s.io/…`, `quay.io/…` | On-demand mirrors, linux/amd64 only | Anonymous pull; nobody can push |
| `local/…` | Your own images | Anonymous pull; push requires the publisher account |

A mirrored image is fetched from upstream on its first pull and served from the
NAS afterwards. Digest-pinned references keep their digests.

Browse the registry with zui, Zot's web UI, at the same address. Anyone allowed
to pull can search and inspect images; use **Log in** with the publisher account
for the same permissions as a push client. CVE scanning is not enabled, so Zot
downloads no vulnerability database.

Zot verifies cosign signatures against the public keys in
`microserver_registry_cosign_public_keys` and shows a signed image as trusted
when its signature verifies against one of them. The AI-stack build signs every
image it pushes under `local/` with the key whose public half is the workstation
repository's `infrastructure/ai-stack/signing/cosign.pub`; add that file's
contents to the owner profile. The play writes each key to
`/srv/microserver/registry/_cosign/sha256:<digest of the key>`, which Zot reads
at every verification. Zot rechecks stored signatures every 2 hours, so existing
images change to trusted within 2 hours of the play. Removing a key from the
list does not delete its file; delete it on the NAS.

## Design

- Zot runs as a hardened container (read-only root, no capabilities,
  `no-new-privileges`, 1 GiB memory, one CPU) as the non-login `zot-registry`
  account (UID/GID 2001). It listens on `127.0.0.1:5000` only.
- One blob transfer may take up to `microserver_registry_transfer_timeout_seconds`
  (30 minutes by default), set as Zot's `readTimeout`/`writeTimeout` and as the
  proxy's send/read timeouts. Zot otherwise ends every request after 60
  seconds, and on one CPU a multi-GB layer upload took longer (502 at the
  proxy, 2026-11-09).
- The intranet HTTPS proxy publishes Zot at `registry.<ui-domain>:443` (zui at
  `/`, the OCI API at `/v2/`) with the existing wildcard certificate. Its
  allowlist defaults to all RFC1918 networks
  (`microserver_registry_allowed_networks`); gateway firewall policies still
  decide which networks can reach the NAS. UI hosts keep their own allowlist.
- Blobs are stored on the `nas/registry` dataset (`/srv/microserver/registry`,
  lz4, 1M records, 1 TiB quota by default), with deduplication and garbage
  collection enabled. The dataset is not exported over NFS.
- The image is pinned by digest in `microserver_registry_image`. Zot does not
  publish signatures for it on GHCR, and its binary differs from the separately
  built release asset; the trust basis is the same as the other digest-pinned
  NAS images.

## Owner commissioning

HTTPS must already be commissioned ([INTRANET-TLS.md](INTRANET-TLS.md)).

1. Add an internal DNS A record `registry.<ui-domain>` for the NAS address.
2. Create the publisher credential on the NAS. Enter the password only at the
   hidden prompt; it is never stored in Git, inventory or logs:

   ```sh
   sudo apt-get install --no-install-recommends apache2-utils
   sudo htpasswd -B -C 12 -c /etc/microserver/private/registry-htpasswd publisher
   sudo chown root:2001 /etc/microserver/private/registry-htpasswd
   sudo chmod 0440 /etc/microserver/private/registry-htpasswd
   ```

3. Run `ansible/owner-registry.yml` with the latest owner profile plus
   `microserver_registry_enabled: true`. The play creates the dataset and
   account, starts `microserver-registry.service`, re-renders the HTTPS proxy
   with the registry host, then verifies trusted HTTPS, an anonymous mirrored
   pull, refusal of anonymous pushes and the zui page.

To rotate the publisher password, rerun the `htpasswd` command without `-c`.

## Clients

K3s nodes use `/etc/rancher/k3s/registries.yaml` mirrors that rewrite each
upstream to its prefix; containerd falls back to the upstream when the NAS is
unavailable. To publish an image:

```sh
podman login registry.lan.thespry.family   # user: publisher
podman push my-image registry.lan.thespry.family/local/my-image:1.0
```

## Operations

- Status: `systemctl status microserver-registry.service`; logs:
  `docker logs microserver-registry-zot-1`.
- Capacity: `zfs list nas/registry`. Raise the quota with the owner profile
  value `microserver_registry_quota` and rerun the play.
- Mirrored content is a cache and can be re-fetched; images under `local/` are
  not. Include `nas/registry` in the backup design before relying on it.
