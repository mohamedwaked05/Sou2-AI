"""Trusted-proxy client address resolution."""

import ipaddress

from fastapi import Request

from app.core.config import Settings


def resolve_client_ip(request: Request, settings: Settings) -> str:
    """Resolve X-Forwarded-For only through configured trusted proxy hops."""
    peer_value = request.client.host if request.client is not None else "0.0.0.0"
    try:
        peer = ipaddress.ip_address(peer_value)
    except ValueError:
        return "0.0.0.0"

    networks = tuple(
        ipaddress.ip_network(value, strict=False)
        for value in settings.trusted_proxy_cidrs
    )
    if not networks or not any(peer in network for network in networks):
        return str(peer)

    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded:
        return str(peer)
    try:
        chain = [ipaddress.ip_address(part.strip()) for part in forwarded.split(",")]
    except ValueError:
        return str(peer)
    if not chain:
        return str(peer)

    current = peer
    for hop in reversed(chain):
        if not any(current in network for network in networks):
            return str(current)
        current = hop
    return str(current)
