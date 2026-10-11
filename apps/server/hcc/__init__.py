"""Homelab Control Center control plane.

The control plane owns all persistent state and is the only component that talks
to the database. Agents push to it; the web client reads from it.
"""

__version__ = "0.1.0"
