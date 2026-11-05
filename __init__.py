"""hermes-nurse — supervisor-aware service management for Hermes Agent.

The real implementation lives in the ``hermes_nurse`` subpackage. This file
exists so the plugin also loads correctly when Hermes imports the plugin
directory itself as a package (``__init__.py`` + relative imports).
"""

from __future__ import annotations

from .hermes_nurse import register  # noqa: F401
