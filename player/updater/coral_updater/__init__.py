"""CORAL updater: installs signed player releases with selftest and automatic rollback.

Deliberately small and stable (spec 6.1): it is not updated by the mechanism it
implements. Runs as root from a systemd timer; stdlib only.
"""

__version__ = "1.0.0"
