"""HTTP API surface.

Versioning strategy (ADR-006): routes live under ``/api/v1``. A new major
version is introduced as a sibling package (``v2``) only when a breaking change
is unavoidable; additive changes go into the existing version.
"""
