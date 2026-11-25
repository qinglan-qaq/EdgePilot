"""Session-scoped repositories; every query is tenant-filtered by default."""

from app.repositories.base import BaseRepository

__all__ = ["BaseRepository"]
