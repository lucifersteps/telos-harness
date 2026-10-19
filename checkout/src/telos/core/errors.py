"""Shared error types."""

from __future__ import annotations


class TelosError(Exception):
    """Base error for the new Telos runtime."""


class ConfigurationError(TelosError):
    """Raised when runtime configuration cannot be loaded or interpreted."""


class ProviderError(TelosError):
    """Raised when an LLM provider cannot be created or used."""
