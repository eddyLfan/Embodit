"""Minimal vendor adapter interface used by Embodit's python_adapter Client."""


class RobotAdapter:
    def __init__(self, config):
        self.config = config
        # Import and construct the vendor SDK here.

    def start(self):
        """Optional: acquire control and start sensors. Called only in Live mode."""

    def start_observation(self):
        """Optional: start read-only sensors for adapter-backed Dry Run."""

    def observe(self):
        """Return observations, including the configured action baseline vector."""
        raise NotImplementedError

    def apply_action(self, row):
        """Apply one already-validated action row using the vendor SDK."""
        raise NotImplementedError

    def stop(self):
        """Optional: hold/stop safely and release vendor resources."""

    def stop_observation(self):
        """Optional: release sensors started by start_observation()."""
