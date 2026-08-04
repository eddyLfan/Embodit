"""Minimal user-owned model adapter for Embodit.

Copy this file into the model project's remote working directory and replace
the two marked sections with the model library's native calls. Embodit imports
``my_vla:MyVLA``; this file never needs to implement an HTTP server.
"""

from __future__ import annotations

from typing import Any


class MyVLA:
    # Optional metadata for people and future compatibility checks.
    specification = {
        "action_type": "joint_position",
        "action_horizon": 8,
    }

    def load(self, checkpoint: str, **kwargs: Any) -> None:
        # Replace with the native model loader, for example:
        # self.model = YourModel.from_pretrained(checkpoint, **kwargs)
        raise NotImplementedError("replace MyVLA.load with the native checkpoint loader")

    def predict(self, observations: dict[str, Any], **kwargs: Any) -> Any:
        # observations contains the names declared in robot.client.config.observations.
        # Return a [horizon][joint_count] list, NumPy array, or Torch tensor.
        # Example:
        # return self.model.predict(
        #     image=observations["wrist_camera"],
        #     state=observations["joint_position"],
        #     **kwargs,
        # )
        raise NotImplementedError("replace MyVLA.predict with the native inference call")
