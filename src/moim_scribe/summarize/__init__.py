"""Pluggable summarization backends."""
from .base import Summarizer, get_summarizer

__all__ = ["Summarizer", "get_summarizer"]
