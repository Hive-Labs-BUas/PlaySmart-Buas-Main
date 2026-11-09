"""Upload capture files to the PlaySmart SFTP server on queenbee.

Modified 2026-09 (iteration 5): the password used to be a literal here, in a public
repository, fronting students' biometric data. Host, port, user and password now come
from the environment - put them in the repo's .env (loaded by src/session.py, see
.env.example) or export them in the shell. The defaults below are the non-secret parts.

Modified 2026-09-28: the local copy is now an explicit choice, not an accident of a
commented-out ``os.remove``. ``PLAYSMART_UPLOAD`` in .env selects the mode:

    keep   upload and keep the local copy (default)
    off    never upload; everything stays on this machine
    move   upload, confirm the remote size, then delete the local copy

Other changes with it: one SSH connection per session instead of one per file; a
connect timeout so a lab PC off the VPN fails in ten seconds instead of hanging; remote
paths built with ``posixpath`` (``os.path.join`` on Windows produced ``/data/gaze\\x``);
files land under a ``.part`` name and are renamed on completion, so the server's cron
pipeline never picks up a half-written video; a file already on the server with the same
size is skipped, so re-running an upload is safe.
"""

from __future__ import annotations

import os
import posixpath
import socket
from pathlib import Path

import paramiko

DEFAULT_SFTP_HOST = os.environ.get("PLAYSMART_SFTP_HOST", "10.4.28.2")
DEFAULT_SFTP_PORT = int(os.environ.get("PLAYSMART_SFTP_PORT", "2422"))
DEFAULT_SFTP_USERNAME = os.environ.get("PLAYSMART_SFTP_USER", "localhost")
DEFAULT_SFTP_PASSWORD = os.environ.get("PLAYSMART_SFTP_PASSWORD", "")
CONNECT_TIMEOUT_S = float(os.environ.get("PLAYSMART_SFTP_TIMEOUT_S", "10"))

MODES = ("keep", "off", "move")

# Upload outcomes, as returned by SftpSession.upload().
UPLOADED = "uploaded"      # transferred now, size confirmed
EXISTS = "exists"          # already on the server with the same size, not re-sent
FAILED = "failed"          # error; the local copy is untouched
OFF = "off"                # uploads disabled or no password; kept locally
UNREACHABLE = "unreachable"  # could not connect to the server; kept locally


def upload_mode(password: str | None = None) -> str:
    """The configured mode, or ``off`` when there is no password to upload with."""
    mode = os.environ.get("PLAYSMART_UPLOAD", "keep").strip().lower() or "keep"
    if mode not in MODES:
        print(f"PLAYSMART_UPLOAD={mode!r} is not one of {MODES}; using 'keep'")
        mode = "keep"
    if password is None:
        password = os.environ.get("PLAYSMART_SFTP_PASSWORD", DEFAULT_SFTP_PASSWORD)
    if mode != "off" and not password:
        return "off"
    return mode


class SftpSession:
    """One connection to the server, used for every file of a capture session.

    Use as a context manager. ``connect_error`` is set instead of raising when the
    server cannot be reached, so callers can report once and keep every file local.
    """

    def __init__(self, host: str = DEFAULT_SFTP_HOST, port: int = DEFAULT_SFTP_PORT,
                 username: str = DEFAULT_SFTP_USERNAME, password: str = DEFAULT_SFTP_PASSWORD,
                 timeout_s: float = CONNECT_TIMEOUT_S, mode: str | None = None):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.timeout_s = timeout_s
        self.mode = mode or upload_mode(self.password)
        self.transport: paramiko.Transport | None = None
        self.sftp: paramiko.SFTPClient | None = None
        self.connect_error: str | None = None

    # -- connection -----------------------------------------------------------

    def __enter__(self) -> "SftpSession":
        if self.mode == "off":
            return self
        if not self.password:
            self.connect_error = "PLAYSMART_SFTP_PASSWORD is not set"
            return self
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            self.transport = paramiko.Transport(sock)
            self.transport.banner_timeout = self.timeout_s
            self.transport.connect(username=self.username, password=self.password)
            self.sftp = paramiko.SFTPClient.from_transport(self.transport)
            self.sftp.get_channel().settimeout(max(60.0, self.timeout_s))
        except (OSError, paramiko.SSHException) as exc:
            self.connect_error = f"{self.host}:{self.port}: {exc}"
            self.close()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        for obj in (self.sftp, self.transport):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: S110 - closing is best effort
                    pass
        self.sftp = self.transport = None

    @property
    def connected(self) -> bool:
        return self.sftp is not None

    # -- files -----------------------------------------------------------------

    def _remote_size(self, remote_path: str) -> int | None:
        try:
            return self.sftp.stat(remote_path).st_size
        except OSError:
            return None

    def _ensure_dir(self, directory: str) -> bool:
        try:
            self.sftp.stat(directory)
            return True
        except OSError:
            pass
        try:
            self.sftp.mkdir(directory)
            print(f"  created {directory} on the server")
            return True
        except OSError as exc:
            print(f"  !! {directory} does not exist on the server and could not be created "
                  f"({exc}) - ask Uther to add it to the sftp container's mkdir -p")
            return False

    def upload(self, local_path: str | os.PathLike, dest_directory: str) -> str:
        """Upload one file into ``dest_directory``; returns one of the outcome strings.

        In ``move`` mode the local file is deleted only after paramiko has confirmed
        the remote size matches. A failure never touches the local copy.
        """
        local = Path(local_path)
        if self.mode == "off":
            return OFF
        if not self.connected:
            return UNREACHABLE
        try:
            size = local.stat().st_size
        except OSError as exc:
            print(f"  !! cannot read {local}: {exc}")
            return FAILED

        dest = dest_directory.rstrip("/") or "/"
        remote = posixpath.join(dest, local.name)
        part = remote + ".part"
        try:
            if not self._ensure_dir(dest):
                return FAILED
            if self._remote_size(remote) == size:
                return EXISTS
            self.sftp.put(str(local), part, confirm=True)
            if self._remote_size(remote) is not None:
                self.sftp.remove(remote)
            try:
                self.sftp.posix_rename(part, remote)
            except OSError:
                self.sftp.rename(part, remote)
        except (OSError, paramiko.SSHException) as exc:
            print(f"  !! upload of {local.name} failed: {exc}")
            try:
                self.sftp.remove(part)
            except Exception:  # noqa: S110 - leftover .part is harmless
                pass
            return FAILED

        if self.mode == "move":
            try:
                local.unlink()
            except OSError as exc:
                print(f"  !! uploaded but could not delete local {local.name}: {exc}")
        return UPLOADED


def upload_files(items, **session_kwargs) -> dict[str, str]:
    """Upload ``[(local_path, dest_directory), ...]`` over one connection.

    Returns ``{str(local_path): outcome}``. Prints the connection problem once, if any.
    """
    results: dict[str, str] = {}
    with SftpSession(**session_kwargs) as sess:
        if sess.mode == "off":
            print("Upload off (PLAYSMART_UPLOAD=off or no PLAYSMART_SFTP_PASSWORD) - files kept locally")
        elif not sess.connected:
            print(f"SFTP server not reachable ({sess.connect_error}) - files kept locally; "
                  "re-run src/upload_session.py on the BUas network or VPN")
        for local, dest in items:
            results[str(local)] = sess.upload(local, dest)
    return results


def upload_file_to_sftp(local_file_path, dest_directory,
                        sftp_host=DEFAULT_SFTP_HOST,
                        sftp_port=DEFAULT_SFTP_PORT,
                        sftp_username=DEFAULT_SFTP_USERNAME,
                        sftp_password=DEFAULT_SFTP_PASSWORD) -> str:
    """Upload a single file over its own connection. Kept for older callers.

    Prefer ``upload_files`` / ``SftpSession`` for more than one file.
    """
    results = upload_files([(local_file_path, dest_directory)], host=sftp_host,
                           port=sftp_port, username=sftp_username, password=sftp_password)
    outcome = results[str(local_file_path)]
    if outcome == UPLOADED:
        print(f"Uploaded {os.path.basename(local_file_path)} to {dest_directory}")
    return outcome
