"""Server-side video recording for real-robot deployment sessions."""

from __future__ import annotations

import base64
import io
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any


_STOP = object()


class DeploymentVideoRecorder:
    """Encode the independent live camera preview without blocking control."""

    def __init__(
        self,
        output_root: Path,
        *,
        deployment_id: str,
        orchestration_id: str,
        segment: int,
        frame_rate: float,
        camera_key: str | None = None,
        camera_label: str | None = None,
        width: int = 1280,
        height: int = 720,
    ) -> None:
        self.output_root = output_root.resolve()
        self.deployment_id = deployment_id
        self.orchestration_id = orchestration_id
        self.segment = segment
        self.frame_rate = min(30.0, max(1.0, float(frame_rate)))
        self.camera_key = camera_key.strip() if isinstance(camera_key, str) and camera_key.strip() else None
        self.camera_label = (
            camera_label.strip()
            if isinstance(camera_label, str) and camera_label.strip()
            else self.camera_key
        )
        self.width = width - (width % 2)
        self.height = height - (height % 2)
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue(maxsize=3)
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._last_capture_ns: int | None = None
        self._status = "idle"
        self._path: Path | None = None
        self._started_ns: int | None = None
        self._finished_ns: int | None = None
        self._frames = 0
        self._dropped_frames = 0
        self._error: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("录制器已经启动")
            self.output_root.mkdir(parents=True, exist_ok=True)
            self.output_root.chmod(0o700)
            safe_deployment = re.sub(r"[^A-Za-z0-9._-]+", "-", self.deployment_id).strip("-._") or "deployment"
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            filename = (
                f"{safe_deployment}--{timestamp}--{self.orchestration_id[:8]}"
                f"--segment-{self.segment:03d}.mp4"
            )
            self._path = self.output_root / filename
            self._started_ns = time.time_ns()
            self._status = "recording"
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name=f"deployment-recording-{self.orchestration_id[:8]}",
            )
            self._thread.start()

    def submit(self, preview: dict[str, Any]) -> None:
        captured_ns = preview.get("capturedMonotonicNs")
        if not isinstance(captured_ns, int):
            return
        with self._lock:
            if self._status != "recording" or captured_ns == self._last_capture_ns:
                return
            self._last_capture_ns = captured_ns
        try:
            self._queue.put_nowait(preview)
        except queue.Full:
            # Keep control/telemetry responsive.  Dropping a stale video frame is
            # preferable to blocking the deployment monitor or action loop.
            with self._lock:
                self._dropped_frames += 1

    def stop(self, *, timeout_s: float = 30.0) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            if thread is None:
                return self.snapshot()
            if self._status == "recording":
                self._status = "finalizing"
        self._queue.put(_STOP)
        if thread is not threading.current_thread():
            thread.join(timeout=timeout_s)
        with self._lock:
            if thread.is_alive():
                self._status = "error"
                self._error = "MP4 封装超时"
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "status": self._status,
                "directory": str(self.output_root),
                "path": str(self._path) if self._status == "saved" and self._path is not None else None,
                "startedNs": self._started_ns,
                "finishedNs": self._finished_ns,
                "frameRate": self.frame_rate,
                "cameraKey": self.camera_key,
                "cameraLabel": self.camera_label,
                "frames": self._frames,
                "droppedFrames": self._dropped_frames,
                "error": self._error,
            }

    def _run(self) -> None:
        path = self._path
        if path is None:
            return
        part_path = path.with_suffix(path.suffix + ".part")
        container = None
        stream = None
        try:
            import av

            container = av.open(str(part_path), mode="w", format="mp4", options={"movflags": "+faststart"})
            codec = "libx264" if "libx264" in av.codecs_available else "mpeg4"
            stream = container.add_stream(codec, rate=round(self.frame_rate))
            stream.width = self.width
            stream.height = self.height
            stream.pix_fmt = "yuv420p"
            stream.bit_rate = 6_000_000

            while True:
                value = self._queue.get()
                if value is _STOP:
                    break
                image = self._compose(value)
                if image is None:
                    continue
                frame = av.VideoFrame.from_image(image)
                for packet in stream.encode(frame):
                    container.mux(packet)
                with self._lock:
                    self._frames += 1

            if self._frames <= 0:
                raise RuntimeError("未收到可录制的相机画面")
            for packet in stream.encode():
                container.mux(packet)
            container.close()
            container = None
            part_path.replace(path)
            path.chmod(0o600)
            with self._lock:
                self._status = "saved"
                self._finished_ns = time.time_ns()
        except Exception as error:  # noqa: BLE001
            if container is not None:
                try:
                    container.close()
                except Exception:  # noqa: BLE001
                    pass
            part_path.unlink(missing_ok=True)
            with self._lock:
                self._status = "error"
                self._error = str(error)
                self._finished_ns = time.time_ns()

    def _compose(self, preview: dict[str, Any]):
        from PIL import Image, ImageOps

        selected = None
        for camera in (preview.get("cameras") or [])[:8]:
            if not isinstance(camera, dict):
                continue
            if self.camera_key is not None and camera.get("key") != self.camera_key:
                continue
            selected = camera
            break
        if selected is None:
            return None
        data_url = selected.get("dataUrl")
        if not isinstance(data_url, str) or "," not in data_url:
            return None
        header, encoded = data_url.split(",", 1)
        if not header.startswith("data:image/") or ";base64" not in header:
            return None
        try:
            payload = base64.b64decode(encoded, validate=True)
            image = Image.open(io.BytesIO(payload)).convert("RGB")
        except Exception:  # noqa: BLE001
            return None
        if self.camera_key is None:
            self.camera_key = str(selected.get("key") or "") or None
        if self.camera_label is None:
            self.camera_label = str(selected.get("label") or self.camera_key or "主视角")

        canvas = Image.new("RGB", (self.width, self.height), "#000000")
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        fitted = ImageOps.contain(image, (self.width, self.height), method=resampling)
        canvas.paste(
            fitted,
            ((self.width - fitted.width) // 2, (self.height - fitted.height) // 2),
        )
        return canvas
