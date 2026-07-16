"""Provider adapters: thin wrappers exposing each package's tools to the unified
server through a common interface. Migrated one provider per commit; until a
provider has an adapter the unified server delegates to its legacy handle_request.
"""
