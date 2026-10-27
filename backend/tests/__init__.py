"""Test package. The no-tunnel create guard needs a live Marzban inbound list,
which the suite's fake Marzban does not have - tests/test_tunnel_defaults.py
turns it back on explicitly."""
import os

os.environ.setdefault("TUNNEL_DEFAULT_GUARD", "0")
