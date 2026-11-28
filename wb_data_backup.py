"""Local gateway data export and restart-time restore."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import threading
import zipfile
import zlib

import wb_usage_io


GROUPS = ("accounts", "usage", "subbridge-data")
CONTROL = ".wb-data-backup"
MANIFEST = "manifest.json"
FORMAT = "workbuddy2api-data-v1"
MAX_BYTES = 32 * 1024 ** 3
MAX_FILES = 50_000
MAX_DIRECTORY_BYTES = 16 * 1024 * 1024
CHUNK = 1024 * 1024
_LOCK = threading.Lock()
_EXPORT_LOCK = threading.Lock()
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                    *("COM" + str(i) for i in range(10)),
                    *("LPT" + str(i) for i in range(10)),
                    *("COM" + i for i in "¹²³"), *("LPT" + i for i in "¹²³")}


class BackupError(ValueError):
    pass


def roots(accounts, usage):
    bridge = os.environ.get("WB_SUBBRIDGE_DATA_DIR") or Path(__file__).with_name("subbridge-data")
    return {key: Path(path).resolve() for key, path in zip(GROUPS, (accounts, usage, bridge))}


def _check_roots(data):
    paths = list(data.values())
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if path == other or path in other.parents or other in path.parents:
                raise BackupError("Backup data directories must be separate.")


def _link(info):
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _link(path.lstat()):
        raise BackupError("Backup storage cannot be a link.")
    return path


def _state_path(data):
    return data["accounts"] / CONTROL / "state.json"


def _read_state(data):
    path = _state_path(data)
    if path.parent.exists() and _link(path.parent.lstat()):
        raise BackupError("Backup storage cannot be a link.")
    try:
        with path.open(encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {"phase": "idle"}
    if (not isinstance(state, dict) or state.get("phase") not in
            ("ready", "pending", "applying", "restored", "cancelled", "failed")
            or not re.fullmatch(r"[0-9a-f]{24}", str(state.get("id", "")))):
        raise BackupError("Backup recovery state is invalid.")
    return state


def _write_state(data, state):
    path = _state_path(data)
    _private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix="state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _public(state):
    return {key: state[key] for key in ("phase", "id", "files", "bytes", "groups") if key in state}


def status(data):
    with _LOCK:
        return _public(_read_state(data))


def _parts(name):
    parts = name.split("/")
    for part in parts:
        if (not part or part in (".", "..") or part != part.strip()
                or part.endswith(".") or any(ord(c) < 32 or c in '\\:<>|?*"' for c in part)
                or part.partition(".")[0].rstrip().upper() in _WINDOWS_DEVICES
                or part.casefold() == CONTROL):
            raise BackupError("Backup contains an unsafe file path.")
    return parts


def _skip(group, parts):
    first = parts[0].lower()
    if first in ("readme.txt", "readme.md", ".gitkeep"):
        return True
    if any(part.casefold() == CONTROL or part.endswith((".tmp", ".lock")) for part in parts):
        return True
    if group == "usage":
        return (first == "usage.jsonl.segments" or first.startswith("usage-aggregate-cache")
                or first in ("usage-summary.json", "usage-recent-index.json",
                             "logs.sqlite3-wal", "logs.sqlite3-shm", "logs.sqlite3-journal"))
    return False


def _files(root, group, parts=()):
    directory = root.joinpath(*parts)
    for path in sorted(directory.iterdir()):
        relative = (*parts, path.name)
        if _skip(group, relative):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if _link(info):
            continue
        if stat.S_ISDIR(info.st_mode):
            yield from _files(root, group, relative)
        elif stat.S_ISREG(info.st_mode):
            yield path, group + "/" + "/".join(relative)


def _add(archive, name, source, size):
    _parts(name)
    with archive.open(name, "w", force_zip64=True) as dst:
        _copy(source, dst, size)


def _copy(source, target, size):
    while size:
        chunk = source.read(min(CHUNK, size))
        if not chunk:
            raise BackupError("Backup data ended before the expected size.")
        target.write(chunk)
        size -= len(chunk)


def export_file(data):
    return tempfile.TemporaryFile(dir=_private_dir(data["usage"] / CONTROL))


def write_zip(data, output):
    """Write a seekable archive without loading the ledger into memory."""
    _check_roots(data)
    if not _EXPORT_LOCK.acquire(blocking=False):
        raise BackupError("Another backup export is in progress.")
    try:
        groups = [group for group in GROUPS if group != "subbridge-data" or data[group].is_dir()]
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            archive.writestr(MANIFEST, json.dumps({"format": FORMAT, "groups": groups}))
            total, count = 0, 0
            for group in groups:
                root = data[group]
                if not root.is_dir():
                    continue
                ledger = root / "usage.jsonl"
                files = _files(root, group)
                if group == "usage":
                    files = ((path, name) for path, name in files if path != ledger)
                    for ledger_path in (ledger, root / "usage.jsonl.segments"):
                        if _exists(ledger_path) and _link(ledger_path.lstat()):
                            raise BackupError("Usage ledger cannot be a link.")
                    try:
                        source = wb_usage_io.open_log(str(ledger))
                    except FileNotFoundError:
                        pass
                    else:
                        with source:
                            size = source.seek(0, os.SEEK_END)
                            source.seek(0)
                            if size > MAX_BYTES:
                                raise BackupError("Backup exceeds maximum size.")
                            _add(archive, "usage/usage.jsonl", source, size)
                        total, count = size, 1
                for path, name in files:
                    with _snapshot(path, name) as source:
                        if source is None:
                            continue
                        size = os.fstat(source.fileno()).st_size
                        total += size
                        count += 1
                        if total > MAX_BYTES or count > MAX_FILES:
                            raise BackupError("Backup exceeds maximum size.")
                        _add(archive, name, source, size)
    finally:
        _EXPORT_LOCK.release()


@contextmanager
def _snapshot(path, name):
    if name == "usage/logs.sqlite3":
        # SQLite's online backup includes committed WAL rows in a standalone DB.
        with tempfile.TemporaryDirectory(prefix="db-", dir=_private_dir(path.parent / CONTROL)) as temp:
            destination = Path(temp) / "logs.sqlite3"
            source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                target = sqlite3.connect(str(destination))
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
            with destination.open("rb") as handle:
                yield handle
        return
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        yield None
        return
    with handle:
        yield handle


def _open_archive(path):
    # Bound the central directory before ZipFile reads it into memory,
    # including ZIP64 archives used for a large usage ledger.
    with open(path, "rb") as source:
        end = zipfile._EndRecData(source)
    if (not end or end[zipfile._ECD_ENTRIES_TOTAL] > MAX_FILES + 1
            or not 0 <= end[zipfile._ECD_SIZE] <= MAX_DIRECTORY_BYTES):
        raise BackupError("Backup ZIP directory is missing or too large.")
    return zipfile.ZipFile(path)


def _inspect(archive):
    entries = archive.infolist()
    if len(entries) > MAX_FILES + 1:
        raise BackupError("Backup contains too many files.")
    manifests = [item for item in entries if item.filename == MANIFEST]
    if len(manifests) != 1 or manifests[0].file_size > 65536:
        raise BackupError("Backup manifest is missing or invalid.")
    meta = json.loads(archive.read(manifests[0]))
    if not isinstance(meta, dict) or meta.get("format") != FORMAT:
        raise BackupError("Unsupported backup format.")
    groups = meta.get("groups")
    if (not isinstance(groups, list) or groups not in (list(GROUPS), list(GROUPS[:2]))):
        raise BackupError("Backup data groups are invalid.")
    members = []
    names, parents = set(), {}
    total = 0
    for item in entries:
        if item.filename == MANIFEST:
            continue
        if item.orig_filename != item.filename:
            raise BackupError("Backup contains an unsafe file path.")
        parts = _parts(item.filename)
        kind = stat.S_IFMT(item.external_attr >> 16)
        if (len(parts) < 2 or parts[0] not in groups or _skip(parts[0], parts[1:])
                or kind not in (0, stat.S_IFREG) or item.flag_bits & 1
                or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
            raise BackupError("Backup contains an unsupported file.")
        name = item.filename.casefold()
        ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
        if name in names or name in parents:
            raise BackupError("Backup contains conflicting file paths.")
        for ancestor in ancestors:
            normalized = ancestor.casefold()
            if normalized in names or parents.setdefault(normalized, ancestor) != ancestor:
                raise BackupError("Backup contains conflicting file paths.")
        names.add(name)
        total += item.file_size
        if total > MAX_BYTES:
            raise BackupError("Uncompressed backup exceeds maximum size.")
        members.append((item, parts))
    return groups, members, total


def _pending_file(data, backup_id):
    return _state_path(data).parent / (backup_id + ".zip")


def stage(data, source, size):
    if not 0 < size <= MAX_BYTES:
        raise BackupError("Backup upload size is out of range.")
    _check_roots(data)
    with _LOCK:
        before = _read_state(data)
        if before["phase"] in ("pending", "applying"):
            raise BackupError("Cancel the scheduled restore before importing another backup.")
        backup_id = secrets.token_hex(12)
        _private_dir(_state_path(data).parent)
        path = _pending_file(data, backup_id)
        try:
            with open(path, "xb", opener=_private_file) as handle:
                _copy(source, handle, size)
                handle.flush()
                os.fsync(handle.fileno())
            with _open_archive(path) as archive:
                groups, members, total = _inspect(archive)
                for item, _ in members:
                    with archive.open(item) as member:
                        while member.read(CHUNK):
                            pass
            state = {"phase": "ready", "id": backup_id, "files": len(members),
                     "bytes": total, "groups": groups, "roots": _root_ids(data, groups)}
            _write_state(data, state)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, UnicodeError,
                json.JSONDecodeError, zlib.error) as exc:
            path.unlink(missing_ok=True)
            raise BackupError("Invalid or damaged backup archive.") from exc
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        if before["phase"] == "ready":
            _pending_file(data, before["id"]).unlink(missing_ok=True)
        return _public(state)


def _private_file(path, flags):
    return os.open(path, flags, 0o600)


def _root_ids(data, groups):
    return {group: str(data[group]) for group in groups}


def choose(data, backup_id, confirm):
    with _LOCK:
        state = _read_state(data)
        if state["phase"] not in ("ready", "pending") or state.get("id") != backup_id:
            raise BackupError("The imported backup is no longer available.")
        state["phase"] = "pending" if confirm else "cancelled"
        _write_state(data, state)
        if not confirm:
            _pending_file(data, backup_id).unlink(missing_ok=True)
        return _public(state)


def _transaction(data, group, backup_id):
    return _private_dir(_private_dir(data[group] / CONTROL) / backup_id)


def _exists(path):
    return os.path.lexists(path)


def _rollback(data, state):
    for group, plan in state["plan"].items():
        root = data[group]
        txn = _transaction(data, group, state["id"])
        staged, previous = txn / "staged", txn / "previous"
        original = {os.path.normcase(name) for name in plan["old"]}
        for name in plan["new"]:
            live = root / name
            if (os.path.normcase(name) not in original and _exists(live)
                    and not _exists(staged / name)):
                os.replace(live, staged / name)
        for name in plan["old"]:
            saved, live = previous / name, root / name
            if _exists(saved):
                if _exists(live):
                    # The new entry has already been installed over this name.
                    os.replace(live, staged / name)
                os.replace(saved, live)


def apply_pending(data):
    """Swap data before gateway writers start. A journal makes rollback repeatable."""
    with _LOCK:
        state = _read_state(data)
        if state["phase"] not in ("pending", "applying"):
            return None
        _check_roots(data)
        groups, backup_id = state["groups"], state["id"]
        if state["roots"] != _root_ids(data, groups):
            if state["phase"] == "applying":
                raise BackupError("Restore data directory paths changed during rollback.")
            state["phase"] = "failed"
            _write_state(data, state)
            return "failed"
        if state["phase"] == "applying":
            _rollback(data, state)
            state["phase"] = "failed"
            _write_state(data, state)
            return "failed"
        try:
            transactions = {g: _transaction(data, g, backup_id) for g in groups}
            for txn in transactions.values():
                stage_dir = txn / "staged"
                if stage_dir.exists():
                    shutil.rmtree(stage_dir)
                _private_dir(stage_dir)
                _private_dir(txn / "previous")
            with _open_archive(_pending_file(data, backup_id)) as archive:
                archive_groups, members, _ = _inspect(archive)
                if archive_groups != groups:
                    raise BackupError("Backup data groups changed since upload.")
                for item, (group, *parts) in members:
                    target = transactions[group].joinpath("staged", *parts)
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with archive.open(item) as source, open(target, "xb", opener=_private_file) as dest:
                        _copy(source, dest, item.file_size)
                        if source.read(1):
                            raise BackupError("Backup member size does not match its manifest.")
            state["plan"] = {}
            for group, txn in transactions.items():
                state["plan"][group] = {
                    "old": [p.name for p in data[group].iterdir()
                            if p.name not in (CONTROL, "README.txt", "README.md", ".gitkeep")],
                    "new": [p.name for p in (txn / "staged").iterdir()],
                }
            state["phase"] = "applying"
            _write_state(data, state)
            for group, plan in state["plan"].items():
                root, txn = data[group], transactions[group]
                for name in plan["old"]:
                    os.replace(root / name, txn / "previous" / name)
                for name in plan["new"]:
                    os.replace(txn / "staged" / name, root / name)
            state["phase"] = "restored"
            _write_state(data, state)
        except (OSError, ValueError, zipfile.BadZipFile):
            # Read the durable phase: a failed write of the final state still
            # means applying, while failed extraction never touched live data.
            durable = _read_state(data)
            if durable["phase"] == "applying":
                _rollback(data, durable)
            durable["phase"] = "failed"
            _write_state(data, durable)
            return "failed"
        _pending_file(data, backup_id).unlink(missing_ok=True)
        return "restored"
