"""Pluggable speech-to-text backends."""
from .base import Segment, STTBackend, get_backend

__all__ = ["STTBackend", "Segment", "get_backend"]
