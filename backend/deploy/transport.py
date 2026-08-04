"""OpenSSH transport for Recipe v2, including non-interactive password auth."""

from __future__ import annotations

import os
import hashlib
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .recipe import RecipeHost


@dataclass(frozen=True)
class RemoteResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class RecipeSshRunner:
    """Execute argv-based commands without placing passwords in argv or logs."""

    def __init__(self, host: RecipeHost, known_hosts: Path, askpass_root: Path):
        self.host = host
        self.known_hosts = known_hosts.expanduser().resolve()
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        self.known_hosts.touch(mode=0o600, exist_ok=True)
        self.known_hosts.chmod(0o600)
        self.askpass_root = askpass_root.expanduser().resolve()
        self.askpass_root.mkdir(parents=True, exist_ok=True)

    @property
    def target(self) -> str:
        return f"{self.host.user}@{self.host.address}"

    def _askpass_helper(self) -> Path:
        helper = self.askpass_root / "ssh-askpass"
        if not helper.exists():
            fd, temporary = tempfile.mkstemp(prefix=".askpass-", dir=self.askpass_root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write('#!/bin/sh\nprintf "%s\\n" "$EMBODIT_RECIPE_SSH_PASSWORD"\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o700)
                os.replace(temporary, helper)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        helper.chmod(0o700)
        return helper

    def base_command(self) -> tuple[list[str], dict[str, str]]:
        control_root = Path(tempfile.gettempdir()) / f"embodit-ssh-{os.getuid()}"
        control_root.mkdir(mode=0o700, exist_ok=True)
        control_root.chmod(0o700)
        control_digest = hashlib.sha256(
            f"{self.target}:{self.host.port}:{self.known_hosts}".encode("utf-8")
        ).hexdigest()[:24]
        control_path = control_root / control_digest
        command = [
            "ssh",
            "-p",
            str(self.host.port),
            "-o",
            f"ConnectTimeout={self.host.connect_timeout_s}",
            "-o",
            f"StrictHostKeyChecking={self.host.host_key_policy}",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=300",
            "-o",
            f"ControlPath={control_path}",
        ]
        environment = os.environ.copy()
        if self.host.auth.type == "key":
            identity = Path(self.host.auth.identity_file or "").expanduser().resolve()
            if not identity.is_file():
                raise ValueError(f"SSH identity_file 不存在：{identity}")
            command.extend(
                [
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-i",
                    str(identity),
                ]
            )
        else:
            password = self.host.auth.resolved_password()
            environment.update(
                {
                    "SSH_ASKPASS": str(self._askpass_helper()),
                    "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": environment.get("DISPLAY") or "embodit:0",
                    "EMBODIT_RECIPE_SSH_PASSWORD": password or "",
                }
            )
            command.extend(
                [
                    "-o",
                    "BatchMode=no",
                    "-o",
                    "NumberOfPasswordPrompts=1",
                    "-o",
                    "PreferredAuthentications=publickey,password,keyboard-interactive",
                ]
            )
        return command, environment

    def run(
        self,
        args: list[str],
        *,
        timeout: float = 30,
        input_data: bytes | None = None,
    ) -> RemoteResult:
        base, environment = self.base_command()
        remote_command = shlex.join(args)
        try:
            completed = subprocess.run(
                [*base, "--", self.target, remote_command],
                input=input_data,
                capture_output=True,
                check=False,
                timeout=timeout,
                env=environment,
                start_new_session=self.host.auth.type != "key",
            )
            return RemoteResult(
                completed.returncode,
                completed.stdout.decode("utf-8", errors="replace"),
                completed.stderr.decode("utf-8", errors="replace"),
            )
        except subprocess.TimeoutExpired as error:
            return RemoteResult(
                124,
                _decode(error.stdout),
                _decode(error.stderr),
                timed_out=True,
            )


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def require_remote_ok(result: RemoteResult, action: str) -> RemoteResult:
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{action}失败：{output[-2000:] or 'remote command failed'}")
    return result
