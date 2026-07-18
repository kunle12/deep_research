"""Personal Digital Library — artifact storage, metadata, and archival.

P10.5a: implemented — core storage + archival. SQLite default backend.
P10.5b: implemented — refresh foundation logic.
P10.6: implemented — glossary generation.
"""

from deep_research.library.writer import LibraryWriter

__all__ = ["LibraryWriter"]
