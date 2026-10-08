#!/usr/bin/env python3
# Route 2 V2 - iOS 27 Mercury Configuration Extractor
#
# READ-ONLY device-side behavior:
# - Opens a lockdown/usbmux connection.
# - Requests a targeted MobileBackup2 backup stream.
# - Keeps PosterBoard / PRBPosterExtensionDataStore payloads on the PC.
# - Reads Manifest.db locally and copies selected Mercury configuration payloads.
# - NEVER calls restore/rebuild/write-back APIs.
#
# Targeted candidates confirmed by the user's previous iOS 27 Manifest:
#   0AEA1C8E-3EA7-4E6E-81E4-511972087A54
#   769BEBC4-9C5E-4A57-84C6-939C9C90C521
#
# C90158DC-CC4B-4B6B-B86B-997F87CEF0DA is intentionally excluded from
# extraction because it is a previously modified experimental configuration.

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service


TOOL_NAME = "Route 2 V2 - iOS 27 Mercury Configuration Extractor"
PREFERRED_UUIDS = [
    "0AEA1C8E-3EA7-4E6E-81E4-511972087A54",
    "769BEBC4-9C5E-4A57-84C6-939C9C90C521",
]
EXCLUDED_UUIDS = {
    "C90158DC-CC4B-4B6B-B86B-997F87CEF0DA",
}
PROVIDERS = (
    "com.apple.MercuryPoster",
    "com.apple.Posters.MercuryPosterApp",
)
CORE_ROOT_FILES = (
    "com.apple.posterkit.provider.descriptor.identifier",
    "com.apple.posterkit.provider.identifierURL.suggestionMetadata.plist",
    "com.apple.posterkit.role.identifier",
    "providerInfo.plist",
)
CORE_CONTENT_FILES = (
    ".com.apple.posterkit.provider.contents.configurableOptions.plist",
    "com.apple.posterkit.provider.contents.otherMetadata.plist",
    "com.apple.posterkit.provider.contents.userInfo",
)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def find_manifest(backup_root: Path) -> Path | None:
    for p in backup_root.rglob("Manifest.db"):
        if p.is_file():
            return p
    return None


def open_manifest_rows(manifest: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{manifest}?mode=ro", uri=True)
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(Files)").fetchall()]
        if not columns:
            raise RuntimeError("Manifest.db contains no Files table columns.")

        requested = ["domain", "relativePath", "fileID", "flags"]
        present = [c for c in requested if c in columns]
        if "relativePath" not in present or "fileID" not in present or "flags" not in present:
            raise RuntimeError(f"Unexpected Files table columns: {columns}")

        sql = "SELECT " + ", ".join(present) + " FROM Files"
        raw_rows = conn.execute(sql).fetchall()
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            row = dict(zip(present, raw))
            row.setdefault("domain", "")
            rows.append(row)
        return rows
    finally:
        conn.close()


def payload_source(device_dir: Path, file_id: str) -> Path | None:
    if not file_id:
        return None

    candidates = [
        device_dir / file_id[:2] / file_id,
        device_dir / file_id,
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def copy_payload(
    device_dir: Path,
    row: dict[str, Any],
    dst: Path,
    inventory: list[dict[str, Any]],
    missing: list[dict[str, Any]],
) -> bool:
    file_id = safe_text(row.get("fileID"))
    src = payload_source(device_dir, file_id)
    if src is None:
        missing.append({
            "domain": safe_text(row.get("domain")),
            "relativePath": safe_text(row.get("relativePath")),
            "fileID": file_id,
            "reason": "payload file was not retained by the filtered backup",
        })
        return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    inventory.append({
        "domain": safe_text(row.get("domain")),
        "relativePath": safe_text(row.get("relativePath")),
        "fileID": file_id,
        "output": str(dst),
        "size": dst.stat().st_size,
        "sha256": sha256_file(dst),
    })
    return True


def relative_parts(path: str) -> list[str]:
    return [p for p in path.replace("\\", "/").split("/") if p]


def detect_mercury_config(
    relative_path: str,
) -> tuple[str, str, str, str] | None:
    """
    Return (structure_version, provider, uuid, suffix_after_uuid)
    when relative_path is inside a Mercury configuration tree.
    """
    rp = relative_path.replace("\\", "/")
    providers_pattern = "|".join(re.escape(p) for p in PROVIDERS)
    pattern = re.compile(
        rf"(?:^|/)PRBPosterExtensionDataStore/([^/]+)/Extensions/"
        rf"({providers_pattern})/configurations/"
        rf"([0-9A-Fa-f]{{8}}-[0-9A-Fa-f]{{4}}-[0-9A-Fa-f]{{4}}-"
        rf"[0-9A-Fa-f]{{4}}-[0-9A-Fa-f]{{12}})(?:/(.*))?$"
    )
    m = pattern.search(rp)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).upper(), m.group(4) or ""


def detect_provider_root(
    relative_path: str,
) -> tuple[str, str, str] | None:
    """
    Return (structure_version, provider, suffix_after_provider)
    for files directly under a Mercury provider tree.
    """
    rp = relative_path.replace("\\", "/")
    providers_pattern = "|".join(re.escape(p) for p in PROVIDERS)
    pattern = re.compile(
        rf"(?:^|/)PRBPosterExtensionDataStore/([^/]+)/Extensions/"
        rf"({providers_pattern})(?:/(.*))?$"
    )
    m = pattern.search(rp)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3) or ""


def detect_store_db(relative_path: str) -> bool:
    rp = relative_path.replace("\\", "/")
    return (
        "PRBPosterExtensionDataStore/" in rp
        and "PBFPosterExtensionDataStoreSQLiteDatabase.sqlite3" in rp
    )


def choose_version(config_rows: list[dict[str, Any]], uuid: str) -> str | None:
    versions: set[str] = set()
    needle = f"/configurations/{uuid}/versions/"
    for row in config_rows:
        rp = safe_text(row.get("relativePath")).replace("\\", "/")
        upper = rp.upper()
        pos = upper.find(needle.upper())
        if pos < 0:
            continue
        tail = rp[pos + len(needle):]
        version = tail.split("/", 1)[0]
        if version:
            versions.add(version)

    if not versions:
        return None

    numeric: list[tuple[int, str]] = []
    other: list[str] = []
    for v in versions:
        try:
            numeric.append((int(v), v))
        except ValueError:
            other.append(v)

    if numeric:
        return max(numeric)[1]
    return sorted(other)[-1]


def locate_exact_row(
    rows: list[dict[str, Any]],
    expected_relative_path: str,
) -> dict[str, Any] | None:
    expected = expected_relative_path.replace("\\", "/")
    for row in rows:
        if safe_text(row.get("relativePath")).replace("\\", "/") == expected:
            return row
    return None


def config_root_prefix(relative_path: str, uuid: str) -> str | None:
    rp = relative_path.replace("\\", "/")
    marker = f"/configurations/{uuid}"
    idx = rp.upper().find(marker.upper())
    if idx < 0:
        return None
    return rp[: idx + len(marker)]


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def make_zip(capture_dir: Path, backup_root: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as zf:
        for p in sorted(capture_dir.rglob("*")):
            if not p.is_file():
                continue
            if p == zip_path:
                continue
            try:
                p.relative_to(backup_root)
                continue
            except ValueError:
                pass
            zf.write(p, p.relative_to(capture_dir))


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    script_dir = Path(__file__).resolve().parent
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    capture_root = script_dir / "Route2-V2-Captures"
    capture_dir = capture_root / f"Route2-V2-{stamp}"
    backup_root = capture_dir / "_working_backup"
    extracted_root = capture_dir / "Extracted"
    full_root = extracted_root / "FullConfigurations"
    core_root = extracted_root / "Core7Candidates"
    support_root = extracted_root / "Support"

    capture_dir.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)

    log_path = capture_dir / "extract.log"
    log_fp = log_path.open("w", encoding="utf-8")

    def log(message: str = "") -> None:
        print(message, flush=True)
        log_fp.write(message + "\n")
        log_fp.flush()

    log("=" * 68)
    log(TOOL_NAME)
    log("READ-ONLY: backup/read/copy on PC only; NO restore / NO rebuild / NO write-back")
    log("=" * 68)

    lockdown = None
    device_info: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    inventory: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    selected_uuids: list[str] = []
    complete_core7: list[str] = []
    providers_found: list[str] = []
    structure_versions: list[str] = []
    zip_path: Path | None = None
    status = "ERROR"
    error_message = ""

    try:
        log("\n[1/6] Connecting to iPhone...")
        lockdown = await create_using_usbmux(
            serial=None,
            autopair=True,
            pair_timeout=120.0,
        )
        if not getattr(lockdown, "paired", False):
            raise RuntimeError("Device did not confirm trust/pairing.")

        values = getattr(lockdown, "all_values", {}) or {}
        device_info = {
            "ProductType": values.get("ProductType"),
            "ProductVersion": values.get("ProductVersion"),
            "BuildVersion": values.get("BuildVersion"),
        }
        log(
            f"[OK] {device_info.get('ProductType') or 'iPhone'} "
            f"iOS {device_info.get('ProductVersion') or '?'} "
            f"({device_info.get('BuildVersion') or '?'})"
        )

        (capture_dir / "device-info.json").write_text(
            json.dumps(device_info, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        log("\n[2/6] Starting targeted MobileBackup2 read...")
        log("      PosterBoard / PRBPosterExtensionDataStore payloads are kept on PC.")
        log("      Keep the iPhone unlocked and awake. Enter the device passcode if asked.")

        last_pct = -1

        def progress_cb(value: Any) -> None:
            nonlocal last_pct
            try:
                pct = int(round(float(value)))
            except Exception:
                return
            pct = max(0, min(100, pct))
            if pct != last_pct:
                print(f"[backup] {pct}%", flush=True)
                log_fp.write(f"[backup] {pct}%\n")
                log_fp.flush()
                last_pct = pct

        def pb_only(backup_file: Any) -> bool:
            device_name = safe_text(getattr(backup_file, "device_name", None))
            relative_path = safe_text(getattr(backup_file, "relative_path", None))
            domain = safe_text(getattr(backup_file, "domain", None))
            haystack = "\n".join((device_name, relative_path, domain))
            return (
                "PRBPosterExtensionDataStore" in haystack
                or "PosterBoard" in haystack
                or "com.apple.MercuryPoster" in haystack
                or "com.apple.Posters.MercuryPosterApp" in haystack
            )

        async with Mobilebackup2Service(lockdown) as backup_client:
            await backup_client.backup(
                full=True,
                backup_directory=backup_root,
                progress_callback=progress_cb,
                filter_callback=pb_only,
            )

        log("[OK] Device backup stream finished.")

        log("\n[3/6] Reading Manifest.db and locating Mercury configurations...")
        manifest = find_manifest(backup_root)
        if manifest is None:
            raise RuntimeError("Manifest.db was not found after MobileBackup2 finished.")

        device_dir = manifest.parent
        rows = open_manifest_rows(manifest)

        candidate_lines: list[str] = []
        mercury_lines: list[str] = []
        configs: dict[str, dict[str, Any]] = {}

        for row in rows:
            domain = safe_text(row.get("domain"))
            rp = safe_text(row.get("relativePath"))
            file_id = safe_text(row.get("fileID"))
            flags = row.get("flags")
            line = f"{domain}\t{rp}\tflags={flags}\tfileID={file_id}"

            if (
                "PosterBoard" in domain
                or "PosterBoard" in rp
                or "PRBPosterExtensionDataStore" in rp
            ):
                candidate_lines.append(line)

            if "MercuryPoster" in domain or "MercuryPoster" in rp:
                mercury_lines.append(line)

            detected = detect_mercury_config(rp)
            if detected:
                structure_version, provider, uuid, suffix = detected
                item = configs.setdefault(
                    uuid,
                    {
                        "uuid": uuid,
                        "provider": provider,
                        "structure_version": structure_version,
                        "rows": [],
                    },
                )
                item["rows"].append(row)

        write_lines(capture_dir / "manifest_candidates.txt", candidate_lines)
        write_lines(capture_dir / "manifest_mercury_rows.txt", mercury_lines)

        log(f"[INFO] Manifest rows: {len(rows)}")
        log(f"[INFO] PosterBoard candidate rows: {len(candidate_lines)}")
        log(f"[INFO] Mercury configuration UUIDs found: {len(configs)}")

        found_uuids = sorted(configs)
        if found_uuids:
            log("[INFO] UUIDs: " + ", ".join(found_uuids))

        preferred_present = [u for u in PREFERRED_UUIDS if u in configs]
        if preferred_present:
            selected_uuids = preferred_present
        else:
            selected_uuids = [
                u for u in found_uuids
                if u.upper() not in EXCLUDED_UUIDS
            ]
            if selected_uuids:
                log("[WARN] Preferred UUIDs were not found; using other non-C901 candidates.")

        selected_uuids = [
            u for u in selected_uuids
            if u.upper() not in EXCLUDED_UUIDS
        ]

        if not selected_uuids:
            raise RuntimeError(
                "No non-C901 Mercury configuration candidate was present in this backup."
            )

        log("[TARGET] Extracting: " + ", ".join(selected_uuids))
        log("[EXCLUDE] C90158DC-CC4B-4B6B-B86B-997F87CEF0DA")

        log("\n[4/6] Copying full selected configuration payloads...")
        for uuid in selected_uuids:
            cfg = configs[uuid]
            provider = cfg["provider"]
            structure_version = cfg["structure_version"]
            if provider not in providers_found:
                providers_found.append(provider)
            if structure_version not in structure_versions:
                structure_versions.append(structure_version)

            copied = 0
            config_rows = cfg["rows"]
            for row in config_rows:
                if row.get("flags") != 1:
                    continue
                rp = safe_text(row.get("relativePath")).replace("\\", "/")
                prefix = config_root_prefix(rp, uuid)
                if not prefix:
                    continue
                suffix = rp[len(prefix):].lstrip("/")
                if not suffix:
                    continue
                dst = full_root / provider / uuid / Path(*relative_parts(suffix))
                if copy_payload(device_dir, row, dst, inventory, missing):
                    copied += 1

            log(f"[OK] {uuid}: copied {copied} full configuration file(s).")

        log("\n[5/6] Building byte-for-byte Core-7 candidate views...")
        core7_inventory: dict[str, Any] = {}

        for uuid in selected_uuids:
            cfg = configs[uuid]
            provider = cfg["provider"]
            config_rows = cfg["rows"]
            version = choose_version(config_rows, uuid)

            # Recover the exact configuration root from any matching row.
            cfg_prefix: str | None = None
            for row in config_rows:
                cfg_prefix = config_root_prefix(
                    safe_text(row.get("relativePath")),
                    uuid,
                )
                if cfg_prefix:
                    break

            result = {
                "provider": provider,
                "uuid": uuid,
                "selected_version": version,
                "files": [],
                "complete": False,
            }

            if cfg_prefix is None:
                core7_inventory[uuid] = result
                log(f"[PARTIAL] {uuid}: configuration root could not be reconstructed.")
                continue

            expected: list[tuple[str, str]] = []
            for name in CORE_ROOT_FILES:
                expected.append((name, f"{cfg_prefix}/{name}"))

            if version is not None:
                for name in CORE_CONTENT_FILES:
                    expected.append((
                        f"versions/{version}/contents/{name}",
                        f"{cfg_prefix}/versions/{version}/contents/{name}",
                    ))

            copied_core = 0
            for output_suffix, expected_rp in expected:
                row = locate_exact_row(rows, expected_rp)
                entry = {
                    "output_suffix": output_suffix,
                    "source_relative_path": expected_rp,
                    "found_in_manifest": row is not None,
                    "copied": False,
                }
                if row is not None and row.get("flags") == 1:
                    dst = core_root / provider / uuid / Path(*relative_parts(output_suffix))
                    before = len(inventory)
                    ok = copy_payload(device_dir, row, dst, inventory, missing)
                    entry["copied"] = ok
                    if ok:
                        copied_core += 1
                        inv = inventory[-1] if len(inventory) > before else None
                        if inv:
                            entry["size"] = inv["size"]
                            entry["sha256"] = inv["sha256"]
                result["files"].append(entry)

            result["complete"] = copied_core == 7
            if result["complete"]:
                complete_core7.append(uuid)
                log(f"[OK] {uuid}: Core-7 complete (7/7), selected version {version}.")
            else:
                log(f"[PARTIAL] {uuid}: Core-7 {copied_core}/7, selected version {version}.")

            core7_inventory[uuid] = result

        (capture_dir / "core7_inventory.json").write_text(
            json.dumps(core7_inventory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Provider-root support files and PosterBoard SQLite are useful for
        # cross-checking, but are never modified.
        log("\n[6/6] Copying support files and creating compact ZIP...")

        for row in rows:
            if row.get("flags") != 1:
                continue
            rp = safe_text(row.get("relativePath")).replace("\\", "/")

            provider_root = detect_provider_root(rp)
            if provider_root:
                structure_version, provider, suffix = provider_root
                if suffix == "ProviderInfo.plist":
                    dst = support_root / provider / "ProviderInfo.plist"
                    copy_payload(device_dir, row, dst, inventory, missing)

            if detect_store_db(rp):
                name = Path(rp).name
                dst = support_root / "PosterBoardDB" / name
                copy_payload(device_dir, row, dst, inventory, missing)

        (capture_dir / "file_inventory.json").write_text(
            json.dumps(inventory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (capture_dir / "missing_payloads.json").write_text(
            json.dumps(missing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if complete_core7:
            status = "CORE7-CANDIDATES-EXTRACTED"
        else:
            status = "PARTIAL-CANDIDATES-EXTRACTED"

        result_json = {
            "success": bool(complete_core7),
            "status": status,
            "read_only": True,
            "device": device_info,
            "providers_found": providers_found,
            "structure_versions": structure_versions,
            "selected_configuration_uuids": selected_uuids,
            "excluded_configuration_uuids": sorted(EXCLUDED_UUIDS),
            "complete_core7_uuids": complete_core7,
            "copied_payload_count": len(inventory),
            "missing_payload_count": len(missing),
            "note": (
                "These are files read from iOS 27 Mercury configuration trees. "
                "No descriptor package is generated and no phone data is modified."
            ),
        }
        (capture_dir / "result.json").write_text(
            json.dumps(result_json, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        zip_path = capture_dir / f"iOS27-Route2-Mercury-Configuration-V2-{stamp}.zip"
        make_zip(capture_dir, backup_root, zip_path)

        # Remove the temporary MobileBackup2 tree after the compact evidence ZIP
        # has been written. The extracted evidence files remain.
        shutil.rmtree(backup_root, ignore_errors=True)

        log("")
        log("=" * 68)
        if complete_core7:
            log("SUCCESS: at least one iOS 27 Mercury Core-7 candidate was extracted.")
        else:
            log("PARTIAL: Mercury configurations were extracted, but no Core-7 set was complete.")
        log(f"ZIP: {zip_path}")
        log("Upload this ZIP to ChatGPT for structure/field comparison.")
        log("Phone data was not modified.")
        log("=" * 68)

        return 0 if complete_core7 else 2

    except Exception as exc:
        status = "ERROR"
        error_message = f"{type(exc).__name__}: {exc}"
        log("")
        log("[ERROR] " + error_message)
        tb = traceback.format_exc()
        log_fp.write(tb + "\n")
        log_fp.flush()

        result_json = {
            "success": False,
            "status": status,
            "read_only": True,
            "device": device_info,
            "error": error_message,
            "selected_configuration_uuids": selected_uuids,
            "excluded_configuration_uuids": sorted(EXCLUDED_UUIDS),
        }
        try:
            (capture_dir / "result.json").write_text(
                json.dumps(result_json, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            zip_path = capture_dir / f"iOS27-Route2-Mercury-Configuration-V2-ERROR-{stamp}.zip"
            make_zip(capture_dir, backup_root, zip_path)
            log(f"[INFO] Diagnostic ZIP: {zip_path}")
        except Exception as zip_exc:
            log(f"[WARN] Could not create diagnostic ZIP: {zip_exc}")

        shutil.rmtree(backup_root, ignore_errors=True)
        return 1

    finally:
        if lockdown is not None:
            try:
                await lockdown.close()
            except Exception:
                pass
        log_fp.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        raise SystemExit(130)
