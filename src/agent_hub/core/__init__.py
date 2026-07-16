"""Shared infrastructure for agent-hub providers and the conductor.

Extracted from the previously-duplicated per-package modules. Each legacy
package keeps a thin re-export shim at its old import path so existing tests and
callers keep working unchanged while the implementation lives here once.
"""
