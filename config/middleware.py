"""Real client address behind the reverse proxy."""

import ipaddress

from django.conf import settings


class ClientAddressMiddleware:
    """Set REMOTE_ADDR from X-Forwarded-For, trusting TRUSTED_PROXY_COUNT proxies.

    The trusted entry is the one that many positions from the right (the proxy
    appends, so anything to its left is client-supplied). Anything unusable
    leaves REMOTE_ADDR untouched.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        address = self._address(request.META.get("HTTP_X_FORWARDED_FOR", ""))
        if address:
            request.META["REMOTE_ADDR"] = address
        return self.get_response(request)

    @staticmethod
    def _address(header):
        count = settings.TRUSTED_PROXY_COUNT
        if count <= 0 or not header:
            return ""
        entries = [e.strip() for e in header.split(",")]
        if len(entries) < count:
            return ""
        candidate = entries[-count]
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return ""
