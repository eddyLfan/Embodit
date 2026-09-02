from __future__ import annotations

import base64
import io
import time
from pathlib import Path

import av
from PIL import Image

from deploy.recording import DeploymentVideoRecorder


def _camera(label: str, color: tuple[int, int, int]) -> dict[str, object]:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 240), color).save(buffer, format="JPEG", quality=85)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "key": label,
        "label": label,
        "dataUrl": f"data:image/jpeg;base64,{encoded}",
        "width": 320,
        "height": 240,
    }


def test_server_side_recording_writes_playable_mp4(tmp_path: Path) -> None:
    recorder = DeploymentVideoRecorder(
        tmp_path / "outputs" / "deployment-recordings",
        deployment_id="astribot-s1--pi05-215000",
        orchestration_id="0123456789abcdef",
        segment=1,
        frame_rate=8,
        camera_key="head",
        camera_label="Head camera",
    )
    recorder.start()
    preview = {
        "capturedMonotonicNs": 1,
        "cameras": [
            _camera("left-wrist", (40, 180, 80)),
            _camera("head", (220, 40, 40)),
            _camera("right-wrist", (40, 90, 220)),
        ],
    }
    recorder.submit(preview)
    deadline = time.monotonic() + 5
    while recorder.snapshot()["frames"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    status = recorder.stop()

    assert status["status"] == "saved"
    assert status["frames"] >= 1
    assert status["cameraKey"] == "head"
    assert status["cameraLabel"] == "Head camera"
    path = Path(status["path"])
    assert path.parent == (tmp_path / "outputs" / "deployment-recordings").resolve()
    assert path.suffix == ".mp4"
    assert path.stat().st_size > 0
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height) == (1280, 720)
        frame = next(container.decode(video=0), None)
        assert frame is not None
        red, green, blue = frame.to_image().getpixel((640, 360))
        assert red > 150
        assert green < 100
        assert blue < 100


def test_recording_skips_duplicate_preview_frames(tmp_path: Path) -> None:
    recorder = DeploymentVideoRecorder(
        tmp_path,
        deployment_id="demo",
        orchestration_id="fedcba9876543210",
        segment=2,
        frame_rate=8,
    )
    recorder.start()
    preview = {"capturedMonotonicNs": 9, "cameras": [_camera("head", (20, 30, 40))]}
    recorder.submit(preview)
    recorder.submit(preview)
    deadline = time.monotonic() + 5
    while recorder.snapshot()["frames"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    status = recorder.stop()

    assert status["status"] == "saved"
    assert status["frames"] == 1
