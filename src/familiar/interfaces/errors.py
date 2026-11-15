"""Errors shared by the application and backend plugins."""


class BackendError(Exception):
    """A backend operation failed, possibly temporarily."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class PluginError(Exception):
    """A plugin could not be found or is incompatible."""


class ConfigError(Exception):
    """Configuration or persona data is missing or invalid."""
