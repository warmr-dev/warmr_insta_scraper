"""Instagram Stories Monitor.

Watches ~57,000 Instagram accounts for newly posted stories via ~20 worker accounts
that collectively follow all targets. Each worker polls `feed/reels_tray/` once -
one request covering thousands of monitored targets.
"""

__version__ = "0.1.0"
