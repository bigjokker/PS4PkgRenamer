#!/usr/bin/env python3
"""PS4PkgRenamer — reads param.sfo from PS4 .pkg files to rename them for
correct alphabetical sorting in flat package-list installers (GoldHEN)."""

from __future__ import annotations

import json
import os
import queue
import re
import struct
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

APP_TITLE = "PS4PkgRenamer"
APP_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SFO_MAGIC = b"\x00PSF"
PKG4_MAGIC = b"\x7fCNT"
PARAM_SFO_ID = 0x1000
MAX_SFO_SIZE = 1_000_000  # real param.sfo is tiny; reject absurd sizes

# CATEGORY -> (label, order, sort_suffix)
# Base also gets a suffix ("- 0 Base"): bare "Name.pkg" sorts AFTER
# "Name - 1 Update.pkg" because space (0x20) < period (0x2E) in ASCII.
CATEGORY_MAP = {
    "gd": ("base", 0, " - 0 Base"),
    "gde": ("base", 0, " - 0 Base"),  # demo treated as base
    "gp": ("update", 1, " - 1 Update"),
    "ac": ("dlc", 2, " - 2 DLC"),
}
FALLBACK_DIGIT_MAP = {
    "1": ("base", 0, " - 0 Base"),
    "2": ("update", 1, " - 1 Update"),
    "3": ("dlc", 2, " - 2 DLC"),
}
OTHER = ("other", 9, " - 9 Other")

FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|]')

DEFAULT_SCAN_MB = 48
DEFAULT_PREFIX = "ZZZ - "
UNDO_LOG_NAME = ".ps4pkgrenamer_last_undo.json"

# Naming presets: {key: (display_name, description)}
# Placeholders: {title} {title_id} {title_block} {order} {kind}
#               {fw} {fw_tag} {version} {version_tag} {dlc_title}
NAME_PRESETS = {
    "goldhen": (
        "GoldHEN sort (default)",
        "Title [CUSA] - 0 Base (FW x.xx+) / Update / DLC",
    ),
    "no_fw": (
        "Without firmware",
        "Title [CUSA] - 0 Base / 1 Update vX / 2 DLC - name",
    ),
    "compact": (
        "Compact",
        "Title [CUSA] - Base / Update vX / DLC - name",
    ),
    "title_only": (
        "Title + kind only",
        "Title - Base / Update / DLC - name",
    ),
}

NAME_TEMPLATES = {
    "goldhen": {
        0: "{title_block} - 0 Base{fw_tag}",
        1: "{title_block} - 1 Update{fw_tag}{version_tag}",
        2: "{title_block} - 2 DLC{fw_tag} - {dlc_title}",
        9: "{title_block} - 9 Other{fw_tag} - {dlc_title}",
    },
    "no_fw": {
        0: "{title_block} - 0 Base",
        1: "{title_block} - 1 Update{version_tag}",
        2: "{title_block} - 2 DLC - {dlc_title}",
        9: "{title_block} - 9 Other - {dlc_title}",
    },
    "compact": {
        0: "{title_block} - Base",
        1: "{title_block} - Update{version_tag}",
        2: "{title_block} - DLC - {dlc_title}",
        9: "{title_block} - Other - {dlc_title}",
    },
    "title_only": {
        0: "{title} - Base",
        1: "{title} - Update{version_tag}",
        2: "{title} - DLC - {dlc_title}",
        9: "{title} - Other - {dlc_title}",
    },
}

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str], None]


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def sanitize_name(name: str) -> str:
    """Clean a name component (title, DLC title, etc.).
    Trailing dots/spaces are NOT stripped here — that is applied once in
    finalize_filename() on the fully assembled stem so mid-name dots
    (e.g. P.T.) stay intact."""
    if not name:
        return "Unknown"
    name = name.replace(":", " -")
    name = FORBIDDEN_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"


def finalize_filename(name: str) -> str:
    """Strip trailing dots/spaces invalid on exFAT/Windows from the full stem."""
    name = sanitize_name(name)
    name = name.rstrip(" .")
    return name or "Unknown"


def nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def same_path(a: str, b: str) -> bool:
    return nfc(os.path.normcase(os.path.abspath(a))) == nfc(
        os.path.normcase(os.path.abspath(b))
    )


# ---------------------------------------------------------------------------
# param.sfo parsing
# ---------------------------------------------------------------------------

def parse_sfo(data: bytes) -> Optional[dict]:
    """Parse a complete param.sfo blob and return a key/value dict."""
    if len(data) < 20 or data[0:4] != SFO_MAGIC:
        return None
    key_table_offset, data_table_offset, count = struct.unpack_from("<III", data, 0x08)
    if count == 0 or count > 200:
        return None
    entries = {}
    for i in range(count):
        entry_off = 0x14 + i * 16
        if entry_off + 16 > len(data):
            break
        key_offset, data_fmt, data_len, data_max_len, data_offset = struct.unpack_from(
            "<HHIII", data, entry_off
        )
        k_start = key_table_offset + key_offset
        if k_start >= len(data):
            continue
        k_end = data.find(b"\x00", k_start)
        if k_end == -1:
            k_end = k_start
        key = data[k_start:k_end].decode("utf-8", "replace")

        val_start = data_table_offset + data_offset
        val_bytes = data[val_start : val_start + data_len]
        if data_fmt in (0x0004, 0x0204):
            value = val_bytes.split(b"\x00", 1)[0].decode("utf-8", "replace")
        elif data_fmt == 0x0404:
            padded = val_bytes.ljust(4, b"\x00")[:4]
            value = struct.unpack("<I", padded)[0]
        else:
            value = val_bytes
        entries[key] = value
    return entries


def find_sfo_in_buffer(buf: bytes) -> Optional[dict]:
    """Scan buffer for SFO magic; return first valid block that has TITLE."""
    start = 0
    while True:
        idx = buf.find(SFO_MAGIC, start)
        if idx == -1:
            return None
        start = idx + 1
        if idx + 20 > len(buf):
            continue
        try:
            key_table_offset, data_table_offset, count = struct.unpack_from(
                "<III", buf, idx + 0x08
            )
        except struct.error:
            continue
        if count == 0 or count > 200:
            continue
        if key_table_offset > 0x20000 or data_table_offset > 0x20000:
            continue
        end_guess = idx + max(key_table_offset, data_table_offset) + 8192
        end_guess = min(end_guess, len(buf))
        entries = parse_sfo(buf[idx:end_guess])
        if entries and "TITLE" in entries:
            return entries
    return None


# ---------------------------------------------------------------------------
# SFO extraction: PKG header first, brute scan fallback
# ---------------------------------------------------------------------------

def _u32be(buf: bytes, off: int) -> int:
    return struct.unpack_from(">I", buf, off)[0]


def _u16be(buf: bytes, off: int) -> int:
    return struct.unpack_from(">H", buf, off)[0]


def extract_sfo_via_pkg_header(pkg_path: str) -> Optional[dict]:
    """Locate param.sfo via PS4 PKG meta entry table (id 0x1000).

    PS4 PKG header / meta table are big-endian (see LibOrbisPkg / psdevwiki).
    Entry 0x1000 is plaintext param.sfo — no decryption needed.
    """
    try:
        file_size = os.path.getsize(pkg_path)
        with open(pkg_path, "rb") as f:
            head = f.read(0x80)
            if len(head) < 0x40 or head[0:4] != PKG4_MAGIC:
                return None

            # Layout (big-endian):
            # 0x00 magic(4) rev(2) type(2) unk(4)
            # 0x0C file_count(4) entry_count(4)
            # 0x14 sc_entry_count(2) meta_count(2)
            # 0x18 meta_table_offset(4) ent_data_size(4)
            entry_count = _u32be(head, 0x10)
            meta_count = _u16be(head, 0x16)
            meta_table_offset = _u32be(head, 0x18)

            # Prefer meta_count when present; fall back to entry_count
            count = meta_count or entry_count
            if count == 0 or count > 10_000:
                return None
            if meta_table_offset == 0 or meta_table_offset >= file_size:
                return None

            table_size = count * 32
            if meta_table_offset + table_size > file_size:
                return None

            f.seek(meta_table_offset)
            table = f.read(table_size)
            if len(table) < table_size:
                return None

            for i in range(count):
                off = i * 32
                meta_id = _u32be(table, off + 0)
                data_ofs = _u32be(table, off + 16)
                data_size = _u32be(table, off + 20)
                if meta_id != PARAM_SFO_ID:
                    continue
                if data_size == 0 or data_size > MAX_SFO_SIZE:
                    return None
                if data_ofs + data_size > file_size:
                    return None
                f.seek(data_ofs)
                sfo_bytes = f.read(data_size)
                if len(sfo_bytes) != data_size:
                    return None
                entries = parse_sfo(sfo_bytes)
                if entries and "TITLE" in entries:
                    return entries
                return None
    except OSError:
        return None
    return None


def extract_sfo_via_scan(
    pkg_path: str, scan_mb: int, log: Optional[LogFn] = None
) -> Optional[dict]:
    """Fallback: read up to scan_mb and search for SFO magic."""
    max_bytes = scan_mb * 1024 * 1024
    chunk_size = 4 * 1024 * 1024
    buf = b""
    try:
        with open(pkg_path, "rb") as f:
            read_total = 0
            while read_total < max_bytes:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                buf += chunk
                read_total += len(chunk)
                entries = find_sfo_in_buffer(buf)
                if entries:
                    return entries
    except OSError as e:
        if log:
            log(f"  [ERROR] Could not read {pkg_path}: {e}")
        return None
    return find_sfo_in_buffer(buf)


def extract_sfo(
    pkg_path: str, scan_mb: int = DEFAULT_SCAN_MB, log: Optional[LogFn] = None
) -> Tuple[Optional[dict], str]:
    """Return (entries, method) where method is 'header', 'scan', or 'none'."""
    entries = extract_sfo_via_pkg_header(pkg_path)
    if entries:
        return entries, "header"
    entries = extract_sfo_via_scan(pkg_path, scan_mb, log)
    if entries:
        return entries, "scan"
    return None, "none"


def content_id_from_header(pkg_path: str) -> Optional[str]:
    """Best-effort CONTENT_ID from PKG header (offset 0x40, 0x30 bytes)."""
    try:
        with open(pkg_path, "rb") as f:
            head = f.read(0x70)
        if len(head) < 0x70 or head[0:4] != PKG4_MAGIC:
            return None
        raw = head[0x40 : 0x40 + 0x30]
        cid = raw.split(b"\x00", 1)[0].decode("ascii", "replace").strip()
        return cid or None
    except OSError:
        return None


def title_id_from_content_id(content_id: Optional[str]) -> Optional[str]:
    """Extract CUSAXXXXX from CONTENT_ID like UP9000-CUSA34384_00-..."""
    if not content_id:
        return None
    m = re.search(r"(CUSA\d{5}|PPSA\d{5}|CUSA\d+)", content_id, re.I)
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------------------
# Classification & naming
# ---------------------------------------------------------------------------

def format_fw_version(system_ver) -> Optional[str]:
    """SYSTEM_VER: high byte major, next byte minor as hex pairs (0x06720000 -> 6.72)."""
    if not isinstance(system_ver, int) or system_ver <= 0:
        return None
    major = (system_ver >> 24) & 0xFF
    minor = (system_ver >> 16) & 0xFF
    return f"{major:x}.{minor:02x}"


def classify(entries, original_filename: str):
    """Return (label, order, suffix, title, version, title_id, fw_version)."""
    category = None
    version = None
    title = None
    title_id = None
    fw_version = None
    if entries:
        category = str(entries.get("CATEGORY", "")).strip().lower()
        version = entries.get("APP_VER") or entries.get("VERSION")
        title = entries.get("TITLE")
        if isinstance(title, str):
            title = title.strip()
        title_id = entries.get("TITLE_ID")
        if isinstance(title_id, str):
            title_id = title_id.strip() or None
        fw_version = format_fw_version(entries.get("SYSTEM_VER"))

    if category in CATEGORY_MAP:
        label, order, suffix = CATEGORY_MAP[category]
    else:
        m = re.match(r"\s*([123])\b", original_filename)
        if m:
            label, order, suffix = FALLBACK_DIGIT_MAP[m.group(1)]
        else:
            label, order, suffix = OTHER

    return label, order, suffix, title, version, title_id, fw_version


def strip_base_prefix(dlc_title: str, base_title: str) -> str:
    if not dlc_title:
        return dlc_title
    bt = base_title.strip().lower()
    dt = dlc_title.strip()
    low = dt.lower()
    for sep in (":", " -", "-"):
        prefix = bt + sep
        if low.startswith(prefix):
            return dt[len(prefix) :].strip(" -:")
    return dt


def build_new_name(
    order: int,
    item_label: str,
    base_title: str,
    title: Optional[str],
    title_id: Optional[str],
    fw_version: Optional[str],
    version: Optional[str],
    dlc_or_other_title: str,
    template_key: str,
) -> str:
    templates = NAME_TEMPLATES.get(template_key) or NAME_TEMPLATES["goldhen"]
    tmpl = templates.get(order, templates[9])

    fw_tag = f" (FW {fw_version}+)" if fw_version else ""
    version_tag = f" v{version}" if version else ""
    title_block = item_label
    kind = {0: "Base", 1: "Update", 2: "DLC", 9: "Other"}.get(order, "Other")

    name = tmpl.format(
        title=sanitize_name(base_title),
        title_id=title_id or "",
        title_block=title_block,
        order=order,
        kind=kind,
        fw=fw_version or "",
        fw_tag=fw_tag,
        version=version or "",
        version_tag=version_tag,
        dlc_title=sanitize_name(dlc_or_other_title) if dlc_or_other_title else "Unknown",
    )
    return finalize_filename(name) + ".pkg"


def scan_game_folder(
    folder: str,
    scan_mb: int,
    template_key: str = "goldhen",
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
    progress_base: int = 0,
    progress_total: int = 0,
) -> List[dict]:
    """Analyze .pkg files in one folder; return rename plan items."""
    pkg_files = sorted(f for f in os.listdir(folder) if f.lower().endswith(".pkg"))
    if not pkg_files:
        return []

    items = []
    for i, fname in enumerate(pkg_files):
        full_path = os.path.join(folder, fname)
        if progress and progress_total:
            progress(progress_base + i, progress_total, fname)

        entries, method = extract_sfo(full_path, scan_mb, log)
        label, order, suffix, title, version, title_id, fw_version = classify(
            entries, fname
        )

        # Header CONTENT_ID can fill missing TITLE_ID
        if not title_id:
            cid = content_id_from_header(full_path)
            title_id = title_id_from_content_id(cid)

        items.append(
            {
                "path": full_path,
                "filename": fname,
                "label": label,
                "order": order,
                "suffix": suffix,
                "title": title,
                "version": version,
                "title_id": title_id,
                "fw_version": fw_version,
                "found_metadata": entries is not None,
                "sfo_method": method,
            }
        )

    # Single pkg in folder is always treated as base (homebrew/backport quirks)
    if len(items) == 1 and items[0]["order"] != 0:
        items[0]["order"] = 0
        items[0]["label"] = "base"

    # Prefer title from lowest sort order (Base > Update > DLC > Other).
    # Filename order alone would pick DLC over Update when Base is missing.
    base_title = None
    for preferred in (0, 1, 2, 9):
        for it in items:
            if it["order"] == preferred and it["title"]:
                base_title = it["title"]
                break
        if base_title:
            break
    if not base_title:
        base_title = os.path.basename(folder.rstrip(os.sep))
    base_title = sanitize_name(base_title)

    base_title_id = None
    for preferred in (0, 1, 2, 9):
        for it in items:
            if it["order"] == preferred and it["title_id"]:
                base_title_id = it["title_id"]
                break
        if base_title_id:
            break

    label = f"{base_title} [{base_title_id}]" if base_title_id else base_title

    # Multiple "base" pkgs → each keeps its own title (don't force one name)
    ambiguous = sum(1 for it in items if it["order"] == 0) > 1

    for it in items:
        if ambiguous:
            own_title = (
                sanitize_name(it["title"])
                if it["title"]
                else sanitize_name(os.path.splitext(it["filename"])[0])
            )
            item_label = (
                f"{own_title} [{it['title_id']}]" if it["title_id"] else own_title
            )
            own_base = own_title
        else:
            item_label = label
            own_base = base_title

        dlc_title = ""
        if it["order"] == 2:
            dlc_title = it["title"] or ""
            if dlc_title:
                dlc_title = sanitize_name(dlc_title)
                dlc_title = strip_base_prefix(dlc_title, base_title)
            if not dlc_title:
                dlc_title = sanitize_name(os.path.splitext(it["filename"])[0])
        elif it["order"] == 9:
            dlc_title = sanitize_name(
                it["title"] or os.path.splitext(it["filename"])[0]
            )

        it["new_filename"] = build_new_name(
            order=it["order"],
            item_label=item_label,
            base_title=own_base,
            title=it["title"],
            title_id=it["title_id"],
            fw_version=it["fw_version"],
            version=it["version"],
            dlc_or_other_title=dlc_title,
            template_key=template_key,
        )
        it["base_title"] = label
        it["folder"] = folder

    # Avoid name collisions within the same folder
    seen: Dict[str, int] = {}
    for it in items:
        key = it["new_filename"].lower()
        if key in seen:
            seen[key] += 1
            stem, ext = os.path.splitext(it["new_filename"])
            it["new_filename"] = f"{stem} ({seen[key]}){ext}"
        else:
            seen[key] = 1

    return items


def find_game_folders(root: str, recursive: bool = True):
    """Yield folders that directly contain one or more .pkg files.

    recursive=True: full os.walk (legacy behaviour).
    recursive=False: root itself + immediate child folders only.
    """
    root = os.path.abspath(root)
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(root):
            if any(f.lower().endswith(".pkg") for f in filenames):
                yield dirpath
        return

    # Non-recursive: root + one level of children
    try:
        names = os.listdir(root)
    except OSError:
        return
    if any(n.lower().endswith(".pkg") for n in names):
        yield root
    for name in sorted(names):
        child = os.path.join(root, name)
        if not os.path.isdir(child):
            continue
        try:
            child_names = os.listdir(child)
        except OSError:
            continue
        if any(n.lower().endswith(".pkg") for n in child_names):
            yield child


def count_pkgs(folders) -> int:
    total = 0
    for folder in folders:
        try:
            total += sum(
                1 for f in os.listdir(folder) if f.lower().endswith(".pkg")
            )
        except OSError:
            pass
    return total


# ---------------------------------------------------------------------------
# Preflight + safe apply
# ---------------------------------------------------------------------------

def preflight_items(items: List[dict]) -> List[str]:
    """Return list of hard conflict messages. Empty = safe to apply."""
    errors = []
    # Planned targets within each folder
    by_folder: Dict[str, List[dict]] = {}
    for it in items:
        by_folder.setdefault(it["folder"], []).append(it)

    for folder, group in by_folder.items():
        targets: Dict[str, str] = {}  # new_name.lower() -> old filename
        for it in group:
            key = it["new_filename"].lower()
            if key in targets and targets[key] != it["filename"]:
                errors.append(
                    f"Conflict in {folder}: both "
                    f"'{targets[key]}' and '{it['filename']}' "
                    f"want '{it['new_filename']}'"
                )
            targets[key] = it["filename"]

            old_path = it["path"]
            new_path = os.path.join(folder, it["new_filename"])
            if same_path(old_path, new_path):
                continue
            if os.path.exists(new_path):
                # Another planned rename of that existing file is OK
                taking_it = any(
                    same_path(other["path"], new_path)
                    and not same_path(
                        os.path.join(other["folder"], other["new_filename"]),
                        new_path,
                    )
                    for other in group
                )
                if not taking_it:
                    # If the file at new_path is not one we are renaming away
                    source_of_target = next(
                        (
                            other
                            for other in group
                            if same_path(other["path"], new_path)
                        ),
                        None,
                    )
                    if source_of_target is None:
                        errors.append(
                            f"Target already exists: {new_path}"
                        )
    return errors


def two_phase_rename(ops: List[Tuple[str, str]], log: LogFn) -> List[Tuple[str, str]]:
    """Rename (old, new) pairs safely via unique temps. Returns successful pairs.

    Works for files or folders. Temp names keep the source extension (if any)
    so folder undos are not forced into a fake `.pkg` name.
    """
    if not ops:
        return []

    temps: List[Tuple[str, str, str]] = []  # old, temp, final
    done: List[Tuple[str, str]] = []

    try:
        for old, final in ops:
            if same_path(old, final):
                continue
            directory = os.path.dirname(old.rstrip(os.sep))
            _stem, ext = os.path.splitext(os.path.basename(old.rstrip(os.sep)))
            # Folders usually have no ext; files keep theirs
            if os.path.isdir(old):
                temp = os.path.join(directory, f".pkgrename_tmp_{uuid.uuid4().hex}")
            else:
                temp = os.path.join(
                    directory, f".pkgrename_tmp_{uuid.uuid4().hex}{ext}"
                )
            os.rename(old, temp)
            temps.append((old, temp, final))
        for old, temp, final in temps:
            if os.path.exists(final) and not same_path(temp, final):
                # Put back and skip
                os.rename(temp, old)
                log(f"  [SKIP] target exists after temp stage: {final}")
                continue
            os.rename(temp, final)
            done.append((old, final))
    except OSError as e:
        log(f"  [ERROR] Rename failed: {e}")
        # Best-effort rollback of remaining temps
        for old, temp, final in temps:
            if os.path.exists(temp) and not os.path.exists(old):
                try:
                    os.rename(temp, old)
                    log(f"  [ROLLBACK] restored {old}")
                except OSError as e2:
                    log(f"  [ERROR] rollback failed for {temp}: {e2}")
    return done


def write_undo_log(root: str, renames: List[dict], kind: str) -> Optional[str]:
    """Write undo log next to root (or its parent / home). renames: [{old, new, type}]"""
    if not renames:
        return None
    payload = {
        "version": 1,
        "app": APP_TITLE,
        "kind": kind,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "root": os.path.abspath(root),
        "renames": renames,
    }
    candidates = []
    abs_root = os.path.abspath(root)
    candidates.append(os.path.join(abs_root, UNDO_LOG_NAME))
    parent = os.path.dirname(abs_root)
    if parent and parent != abs_root:
        candidates.append(os.path.join(parent, UNDO_LOG_NAME))
    candidates.append(os.path.join(str(Path.home()), UNDO_LOG_NAME))

    for path in candidates:
        try:
            parent_dir = os.path.dirname(path)
            if parent_dir and not os.path.isdir(parent_dir):
                continue
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            return path
        except OSError:
            continue
    return None


def load_undo_log(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def apply_undo(log_path: str, log: LogFn) -> dict:
    data = load_undo_log(log_path)
    if not data or "renames" not in data:
        log("No valid undo log found.")
        return {"undone": 0, "failed": 0}

    renames = list(data["renames"])
    # Undo files first (while they still sit under the renamed folder),
    # then folders. Within each group, reverse apply order.
    files = [r for r in renames if r.get("type") != "folder"]
    folders = [r for r in renames if r.get("type") == "folder"]
    ordered = list(reversed(files)) + list(reversed(folders))

    undone = 0
    failed = 0
    for r in ordered:
        new_path = r.get("new")
        old_path = r.get("old")
        rtype = r.get("type", "file")
        if not new_path or not old_path:
            continue
        if not os.path.exists(new_path):
            log(f"  [SKIP] missing: {new_path}")
            failed += 1
            continue
        if os.path.exists(old_path) and not same_path(old_path, new_path):
            log(f"  [SKIP] original path occupied: {old_path}")
            failed += 1
            continue
        log(
            f"  undo ({rtype}): {os.path.basename(new_path.rstrip(os.sep))}  ->  "
            f"{os.path.basename(old_path.rstrip(os.sep))}"
        )
        try:
            # Single rename is enough; two-phase not required one-at-a-time
            os.rename(new_path, old_path)
            undone += 1
        except OSError as e:
            log(f"  [ERROR] undo failed: {e}")
            failed += 1

    log(f"\nUndone: {undone} / planned {len(ordered)} (failed/skipped: {failed})")
    return {"undone": undone, "failed": failed}


def export_preview(path: str, lines: List[str]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main rename operations
# ---------------------------------------------------------------------------

def rename_games(
    root: str,
    apply: bool,
    folders: bool,
    scan_mb: int,
    recursive: bool,
    template_key: str,
    log: LogFn,
    progress: Optional[ProgressFn] = None,
) -> dict:
    root = os.path.abspath(root)
    summary = {
        "total": 0,
        "unknown": 0,
        "header": 0,
        "scan": 0,
        "unchanged": 0,
        "planned": 0,
        "renamed": 0,
        "skipped": 0,
        "conflicts": 0,
        "folders_renamed": 0,
        "errors": [],
    }

    if not os.path.isdir(root):
        log(f"Folder does not exist: {root}")
        summary["errors"].append("path missing")
        return summary

    all_folders = list(find_game_folders(root, recursive=recursive))
    if not all_folders:
        log("No .pkg files found under that path.")
        if not recursive:
            log("(Tip: enable 'Scan subfolders' if packages are deeper.)")
        return summary

    total_pkgs = count_pkgs(all_folders)
    log(f"Found {len(all_folders)} folder(s), {total_pkgs} .pkg file(s).")
    log(f"SFO: header first, scan fallback (max {scan_mb} MB).")
    log(f"Template: {NAME_PRESETS.get(template_key, (template_key,))[0]}")
    log(f"Scan mode: {'recursive' if recursive else 'root + 1 level only'}")
    log("")

    all_items: List[dict] = []
    done_count = 0
    for folder in sorted(all_folders):
        n_here = sum(
            1 for f in os.listdir(folder) if f.lower().endswith(".pkg")
        )
        items = scan_game_folder(
            folder,
            scan_mb,
            template_key=template_key,
            log=log,
            progress=progress,
            progress_base=done_count,
            progress_total=total_pkgs,
        )
        done_count += n_here
        if not items:
            continue

        log(f"\n== {folder} ==")
        for it in items:
            method = it.get("sfo_method", "none")
            if method == "header":
                summary["header"] += 1
                flag = "  [header]"
            elif method == "scan":
                summary["scan"] += 1
                flag = "  [scan fallback]"
            else:
                summary["unknown"] += 1
                flag = "  [!] no metadata — using filename fallback"

            same = nfc(it["filename"]) == nfc(it["new_filename"])
            arrow = "  (unchanged)" if same else ""
            log(f"  {it['filename']}  ->  {it['new_filename']}{arrow}{flag}")
            if same:
                summary["unchanged"] += 1
            else:
                summary["planned"] += 1
            summary["total"] += 1
            all_items.append(it)

    if progress and total_pkgs:
        progress(total_pkgs, total_pkgs, "done")

    conflicts = preflight_items(all_items)
    if conflicts:
        summary["conflicts"] = len(conflicts)
        log("\n--- PREFLIGHT CONFLICTS ---")
        for c in conflicts:
            log(f"  [CONFLICT] {c}")
        if apply:
            log("\nApply aborted — fix conflicts first.")
            summary["errors"].extend(conflicts)
            _log_summary(summary, apply, log)
            return summary

    if not apply:
        log("\n(Simulation only. Nothing was modified.)")
        _log_summary(summary, apply, log)
        summary["_items"] = all_items  # for export
        return summary

    # Apply renames (two-phase per folder)
    undo_records = []
    for folder, group in _group_by_folder(all_items).items():
        ops = []
        for it in group:
            old_path = it["path"]
            new_path = os.path.join(folder, it["new_filename"])
            if same_path(old_path, new_path):
                continue
            ops.append((old_path, new_path))

        if not ops:
            continue
        done = two_phase_rename(ops, log)
        summary["renamed"] += len(done)
        summary["skipped"] += len(ops) - len(done)
        for old, new in done:
            undo_records.append({"old": old, "new": new, "type": "file"})
            # update path for folder rename base
            for it in group:
                if same_path(it["path"], old):
                    it["path"] = new

        if folders and group:
            base_title = finalize_filename(group[0]["base_title"])
            parent = os.path.dirname(folder.rstrip(os.sep))
            new_folder = os.path.join(parent, base_title)
            if not same_path(new_folder, folder.rstrip(os.sep)):
                if os.path.exists(new_folder):
                    log(f"  [SKIP] folder target exists: {new_folder}")
                else:
                    try:
                        os.rename(folder, new_folder)
                        log(f"  Folder renamed -> {new_folder}")
                        summary["folders_renamed"] += 1
                        # File undo paths must reflect post-folder-rename location
                        # so undo can restore names inside new_folder first, then
                        # rename the folder back.
                        for rec in undo_records:
                            if rec.get("type") != "file":
                                continue
                            for key in ("old", "new"):
                                p = rec.get(key, "")
                                if p:
                                    rec[key] = os.path.join(
                                        new_folder, os.path.basename(p)
                                    )
                        undo_records.append(
                            {"old": folder, "new": new_folder, "type": "folder"}
                        )
                    except OSError as e:
                        log(f"  [ERROR] folder rename failed: {e}")

    # Prefer writing undo log next to the scan root's parent if root was a
    # per-game folder that may have been renamed away.
    undo_root = root
    if not os.path.isdir(undo_root):
        undo_root = os.path.dirname(os.path.abspath(root)) or root
    undo_path = write_undo_log(undo_root, undo_records, kind="rename_games")
    if undo_path:
        log(f"\nUndo log written: {undo_path}")
        summary["undo_log"] = undo_path

    _log_summary(summary, apply, log)
    summary["_items"] = all_items
    return summary


def _group_by_folder(items: List[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for it in items:
        out.setdefault(it["folder"], []).append(it)
    return out


def _log_summary(summary: dict, apply: bool, log: LogFn) -> None:
    log("\n---------- SUMMARY ----------")
    log(f"Total pkgs analyzed : {summary['total']}")
    log(f"  SFO via header    : {summary['header']}")
    log(f"  SFO via scan      : {summary['scan']}")
    log(f"  No metadata       : {summary['unknown']}")
    log(f"Already correct     : {summary['unchanged']}")
    log(f"Would rename        : {summary['planned']}")
    if summary["conflicts"]:
        log(f"Conflicts           : {summary['conflicts']}")
    if apply:
        log(f"Renamed             : {summary['renamed']}")
        log(f"Skipped             : {summary['skipped']}")
        log(f"Folders renamed     : {summary['folders_renamed']}")
    else:
        log("(Preview mode — disk unchanged)")
    log("-----------------------------")


def push_to_end(
    root: str,
    prefix: str,
    apply: bool,
    recursive: bool,
    log: LogFn,
    progress: Optional[ProgressFn] = None,
) -> dict:
    root = os.path.abspath(root)
    summary = {
        "total": 0,
        "already": 0,
        "planned": 0,
        "renamed": 0,
        "skipped": 0,
        "conflicts": 0,
        "errors": [],
    }

    if not os.path.isdir(root):
        log(f"Folder does not exist: {root}")
        return summary

    paths = []
    if recursive:
        for dirpath, _dn, filenames in os.walk(root):
            for fname in sorted(filenames):
                if fname.lower().endswith(".pkg"):
                    paths.append(os.path.join(dirpath, fname))
    else:
        try:
            for fname in sorted(os.listdir(root)):
                if fname.lower().endswith(".pkg"):
                    paths.append(os.path.join(root, fname))
        except OSError as e:
            log(f"[ERROR] {e}")
            return summary

    summary["total"] = len(paths)
    ops = []
    for i, old_path in enumerate(paths):
        if progress:
            progress(i, len(paths) or 1, os.path.basename(old_path))
        fname = os.path.basename(old_path)
        if fname.startswith(prefix):
            summary["already"] += 1
            log(f"  {fname}  (already has prefix)")
            continue
        new_name = prefix + fname
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        log(f"  {fname}  ->  {new_name}")
        summary["planned"] += 1
        ops.append((old_path, new_path))

    if progress and paths:
        progress(len(paths), len(paths), "done")

    # Preflight: target exists
    conflicts = []
    for old, new in ops:
        if os.path.exists(new) and not same_path(old, new):
            conflicts.append(f"Target exists: {new}")
    if conflicts:
        summary["conflicts"] = len(conflicts)
        for c in conflicts:
            log(f"  [CONFLICT] {c}")
        if apply:
            log("\nApply aborted — fix conflicts first.")
            _push_summary(summary, apply, log)
            return summary

    if not apply:
        log("\n(Simulation only. Nothing was modified.)")
        _push_summary(summary, apply, log)
        return summary

    done = two_phase_rename(ops, log)
    summary["renamed"] = len(done)
    summary["skipped"] = len(ops) - len(done)
    undo = [{"old": o, "new": n, "type": "file"} for o, n in done]
    undo_path = write_undo_log(root, undo, kind="push_to_end")
    if undo_path:
        log(f"\nUndo log written: {undo_path}")
        summary["undo_log"] = undo_path

    _push_summary(summary, apply, log)
    return summary


def _push_summary(summary: dict, apply: bool, log: LogFn) -> None:
    log("\n---------- SUMMARY ----------")
    log(f"Total .pkg found    : {summary['total']}")
    log(f"Already prefixed    : {summary['already']}")
    log(f"Would rename        : {summary['planned']}")
    if summary["conflicts"]:
        log(f"Conflicts           : {summary['conflicts']}")
    if apply:
        log(f"Renamed             : {summary['renamed']}")
        log(f"Skipped             : {summary['skipped']}")
    else:
        log("(Preview mode — disk unchanged)")
    log("-----------------------------")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class PS4PkgRenamerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} {APP_VERSION}")
        self.geometry("780x640")
        self.minsize(680, 520)

        self._worker: Optional[threading.Thread] = None
        self._msg_q: queue.Queue = queue.Queue()
        self._busy = False
        self._last_preview_lines: List[str] = []
        self._last_summary: dict = {}

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self.games_tab = ttk.Frame(notebook)
        self.themes_tab = ttk.Frame(notebook)
        notebook.add(self.games_tab, text="Rename Games")
        notebook.add(self.themes_tab, text="Push to End")

        self._build_games_tab()
        self._build_themes_tab()
        self._build_status_bar()

        self.after(100, self._drain_queue)

    # --- status / progress ---
    def _build_status_bar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)

        self.summary_var = tk.StringVar(value="Ready.")
        ttk.Label(bar, textvariable=self.summary_var).pack(side="left")

        self.progress = ttk.Progressbar(bar, mode="determinate", length=220)
        self.progress.pack(side="right", padx=(8, 0))
        self.progress_label = ttk.Label(bar, text="")
        self.progress_label.pack(side="right")

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in self._action_buttons:
            btn.configure(state=state)

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self._msg_q.get_nowait()
                if kind == "log":
                    widget, message = payload
                    self._log_direct(widget, message)
                elif kind == "progress":
                    current, total, name = payload
                    self._update_progress(current, total, name)
                elif kind == "done":
                    widget, summary, apply = payload
                    self._on_job_done(widget, summary, apply)
                elif kind == "error":
                    messagebox.showerror(APP_TITLE, str(payload))
                    self._set_busy(False)
                    self.summary_var.set("Error.")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _update_progress(self, current: int, total: int, name: str):
        total = max(total, 1)
        self.progress["maximum"] = total
        self.progress["value"] = min(current, total)
        short = name if len(name) < 40 else name[:37] + "..."
        self.progress_label.configure(text=f"{current}/{total} {short}")

    def _on_job_done(self, widget, summary: dict, apply: bool):
        self._set_busy(False)
        self._last_summary = summary or {}
        self._update_summary_label(summary, apply)
        self.progress_label.configure(text="")
        if summary.get("conflicts"):
            self.summary_var.set(
                f"Conflicts: {summary['conflicts']} — see log. "
                f"{self.summary_var.get()}"
            )

    def _update_summary_label(self, summary: dict, apply: bool):
        if not summary:
            self.summary_var.set("Ready.")
            return
        parts = [
            f"Total {summary.get('total', 0)}",
            f"header {summary.get('header', 0)}",
            f"scan {summary.get('scan', 0)}",
            f"no-meta {summary.get('unknown', 0)}",
        ]
        if apply:
            parts.append(f"renamed {summary.get('renamed', 0)}")
        else:
            parts.append(f"planned {summary.get('planned', 0)}")
        if summary.get("conflicts"):
            parts.append(f"CONFLICTS {summary['conflicts']}")
        self.summary_var.set(" | ".join(parts))

    # --- Rename Games tab ---
    def _build_games_tab(self):
        frame = self.games_tab
        self._action_buttons = []

        path_frame = ttk.Frame(frame)
        path_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(
            path_frame, text="Target folder (Games / Homebrew / Emulators):"
        ).pack(anchor="w")

        picker = ttk.Frame(path_frame)
        picker.pack(fill="x", pady=(5, 0))
        self.games_path_var = tk.StringVar()
        ttk.Entry(picker, textvariable=self.games_path_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            picker,
            text="Browse...",
            command=lambda: self._browse_folder(self.games_path_var),
        ).pack(side="left", padx=(5, 0))

        options = ttk.Frame(frame)
        options.pack(fill="x", padx=10, pady=(0, 6))
        self.folders_var = tk.BooleanVar(value=True)
        self.recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options, text="Also rename folders", variable=self.folders_var
        ).pack(side="left")
        ttk.Checkbutton(
            options,
            text="Scan subfolders",
            variable=self.recursive_var,
        ).pack(side="left", padx=(12, 0))

        tmpl_row = ttk.Frame(frame)
        tmpl_row.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(tmpl_row, text="Name template:").pack(side="left")
        self.template_var = tk.StringVar(value="goldhen")
        tmpl_combo = ttk.Combobox(
            tmpl_row,
            textvariable=self.template_var,
            state="readonly",
            width=36,
            values=[f"{k} — {v[0]}" for k, v in NAME_PRESETS.items()],
        )
        tmpl_combo.current(0)
        tmpl_combo.pack(side="left", padx=(5, 0))
        self._template_combo = tmpl_combo

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=10, pady=(0, 6))
        b_prev = ttk.Button(buttons, text="Preview", command=self._preview_games)
        b_prev.pack(side="left")
        b_apply = ttk.Button(buttons, text="Apply", command=self._apply_games)
        b_apply.pack(side="left", padx=(5, 0))
        b_export = ttk.Button(
            buttons, text="Export preview...", command=self._export_games_preview
        )
        b_export.pack(side="left", padx=(5, 0))
        b_undo = ttk.Button(
            buttons, text="Undo last...", command=self._undo_last
        )
        b_undo.pack(side="left", padx=(5, 0))
        self._action_buttons.extend([b_prev, b_apply, b_export, b_undo])

        self.games_log = scrolledtext.ScrolledText(
            frame, state="disabled", wrap="word", font=("Consolas", 9)
        )
        self.games_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._configure_log_tags(self.games_log)

    def _selected_template_key(self) -> str:
        raw = self.template_var.get()
        key = raw.split(" — ", 1)[0].strip()
        return key if key in NAME_TEMPLATES else "goldhen"

    def _preview_games(self):
        self._run_games(apply=False)

    def _apply_games(self):
        if not messagebox.askyesno(
            APP_TITLE,
            "This will rename .pkg files (and folders, if checked) on disk.\n\n"
            "A two-phase rename is used and an undo log will be written.\n\n"
            "Continue?",
        ):
            return
        self._run_games(apply=True)

    def _run_games(self, apply: bool):
        if self._busy:
            return
        target_dir = self.games_path_var.get().strip()
        if not target_dir or not os.path.isdir(target_dir):
            messagebox.showerror(APP_TITLE, f"Path not found:\n{target_dir}")
            return

        self._clear_log(self.games_log)
        self._last_preview_lines = []
        self._set_busy(True)
        self.summary_var.set("Working...")
        self.progress["value"] = 0

        template_key = self._selected_template_key()
        folders = self.folders_var.get()
        recursive = self.recursive_var.get()
        log_widget = self.games_log

        def worker():
            lines_capture = []

            def log(msg: str):
                lines_capture.append(msg)
                self._msg_q.put(("log", (log_widget, msg)))

            def prog(cur, total, name):
                self._msg_q.put(("progress", (cur, total, name)))

            try:
                summary = rename_games(
                    target_dir,
                    apply=apply,
                    folders=folders,
                    scan_mb=DEFAULT_SCAN_MB,
                    recursive=recursive,
                    template_key=template_key,
                    log=log,
                    progress=prog,
                )
                self._last_preview_lines = lines_capture
                self._msg_q.put(("done", (log_widget, summary, apply)))
            except Exception as e:
                self._msg_q.put(("error", str(e)))
                self._msg_q.put(("done", (log_widget, {}, apply)))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _export_games_preview(self):
        if not self._last_preview_lines:
            messagebox.showinfo(
                APP_TITLE, "Run Preview first, then export the log."
            )
            return
        path = filedialog.asksaveasfilename(
            title="Export preview",
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("CSV", "*.csv"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                rows = ["old,new,note"]
                for line in self._last_preview_lines:
                    if "  ->  " in line and not line.strip().startswith("=="):
                        left, right = line.strip().split("  ->  ", 1)
                        # strip flags
                        note = ""
                        for tag in (
                            "  [header]",
                            "  [scan fallback]",
                            "  [!] no metadata — using filename fallback",
                            "  (unchanged)",
                        ):
                            if tag in right:
                                note = tag.strip()
                                right = right.replace(tag, "")
                        rows.append(
                            f'"{left.strip()}","{right.strip()}","{note}"'
                        )
                export_preview(path, rows)
            else:
                export_preview(path, self._last_preview_lines)
            messagebox.showinfo(APP_TITLE, f"Exported to:\n{path}")
        except OSError as e:
            messagebox.showerror(APP_TITLE, str(e))

    def _undo_last(self):
        if self._busy:
            return
        # Prefer undo log inside selected folder, else pick file
        candidates = []
        for path_var in (self.games_path_var, self.themes_path_var):
            d = path_var.get().strip()
            if d:
                p = os.path.join(d, UNDO_LOG_NAME)
                if os.path.isfile(p):
                    candidates.append(p)
        home = os.path.join(str(Path.home()), UNDO_LOG_NAME)
        if os.path.isfile(home):
            candidates.append(home)

        path = None
        if candidates:
            path = candidates[0]
            use = messagebox.askyesnocancel(
                APP_TITLE,
                f"Undo log found:\n{path}\n\n"
                "Yes = use this log\nNo = choose another file\nCancel = abort",
            )
            if use is None:
                return
            if use is False:
                path = None
        if not path:
            path = filedialog.askopenfilename(
                title="Select undo log",
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
            )
        if not path:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "This will reverse renames from the undo log.\n\nContinue?",
        ):
            return

        self._clear_log(self.games_log)
        self._set_busy(True)
        log_widget = self.games_log

        def worker():
            def log(msg: str):
                self._msg_q.put(("log", (log_widget, msg)))

            try:
                log(f"Undoing from: {path}\n")
                summary = apply_undo(path, log)
                self._msg_q.put(
                    ("done", (log_widget, {"total": summary.get("undone", 0),
                                           "renamed": summary.get("undone", 0),
                                           "planned": 0, "header": 0,
                                           "scan": 0, "unknown": 0}, True))
                )
            except Exception as e:
                self._msg_q.put(("error", str(e)))
                self._msg_q.put(("done", (log_widget, {}, True)))

        threading.Thread(target=worker, daemon=True).start()

    # --- Push to End tab ---
    def _build_themes_tab(self):
        frame = self.themes_tab

        path_frame = ttk.Frame(frame)
        path_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(path_frame, text="Target folder (e.g. Themes):").pack(anchor="w")

        picker = ttk.Frame(path_frame)
        picker.pack(fill="x", pady=(5, 0))
        self.themes_path_var = tk.StringVar()
        ttk.Entry(picker, textvariable=self.themes_path_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            picker,
            text="Browse...",
            command=lambda: self._browse_folder(self.themes_path_var),
        ).pack(side="left", padx=(5, 0))

        opts = ttk.Frame(frame)
        opts.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(opts, text="Prefix:").pack(side="left")
        self.prefix_var = tk.StringVar(value=DEFAULT_PREFIX)
        ttk.Entry(opts, textvariable=self.prefix_var, width=20).pack(
            side="left", padx=(5, 12)
        )
        self.themes_recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="Scan subfolders", variable=self.themes_recursive_var
        ).pack(side="left")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=10, pady=(0, 6))
        b1 = ttk.Button(buttons, text="Preview", command=self._preview_themes)
        b1.pack(side="left")
        b2 = ttk.Button(buttons, text="Apply", command=self._apply_themes)
        b2.pack(side="left", padx=(5, 0))
        b3 = ttk.Button(
            buttons, text="Export preview...", command=self._export_themes_preview
        )
        b3.pack(side="left", padx=(5, 0))
        self._action_buttons.extend([b1, b2, b3])

        self.themes_log = scrolledtext.ScrolledText(
            frame, state="disabled", wrap="word", font=("Consolas", 9)
        )
        self.themes_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self._configure_log_tags(self.themes_log)

    def _preview_themes(self):
        self._run_themes(apply=False)

    def _apply_themes(self):
        if not messagebox.askyesno(
            APP_TITLE,
            "This will rename .pkg files on disk by adding the prefix.\n\nContinue?",
        ):
            return
        self._run_themes(apply=True)

    def _run_themes(self, apply: bool):
        if self._busy:
            return
        target_dir = self.themes_path_var.get().strip()
        if not target_dir or not os.path.isdir(target_dir):
            messagebox.showerror(APP_TITLE, f"Path not found:\n{target_dir}")
            return
        prefix = self.prefix_var.get()
        if not prefix:
            messagebox.showerror(APP_TITLE, "Prefix cannot be empty.")
            return

        self._clear_log(self.themes_log)
        self._last_preview_lines = []
        self._set_busy(True)
        self.summary_var.set("Working...")
        recursive = self.themes_recursive_var.get()
        log_widget = self.themes_log

        def worker():
            lines_capture = []

            def log(msg: str):
                lines_capture.append(msg)
                self._msg_q.put(("log", (log_widget, msg)))

            def prog(cur, total, name):
                self._msg_q.put(("progress", (cur, total, name)))

            try:
                summary = push_to_end(
                    target_dir,
                    prefix=prefix,
                    apply=apply,
                    recursive=recursive,
                    log=log,
                    progress=prog,
                )
                self._last_preview_lines = lines_capture
                self._msg_q.put(("done", (log_widget, summary, apply)))
            except Exception as e:
                self._msg_q.put(("error", str(e)))
                self._msg_q.put(("done", (log_widget, {}, apply)))

        threading.Thread(target=worker, daemon=True).start()

    def _export_themes_preview(self):
        self._export_games_preview()  # same capture buffer

    # --- shared helpers ---
    def _configure_log_tags(self, widget):
        widget.tag_configure("error", foreground="#b00020")
        widget.tag_configure("warn", foreground="#9a5b00")
        widget.tag_configure("ok", foreground="#0b6b0b")
        widget.tag_configure("meta", foreground="#333399")

    def _browse_folder(self, path_var):
        folder = filedialog.askdirectory(title="Select target folder")
        if folder:
            path_var.set(folder)

    def _clear_log(self, widget):
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.configure(state="disabled")

    def _log_direct(self, widget, message: str):
        widget.configure(state="normal")
        tag = None
        if "[ERROR]" in message or "[CONFLICT]" in message:
            tag = "error"
        elif "[!]" in message or "[SKIP]" in message or "[scan fallback]" in message:
            tag = "warn"
        elif "[header]" in message or "SUMMARY" in message:
            tag = "meta"
        elif "Renamed" in message and ":" in message:
            tag = "ok"
        if tag:
            widget.insert(tk.END, message + "\n", tag)
        else:
            widget.insert(tk.END, message + "\n")
        widget.configure(state="disabled")
        widget.see(tk.END)


def main():
    app = PS4PkgRenamerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
