"""sftp_upload.py against a throwaway in-process SFTP server (paramiko stub).

Run with ``poetry run pytest tests/test_sftp_upload.py``. No network, no queenbee.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

import paramiko
import pytest
from paramiko import (SFTPAttributes, SFTPHandle, SFTPServer, SFTPServerInterface,
                      ServerInterface)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.python_app import sftp_upload  # noqa: E402


# ---------------- minimal SFTP server rooted at a temp dir ----------------

class _Server(ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if password == "pw" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class _Handle(SFTPHandle):
    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))

    def chattr(self, attr):
        return paramiko.SFTP_OK


class _Sftp(SFTPServerInterface):
    root: str = ""

    def _real(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def list_folder(self, path):
        try:
            out = []
            for name in os.listdir(self._real(path)):
                attr = SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), name)))
                attr.filename = name
                out.append(attr)
            return out
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        try:
            return SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            fd = os.open(self._real(path), flags | getattr(os, "O_BINARY", 0), 0o644)
            f = os.fdopen(fd, "wb" if flags & os.O_WRONLY else "rb")
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        h = _Handle(flags)
        h.filename = self._real(path)
        h.readfile = h.writefile = f
        return h

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._real(oldpath), self._real(newpath))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    posix_rename = rename

    def mkdir(self, path, attr):
        if os.path.basename(path.rstrip("/")) == "forbidden":
            return paramiko.SFTP_PERMISSION_DENIED
        try:
            os.mkdir(self._real(path))
        except OSError as exc:
            return SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK


@pytest.fixture
def sftp_server(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    (root / "data" / "gaze").mkdir(parents=True)

    class Rooted(_Sftp):
        pass

    Rooted.root = str(root)
    key = paramiko.RSAKey.generate(2048)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    port = sock.getsockname()[1]
    transports = []
    stop = threading.Event()

    def serve():
        sock.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            t = paramiko.Transport(conn)
            t.add_server_key(key)
            t.set_subsystem_handler("sftp", SFTPServer, Rooted)
            t.start_server(server=_Server())
            transports.append(t)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    yield {"host": "127.0.0.1", "port": port, "root": root}
    stop.set()
    th.join(2)
    for t in transports:
        t.close()
    sock.close()


def _sess(srv, **kw):
    kw.setdefault("password", "pw")
    return sftp_upload.SftpSession(host=srv["host"], port=srv["port"], username="u",
                                   timeout_s=5, **kw)


# ---------------- tests ----------------

def test_keep_uploads_and_keeps_local(sftp_server, tmp_path):
    local = tmp_path / "s1_gaze.csv"
    local.write_bytes(b"a" * 5000)
    with _sess(sftp_server, mode="keep") as s:
        assert s.connected, s.connect_error
        assert s.upload(local, "/data/gaze/") == sftp_upload.UPLOADED
        assert s.upload(local, "/data/gaze/") == sftp_upload.EXISTS
    assert local.exists()
    remote = sftp_server["root"] / "data" / "gaze" / "s1_gaze.csv"
    assert remote.read_bytes() == b"a" * 5000
    assert not list((sftp_server["root"] / "data" / "gaze").glob("*.part"))


def test_remote_path_is_posix_and_dir_is_created(sftp_server, tmp_path):
    local = tmp_path / "s1_meta.json"
    local.write_text("{}")
    with _sess(sftp_server, mode="keep") as s:
        assert s.upload(local, "/data/gamestate") == sftp_upload.UPLOADED
    assert (sftp_server["root"] / "data" / "gamestate" / "s1_meta.json").is_file()


def test_unwritable_dir_fails_and_keeps_local(sftp_server, tmp_path):
    local = tmp_path / "x.csv"
    local.write_text("x")
    with _sess(sftp_server, mode="keep") as s:
        assert s.upload(local, "/data/forbidden/") == sftp_upload.FAILED
    assert local.exists()


def test_move_deletes_local_only_after_confirmed_upload(sftp_server, tmp_path):
    local = tmp_path / "s2_video.mkv"
    local.write_bytes(os.urandom(200_000))
    with _sess(sftp_server, mode="move") as s:
        assert s.upload(local, "/data/video/") == sftp_upload.UPLOADED
    assert not local.exists()
    assert (sftp_server["root"] / "data" / "video" / "s2_video.mkv").stat().st_size == 200_000


def test_changed_file_is_resent(sftp_server, tmp_path):
    local = tmp_path / "s3_input.csv"
    local.write_text("v1")
    with _sess(sftp_server) as s:
        assert s.upload(local, "/data/input") == sftp_upload.UPLOADED
        local.write_text("v2-longer")
        assert s.upload(local, "/data/input") == sftp_upload.UPLOADED
    assert (sftp_server["root"] / "data" / "input" / "s3_input.csv").read_text() == "v2-longer"


def test_off_mode_never_connects(tmp_path):
    local = tmp_path / "f"
    local.write_text("x")
    with sftp_upload.SftpSession(host="127.0.0.1", port=1, password="pw", mode="off") as s:
        assert s.upload(local, "/data/gaze") == sftp_upload.OFF
    assert local.exists()


def test_unreachable_server_reports_once_and_keeps_files(tmp_path):
    local = tmp_path / "f"
    local.write_text("x")
    res = sftp_upload.upload_files([(local, "/data/gaze")], host="127.0.0.1", port=1,
                                   password="pw", timeout_s=1, mode="keep")
    assert res == {str(local): sftp_upload.UNREACHABLE}
    assert local.exists()


def test_upload_mode_env(monkeypatch):
    monkeypatch.setenv("PLAYSMART_SFTP_PASSWORD", "pw")
    monkeypatch.setenv("PLAYSMART_UPLOAD", "move")
    assert sftp_upload.upload_mode() == "move"
    monkeypatch.setenv("PLAYSMART_UPLOAD", "nonsense")
    assert sftp_upload.upload_mode() == "keep"
    monkeypatch.setenv("PLAYSMART_SFTP_PASSWORD", "")
    assert sftp_upload.upload_mode() == "off"
    monkeypatch.setenv("PLAYSMART_UPLOAD", "off")
    assert sftp_upload.upload_mode() == "off"


def test_legacy_wrapper(sftp_server, tmp_path):
    local = tmp_path / "s4_audio.wav"
    local.write_bytes(b"RIFF")
    out = sftp_upload.upload_file_to_sftp(str(local), "/data/audio/", sftp_host=sftp_server["host"],
                                          sftp_port=sftp_server["port"], sftp_username="u",
                                          sftp_password="pw")
    assert out == sftp_upload.UPLOADED
    assert (sftp_server["root"] / "data" / "audio" / "s4_audio.wav").is_file()
