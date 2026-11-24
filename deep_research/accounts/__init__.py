"""Global account layer for the multi-user Personal Digital Library.

Accounts (users, sessions, email tokens) are global state stored in a small
registry DB at ``<pdl.root_dir>/registry.db`` — deliberately separate from the
per-user library DBs (``<pdl.root_dir>/users/<user_id>/index.db``), which keep
the untouched ``StorageBackend`` schema.
"""
