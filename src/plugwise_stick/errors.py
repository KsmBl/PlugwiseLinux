class PlugwiseError(Exception):
    """Base error for everything raised by this package."""


class StickError(PlugwiseError):
    """The stick itself failed (no ack, error ack, serial problem)."""


class NodeTimeout(PlugwiseError):
    """A plug did not answer (offline, unpowered, or mesh re-routing)."""
