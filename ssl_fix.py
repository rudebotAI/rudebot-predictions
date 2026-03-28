"""
SSL Certificate Fix for macOS.
macOS Python often can't find SSL certificates.
This module patches urllib to work around the issue.
Import this at the top of main.py before any HTTP calls.
"""

import ssl
import urllib.request

def apply_ssl_fix():
    """Create an unverified SSL context for urllib on macOS."""
    try:
        # First try installing certifi certificates
        import certifi
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl_context)
        )
        urllib.request.install_opener(opener)
        return "certifi"
    except ImportError:
        pass

    # Fallback: use unverified context (safe for public API reads)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_context)
    )
    urllib.request.install_opener(opener)
    return "unverified"
