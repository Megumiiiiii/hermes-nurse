"""hermes-nurse — supervisor-aware service management for Hermes Agent.

Hermes imports a plugin directory as a package, so this file must expose
``register(ctx)``. The implementation lives in the ``hermes_nurse`` subpackage;
this is the thin entry point.

The import is intentionally absolute rather than relative: the catalog scanner,
pytest's rootdir collection and the plugin loader each import this file under a
different package context, and a relative import fails under any of them that
treats the directory as a plain module.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from hermes_nurse import register  # noqa: E402,F401
