#!/usr/bin/env bash
# Owner-run only: DNS credentials and private keys remain on the NAS.
set -euo pipefail
set +x
umask 077
export LC_ALL=C SYSTEMD_PAGER=cat
[[ $# == 3 && $EUID == 0 && -t 0 ]] || {
  printf 'Run the reviewed owner launcher in a terminal.\n' >&2
  exit 1
}
domain=$1
address=$2
expected_nginx=$3
[[ $domain =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*$ && $address =~ ^192\.168\.[0-9]{1,3}\.[0-9]{1,3}$ && $expected_nginx =~ ^[a-f0-9]{64}$ ]] || exit 1
[[ $(hostname -s) == ig0r && $(dpkg --print-architecture) == amd64 ]] || exit 1
# shellcheck source=/dev/null
. /etc/os-release
[[ $ID == debian && $VERSION_ID == 13 ]] || exit 1
systemctl is-active --quiet microserver-ui.service
systemctl is-active --quiet microserver-monitoring.service
ip -4 -o address show | awk '{print $4}' | cut -d / -f 1 | grep -Fxq "$address"
if [[ ! -e /etc/systemd/system/microserver-ui.service.d/tls.conf ]]; then
  [[ $(sha256sum /etc/microserver/ui/nginx.conf | cut -d ' ' -f 1) == "$expected_nginx" ]] || {
    printf 'STOP: Existing proxy configuration differs from the reviewed source.\n' >&2
    exit 1
  }
fi
trusted_directory() {
  [[ -d $1 && ! -L $1 && $(stat -c %u "$1") == 0 ]] || exit 1
  local mode
  mode=$(stat -c %a "$1")
  (((8#$mode & 8#022) == 0)) || exit 1
}
trusted_directory /run
work=$(mktemp -d /run/microserver-acme.XXXXXXXX)
log="$work/owner-acme.log"
printf 'PRIVATE_ACME_LOG=%s\n' "$log"
if ! command -v certbot >/dev/null || ! dpkg-query -W -f='${db:Status-Status}' python3-certbot-dns-cloudflare 2>/dev/null | grep -Fxq installed; then
  printf 'Installing the Debian Certbot client and Cloudflare plugin from configured signed repositories.\n'
  if ! timeout 180 apt-get update >"$log" 2>&1 || ! DEBIAN_FRONTEND=noninteractive timeout 180 apt-get --yes --no-install-recommends install certbot python3-certbot-dns-cloudflare >>"$log" 2>&1; then
    printf 'STOP: Package installation failed; retain the private NAS log.\n' >&2
    exit 1
  fi
fi
trusted_directory /etc/letsencrypt
for directory in /etc/letsencrypt/renewal-hooks /etc/letsencrypt/renewal-hooks/deploy; do
  [[ -e $directory ]] || install -d -m 0755 "$directory"
  trusted_directory "$directory"
done
credential=/etc/letsencrypt/cloudflare.ini
if [[ -e $credential || -L $credential ]]; then
  [[ -f $credential && ! -L $credential && $(stat -c %u:%a "$credential") == 0:600 && -s $credential ]] || exit 1
else
  printf 'Use a Cloudflare API token restricted to DNS edits for the zone containing %s.\n' "$domain"
  IFS= read -r -s -p 'Cloudflare API token (hidden; stored only on this NAS): ' token </dev/tty
  printf '\n' >/dev/tty
  [[ $token =~ ^[A-Za-z0-9_-]{20,256}$ ]] || {
    unset token
    printf 'STOP: API token format is invalid; no credential was written.\n' >&2
    exit 1
  }
  (
    set -o noclobber
    printf 'dns_cloudflare_api_token = %s\n' "$token" >"$credential"
  )
  unset token
fi
account=/etc/letsencrypt/microserver-ui-account.ini
if [[ -e $account || -L $account ]]; then
  [[ -f $account && ! -L $account && $(stat -c %u:%a "$account") == 0:600 && -s $account ]] || exit 1
else
  IFS= read -r -s -p 'ACME contact email (hidden from the session log): ' contact </dev/tty
  printf '\n' >/dev/tty
  [[ $contact =~ ^[A-Za-z0-9.!#+_%=-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || {
    unset contact
    printf 'STOP: Contact email format is invalid; no account setting was written.\n' >&2
    exit 1
  }
  (
    set -o noclobber
    printf 'email = %s\n' "$contact" >"$account"
  )
  unset contact
fi
printf 'Requesting the NAS wildcard through Cloudflare DNS validation; no inbound internet listener is needed.\n'
if ! timeout 300 certbot certonly --config "$account" --non-interactive --agree-tos \
  --dns-cloudflare --dns-cloudflare-credentials "$credential" --dns-cloudflare-propagation-seconds 60 \
  --cert-name microserver-ui --domain "*.$domain" --keep-until-expiring >>"$log" 2>&1; then
  printf 'STOP: ACME issuance failed; protected output remains in the private NAS log.\n' >&2
  exit 1
fi
printf 'NAS_CERTIFICATE=issued or retained; HTTPS_CONFIGURATION=pending\n'
