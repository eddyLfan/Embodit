"""Built-in automatic quality-control detectors."""

from .gripper import GripperDetector
from .motion import MotionDetector
from .signal_integrity import SignalIntegrityDetector
from .video import VideoDetector

BUILTIN_DETECTORS = (
    SignalIntegrityDetector(),
    MotionDetector(),
    GripperDetector(),
    VideoDetector(),
)
