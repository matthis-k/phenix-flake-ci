"""Managed local process fixtures for declarative runtime tests.

This module owns only service lifecycle. The existing test runner owns
commands, assertions, references and evidence.
"""
import os
from pathlib import Path
import re
import signal
import subprocess
import time


class ServiceError(Exception):
    pass


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def validate(socket_names, services, validate_command, validate_references):
    if not isinstance(socket_names, list) or len(socket_names) != len(set(
            name for name in socket_names if isinstance(name, str))):
        raise ValueError("sockets must be a list of unique names")
    if not all(isinstance(name, str) and _NAME.fullmatch(name) for name in socket_names):
        raise ValueError("socket names must start with a letter and be alphanumeric, underscore or hyphen")
    if not isinstance(services, dict):
        raise ValueError("services must be an attribute set")
    for name, service in services.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("invalid service name")
        if not isinstance(service, dict) or set(service) - {"command", "ready", "env"}:
            raise ValueError(f"service {name}: expected command, ready and optional env")
        argv, _ = validate_command(service.get("command"), f"service.{name}")
        validate_references(argv, set(), f"service.{name}.command", set(socket_names))
        ready = service.get("ready")
        if not isinstance(ready, dict) or set(ready) - {"unixSocket", "timeoutSeconds"}:
            raise ValueError(f"service {name}: readiness must use unixSocket and optional timeoutSeconds")
        socket = ready.get("unixSocket")
        if socket not in socket_names:
            raise ValueError(f"service {name}: readiness socket must be declared in sockets")
        timeout = ready.get("timeoutSeconds", 10)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"service {name}: readiness timeout must be positive")
        env = service.get("env", {})
        if not isinstance(env, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            raise ValueError(f"service {name}: env must contain strings")


class ServiceGroup:
    """Owns process groups and log files; teardown also handles partial startup."""

    def __init__(self, root, socket_names, services, environment, resolve):
        self.root = root
        self.services = services
        self.environment = environment
        self.resolve = resolve
        self.sockets = {name: str(root / "sockets" / (name + ".sock")) for name in socket_names}
        self.processes = []
        self.handles = []
        self.log_paths = {}

    def __enter__(self):
        (self.root / "sockets").mkdir(parents=True, exist_ok=True)
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        try:
            for name, service in self.services.items():
                argv = self.resolve(service["command"]["program"], service["command"].get("args", []))
                if not all(isinstance(arg, str) for arg in argv):
                    raise ServiceError(f"service {name}: command reference must resolve to a string")
                log_out = self.root / "logs" / (name + ".stdout.log")
                log_err = self.root / "logs" / (name + ".stderr.log")
                stdout = log_out.open("wb")
                stderr = log_err.open("wb")
                self.handles.extend([stdout, stderr])
                self.log_paths[name] = (log_out, log_err)
                process = subprocess.Popen(
                    argv, cwd=self.root, env={**self.environment, **service.get("env", {})},
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
                self.processes.append((name, process))
                self._await_ready(name, process, service["ready"])
        except (OSError, ValueError, ServiceError) as exc:
            self.close()
            raise ServiceError(str(exc) + "; " + str(self.evidence())) from exc
        return self.sockets

    def _await_ready(self, name, process, ready):
        deadline = time.monotonic() + ready.get("timeoutSeconds", 10)
        path = Path(self.sockets[ready["unixSocket"]])
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ServiceError(f"service {name} exited before readiness (exit {process.returncode})")
            if path.is_socket():
                return
            time.sleep(0.025)
        raise ServiceError(f"service {name} readiness timed out waiting for socket {ready['unixSocket']}")

    def evidence(self):
        def tail(path):
            if not path.exists():
                return ""
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 4096))
                return stream.read().decode("utf-8", errors="replace")
        return {
            name: {"stdoutTail": tail(out), "stderrTail": tail(err)}
            for name, (out, err) in self.log_paths.items()
        }

    def close(self):
        for _, process in reversed(self.processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for _, process in reversed(self.processes):
            if process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
        for handle in self.handles:
            handle.close()

    def __exit__(self, _type, _value, _traceback):
        self.close()
