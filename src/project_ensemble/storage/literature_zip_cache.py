"""Retire reproducible archive ZIPs without rewriting frozen archive manifests."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ZIP_PATH = Path("public/research/literature_bundle.zip")
BUNDLE_PATH = Path("public/research/literature_bundle")
RETIREMENT_PATH = Path("public/archive_maintenance/literature_zip_retirement.json")


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe_file(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("文献缓存路径越过会议目录")
    target = root / relative
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"文献缓存路径不能是外部路径或符号链接：{relative}")
    return target


def preserve_zip_members(source_root: Path, target_root: Path) -> list[dict]:
    """Verify every ZIP member and materialize any ZIP-only/versioned originals."""
    source_zip = _safe_file(source_root, ZIP_PATH)
    if not source_zip.is_file():
        return []
    records = []
    with zipfile.ZipFile(source_zip) as bundle:
        members = [item for item in bundle.infolist() if not item.is_dir()]
        for item in members:
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in item.filename:
                raise ValueError(f"文献 ZIP 包含不安全路径：{item.filename}")
        for item in members:
            with bundle.open(item) as stream:
                expected = hashlib.file_digest(stream, "sha256").hexdigest()
            relative = BUNDLE_PATH / item.filename
            target = _safe_file(target_root, relative)
            if target.is_file() and digest_file(target) != expected:
                # Keep both versions of a derived catalog or changed original.
                relative = BUNDLE_PATH / "recovered_zip" / expected / item.filename
                target = _safe_file(target_root, relative)
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                        temporary = Path(output.name)
                        with bundle.open(item) as stream:
                            shutil.copyfileobj(stream, output, length=1024 * 1024)
                    if digest_file(temporary) != expected:
                        raise ValueError(f"文献 ZIP 解包校验失败：{item.filename}")
                    # Never replace an existing original, including concurrent writes.
                    os.link(temporary, target)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            if digest_file(target) != expected:
                raise ValueError(f"文献 ZIP 内容未被原文件完整保留：{item.filename}")
            records.append({"zip_member": item.filename, "path": relative.as_posix(),
                            "sha256": expected, "size_bytes": item.file_size})
    return records


def effective_archive_hashes(root: Path, record: dict) -> dict:
    """Honor a narrowly scoped, verified cache-retirement supplement."""
    hashes = dict(record.get("retained_file_sha256", {}))
    if str(ZIP_PATH) not in hashes or (root / ZIP_PATH).exists():
        return hashes
    proof_path = _safe_file(root, RETIREMENT_PATH)
    if not proof_path.is_file():
        return hashes  # Normal integrity checking will report the missing ZIP.
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    if (proof.get("status") != "ZIP_RETIRED_CONTENT_PRESERVED"
            or proof.get("archive_manifest_sha256") != digest_file(root / "public/archive_manifest.json")
            or proof.get("zip_sha256") != hashes[str(ZIP_PATH)]
            or proof.get("meeting_id") != record.get("meeting_id")):
        raise ValueError("归档 ZIP 维护记录与原归档清单不匹配")
    members = proof.get("members")
    if not isinstance(members, list):
        raise ValueError("归档 ZIP 维护记录缺少内容校验清单")
    for member in members:
        relative = Path(member["path"])
        if not relative.is_relative_to(BUNDLE_PATH):
            raise ValueError("归档 ZIP 维护记录越过文献原文件目录")
        path = _safe_file(root, relative)
        if not path.is_file() or digest_file(path) != member["sha256"]:
            raise ValueError(f"归档 ZIP 保留原文件校验失败：{relative}")
        if relative.as_posix() in hashes and hashes[relative.as_posix()] != member["sha256"]:
            raise ValueError("ZIP 维护记录不得改写原归档的文件校验哈希")
        hashes[relative.as_posix()] = member["sha256"]
    hashes.pop(str(ZIP_PATH))
    return hashes


def retire_archived_literature_zip(root: str | Path) -> dict:
    """Remove only one validated derived ZIP from an explicitly named archive."""
    from project_ensemble.storage.meeting import MeetingRepository, _verify_archived_parent_integrity

    candidate = Path(root).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("请指定一个普通归档会议目录")
    root = candidate.resolve()
    manifest_path = _safe_file(root, Path("public/archive_manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    repo = MeetingRepository(root)
    with repo.exclusive_run_lock():
        _verify_archived_parent_integrity(root, repo.meeting_id)
        zip_path = _safe_file(root, ZIP_PATH)
        if not zip_path.is_file():
            return {"meeting_id": repo.meeting_id, "status": "ALREADY_ABSENT", "removed_bytes": 0}
        zip_sha = digest_file(zip_path)
        size = zip_path.stat().st_size
        members = preserve_zip_members(root, root)
        proof = {"meeting_id": repo.meeting_id, "status": "ZIP_RETIRED_CONTENT_PRESERVED",
                 "created_at": datetime.now(timezone.utc).isoformat(),
                 "archive_manifest_sha256": digest_file(manifest_path),
                 "zip_sha256": zip_sha, "zip_path": str(ZIP_PATH),
                 "removed_bytes": size, "members": members,
                 "recovery": "Regenerate exports from preserved originals with ensemble gather."}
        expected = manifest.get("retained_file_sha256", {}).get(str(ZIP_PATH))
        if expected != zip_sha:
            raise ValueError("文献 ZIP 不在原归档校验清单中或哈希不一致；未删除")
        proof_path = _safe_file(root, RETIREMENT_PATH)
        proof_path.parent.mkdir(parents=True, exist_ok=True)
        if proof_path.exists():
            previous = json.loads(proof_path.read_text(encoding="utf-8"))
            if any(previous.get(key) != proof[key] for key in (
                "meeting_id", "archive_manifest_sha256", "zip_sha256", "members"
            )):
                raise ValueError("已有不同的 ZIP 维护记录；未覆盖也未删除")
        else:
            # Publish the complete supplemental record atomically and without replacement.
            with tempfile.NamedTemporaryFile(dir=proof_path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(json.dumps(proof, ensure_ascii=False, indent=2).encode("utf-8"))
            try:
                os.link(temporary, proof_path)
            finally:
                temporary.unlink(missing_ok=True)
        if digest_file(zip_path) != zip_sha:
            raise ValueError("文献 ZIP 在清理期间发生变化；未删除")
        zip_path.unlink()
        alias = root / "LITERATURE_BUNDLE.zip"
        if alias.is_symlink() and alias.resolve() == zip_path:
            alias.unlink()
        _verify_archived_parent_integrity(root, repo.meeting_id)
        return {"meeting_id": repo.meeting_id, "status": "RETIRED", "removed_bytes": size,
                "member_count": len(members), "maintenance_record": str(proof_path)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Retire ZIP caches in explicitly named archived meetings")
    parser.add_argument("meetings", nargs="+")
    args = parser.parse_args()
    for meeting in args.meetings:
        print(json.dumps(retire_archived_literature_zip(meeting), ensure_ascii=False), flush=True)
