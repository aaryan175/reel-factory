from __future__ import annotations


class ReelctlError(RuntimeError):
    """Base error with a user-actionable message."""


class ContractError(ReelctlError):
    """Raised when an exact-media contract is not satisfied."""


class ExternalToolError(ReelctlError):
    """Raised when a required executable fails."""
