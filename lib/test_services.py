"""Managed local process fixtures for declarative runtime tests.

This module owns only service lifecycle. The existing test runner owns
commands, assertions, references and evidence.
"""
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import threading
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


DEFAULT_OUTPUT_LIMIT = 1024 * 1024
MAX_OUTPUT_LIMIT = 64 * 1024 * 1024


class OutputLimitExceeded(Exception):
    def __init__(self, channel, limit, stdout, stderr):
        super().__init__(f"{channel} exceeded maxOutputBytes ({limit} bytes)")
        self.stdout = stdout
        self.stderr = stderr


def run_isolated(argv, *, cwd, env, timeout, max_output_bytes=DEFAULT_OUTPUT_LIMIT):
    """Run one test step with bounded capture and process-group cleanup.

    Pipes are drained incrementally, so malicious or accidental output cannot
    consume unbounded memory. Descendants of exited parents are also killed.
    """
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout

    def decoded():
        return {name: value.decode("utf-8", errors="replace")
                for name, value in buffers.items()}

    def terminate_group():
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        with selectors.DefaultSelector() as selector:
            for channel, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, channel)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                events = selector.select(min(remaining, 0.1))
                if not events and process.poll() is not None:
                    # Inherited pipes can stay open after the leader exits.
                    terminate_group()
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    channel = key.data
                    room = max_output_bytes - len(buffers[channel])
                    buffers[channel].extend(chunk[:max(0, room)])
                    if len(chunk) > room:
                        output = decoded()
                        raise OutputLimitExceeded(channel, max_output_bytes,
                                                  output["stdout"], output["stderr"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        process.wait(timeout=remaining)
        output = decoded()
        return subprocess.CompletedProcess(
            argv, process.returncode, output["stdout"], output["stderr"])
    except subprocess.TimeoutExpired as error:
        output = decoded()
        raise subprocess.TimeoutExpired(
            argv, timeout, output=output["stdout"], stderr=output["stderr"]) from error
    finally:
        terminate_group()
        process.wait()
        process.stdout.close()
        process.stderr.close()


SERVICE_LOG_FILE_LIMIT = 1024 * 1024
SERVICE_LOG_TAIL_LIMIT = 4096


class _BoundedServiceLog:
    """Drain a service pipe without ever growing its on-disk log unboundedly."""

    def __init__(self, stream, file):
        self.stream = stream
        self.file = file
        self.total = 0
        self.stored = 0
        self.tail = bytearray()
        self.error = None
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            while True:
                chunk = self.stream.read1(65536)
                if not chunk:
                    break
                self.total += len(chunk)
                remaining = SERVICE_LOG_FILE_LIMIT - self.stored
                if remaining > 0:
                    prefix = chunk[:remaining]
                    self.file.write(prefix)
                    self.stored += len(prefix)
                self.tail = (self.tail + chunk)[-SERVICE_LOG_TAIL_LIMIT:]
        except OSError as error:
            self.error = str(error)
        finally:
            self.file.close()
            self.stream.close()

    def finish(self):
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise ServiceError("managed service retained an output pipe after teardown")
        if self.error:
            raise ServiceError(f"managed service log collection failed: {self.error}")

    def evidence(self):
        return {
            "tail": self.tail.decode("utf-8", errors="replace"),
            "bytes": self.total,
            "truncated": self.total > self.stored,
        }


class ServiceGroup:
    """Owns process groups and log files; teardown also handles partial startup."""

    def __init__(self, root, socket_names, services, environment, resolve):
        self.root = root
        self.services = services
        self.environment = environment
        self.resolve = resolve
        self.sockets = {name: str(root / "sockets" / (name + ".sock")) for name in socket_names}
        self.processes = []
        self.log_paths = {}
        self.captures = {}

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
                stdout_file = log_out.open("wb")
                try:
                    stderr_file = log_err.open("wb")
                except BaseException:
                    stdout_file.close()
                    raise
                try:
                    process = subprocess.Popen(
                        argv, cwd=self.root, env={**self.environment, **service.get("env", {})},
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, start_new_session=True)
                except BaseException:
                    stdout_file.close()
                    stderr_file.close()
                    raise
                self.processes.append((name, process))
                self.log_paths[name] = (log_out, log_err)
                self.captures[name] = (
                    _BoundedServiceLog(process.stdout, stdout_file),
                    _BoundedServiceLog(process.stderr, stderr_file),
                )
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
        return {
            name: {
                "stdoutTail": out.evidence()["tail"],
                "stderrTail": err.evidence()["tail"],
                "stdoutBytes": out.total,
                "stderrBytes": err.total,
                "stdoutTruncated": out.total > out.stored,
                "stderrTruncated": err.total > err.stored,
            }
            for name, (out, err) in self.captures.items()
        }

    def close(self):
        # Children can outlive their service leader. A successful poll() on the
        # leader must not exempt its process group from cleanup.
        for _, process in reversed(self.processes):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 1
        try:
            for _, process in reversed(self.processes):
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            # Also kill descendants of leaders which have already exited (or
            # whose descendants ignore SIGTERM), without waiting indefinitely.
            for _, process in reversed(self.processes):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for _, process in reversed(self.processes):
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            errors = []
            for channels in self.captures.values():
                for capture in channels:
                    try:
                        capture.finish()
                    except ServiceError as error:
                        errors.append(str(error))
            if errors:
                raise ServiceError("; ".join(errors))

    def __exit__(self, _type, _value, _traceback):
        self.close()
