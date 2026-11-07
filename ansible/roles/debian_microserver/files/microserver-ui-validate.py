"""Validate public intranet proxy inputs before rendering Nginx configuration."""
import ipaddress
import json
import re
import sys


def validate(config):
    private = [ipaddress.IPv4Network(value) for value in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if not isinstance(config['address'], str):
        raise ValueError('UI listener must be an IPv4 address string')
    address = ipaddress.IPv4Address(config['address'])
    if not any(address in network for network in private):
        raise ValueError('UI listener must be an exact RFC1918 IPv4 address')
    domain = config['domain']
    if (not isinstance(domain, str) or len(domain) > 230 or not re.fullmatch(
            r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', domain)):
        raise ValueError('UI domain must be a lowercase DNS suffix')
    networks = config['networks']
    if not isinstance(networks, list) or not 1 <= len(networks) <= 16:
        raise ValueError('Specify between one and sixteen trusted private networks')
    for value in networks:
        network = ipaddress.IPv4Network(value, strict=True)
        if (str(network) != value or not any(network.subnet_of(parent) for parent in private)
                or network.num_addresses > 65536):
            raise ValueError('UI access requires canonical private IPv4 CIDRs of /16 or narrower')
    if not any(address in ipaddress.IPv4Network(value) for value in networks):
        raise ValueError('Include the NAS address for local proxy readiness checks')


if __name__ == '__main__':
    try:
        validate(json.loads(sys.argv[1]))
    except (ValueError, TypeError, KeyError, IndexError):
        raise SystemExit('Invalid intranet UI address, DNS suffix or trusted IPv4 networks') from None
