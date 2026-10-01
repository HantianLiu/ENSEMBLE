"""Conservative, copy-on-write import of a v0.6 meeting into the current runtime.

Frozen meeting artifacts and the historical hash chain are copied byte for byte.
Only new metadata in the destination identifies the runtime and configuration
that will govern *future* steps. No provider calls are made by this module.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shutil
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from project_ensemble import GOVERNANCE_VERSION, __version__
from project_ensemble.config import EnsembleConfig, load_config
from project_ensemble.storage.meeting import (
    MeetingRepository,
    PrivateMeetingManifest,
    PublicMeetingManifest,
    PublicTask,
    SessionConfigurationReference,
    directory_digest,
)
from project_ensemble.storage.meeting_index import meeting_is_complete
from project_ensemble.runtime.model_replacements import replacement_model_pairs


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _model_config_source(config_path: Path) -> Path | None:
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    configured = (data.get("project") or {}).get("model_config_file")
    if not configured:
        return None
    source = Path(str(configured)).expanduser()
    return (source if source.is_absolute() else config_path.parent / source).resolve()


def _inventory(root: Path) -> dict[str, str]:
    """Hash every source entry, rejecting links that escape the meeting."""

    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            target_text = os.readlink(path)
            if Path(target_text).is_absolute():
                raise ValueError(f"meeting contains an absolute symlink: {relative}")
            target = path.resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError(f"meeting contains an unsafe symlink: {relative}")
            result[relative] = "link:" + target_text
        elif path.is_file():
            result[relative] = "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[relative] = "dir"
        else:
            raise ValueError(f"meeting contains an unsupported filesystem entry: {relative}")
    return result


def _inventory_digest(inventory: dict[str, str]) -> str:
    payload = json.dumps(inventory, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _legacy_run_lock(source: Path):
    """Respect the v0.6 run lock without creating or changing a source file."""

    path = source / "governance_private/meeting.run.lock"
    if not path.exists():
        yield
        return
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("v0.6 会议仍在运行；请安全中断后再迁移") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _required_providers(manifest: dict, repo: MeetingRepository) -> set[str]:
    pairs = [*manifest.get("selected_models", [])]
    for key in ("chair_model", "research_model"):
        if manifest.get(key):
            pairs.append(manifest[key])
    for key in ("rendering_science_models", "rendering_citation_models"):
        pairs.extend(manifest.get(key) or [])
    pairs.extend(replacement_model_pairs(repo))
    return {str(pair[0]) for pair in pairs}


def inspect_v06_meeting(
    source: str | Path, *, config: EnsembleConfig
) -> dict:
    """Perform a read-only eligibility and integrity check."""

    root = Path(source).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"v0.6 会议目录不存在：{root}")
    public_path = root / "public/meeting_manifest.json"
    private_path = root / "identity_private/meeting_manifest.json"
    task_path = root / "public/task.json"
    for path in (public_path, private_path, task_path):
        if not path.is_file():
            raise ValueError(f"v0.6 会议缺少必要文件：{path.relative_to(root)}")
    legacy_config_ref = root / "human_private/session_configuration.json"
    if legacy_config_ref.is_file():
        old_config_path = _read_json(legacy_config_ref).get("config_path")
        if old_config_path and Path(str(old_config_path)).expanduser().resolve() == config.source_path:
            raise ValueError(
                f"迁移必须使用独立的 v{__version__} 配置文件，不能复用旧会议记录的 v0.6 配置路径"
            )
    public = _read_json(public_path)
    private = _read_json(private_path)
    task = _read_json(task_path)
    legacy_version = private.get("software_version")
    if legacy_version is not None and not str(legacy_version).startswith("0.6"):
        raise ValueError(
            f"只能迁移 v0.6 会议；源会议声明的软件版本为 {legacy_version!r}"
        )
    PublicMeetingManifest.model_validate(public)
    PrivateMeetingManifest.model_validate(private)
    PublicTask.model_validate(task)
    if not (
        public.get("meeting_type")
        == private.get("meeting_type")
        == task.get("meeting_type")
    ) or not (
        public.get("deliverable_type", "normative_instrument")
        == private.get("deliverable_type", "normative_instrument")
        == task.get("deliverable_type", "normative_instrument")
    ):
        raise ValueError("公开 manifest、私有 manifest 与任务的会议类型不一致")
    if public.get("meeting_type") == "audit":
        raise ValueError(
            "当前版本尚无审计会议执行器，不能将 v0.6 审计会议升级为可续跑会议"
        )
    meeting_id = str(public["meeting_id"])
    if not meeting_id.startswith("M-") or root.name != meeting_id:
        raise ValueError("源目录名必须与公开会议 ID 一致")
    if private.get("meeting_id") != meeting_id or task.get("meeting_id") != meeting_id:
        raise ValueError("公开 manifest、私有 manifest 与任务的会议 ID 不一致")
    event_path = root / "governance_private/events.jsonl"
    if not event_path.is_file():
        raise ValueError("源会议缺少事件日志，无法确认已冻结进度")
    repo = MeetingRepository(root)
    repo.events.verify()
    missing_providers = sorted(
        provider
        for provider in _required_providers(private, repo)
        if provider not in config.providers or not config.providers[provider].enabled
    )
    if missing_providers:
        raise ValueError(
            f"v{__version__} 模型配置缺少会议需要的已启用供应商："
            + ", ".join(missing_providers)
        )
    governance = Path(config.project.governance_docs)
    if not governance.is_dir():
        raise ValueError(f"v{__version__} 制度文件目录不存在：{governance}")
    # Check links before copying. This also rejects special files and records
    # the exact source bytes later checked against the copied snapshot.
    inventory = _inventory(root)
    return {
        "meeting_id": meeting_id,
        "meeting_type": public["meeting_type"],
        "deliverable_type": public.get("deliverable_type", "normative_instrument"),
        "legacy_software_version": legacy_version or "0.6-unversioned",
        "legacy_governance_digest": private["governance_digest"],
        "target_governance_digest": directory_digest(governance),
        "source_file_count": sum(value.startswith(("file:", "link:")) for value in inventory.values()),
        "source_tree_sha256": _inventory_digest(inventory),
        "completed": meeting_is_complete(root),
    }


def migrate_v06_meeting(
    source: str | Path,
    destination: str | Path,
    *,
    config_path: str | Path,
    dry_run: bool = False,
) -> dict:
    """Copy a v0.6 meeting and pin current software/governance metadata."""

    root = Path(source).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    config_source = Path(config_path).expanduser().resolve()
    config = load_config(config_source)
    if target == root or target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError("目标目录必须与源会议分离，且不能彼此包含")
    if target.exists():
        raise FileExistsError(f"目标目录已存在：{target}")
    if not target.parent.is_dir():
        raise ValueError(f"目标目录的父目录不存在：{target.parent}")
    with _legacy_run_lock(root):
        report = inspect_v06_meeting(root, config=config)
        report["source_path"] = str(root)
        report["destination_path"] = str(target)
        if dry_run:
            return report
        stage = target.parent / f".{target.name}.migrating-{secrets.token_hex(4)}"
        try:
            shutil.copytree(root, stage, symlinks=True)
            if _inventory(stage) != _inventory(root):
                raise ValueError("复制时源会议发生变化，迁移已取消；请先停止旧版进程")
            repo = MeetingRepository(stage)
            repo.events.verify()
            private_path = stage / "identity_private/meeting_manifest.json"
            private = _read_json(private_path)
            legacy_manifest_path = (
                stage / "human_private/v06_migration/legacy_meeting_manifest.json"
            )
            legacy_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_manifest_path.write_bytes(private_path.read_bytes())
            private["software_version"] = __version__
            private["governance_version"] = GOVERNANCE_VERSION
            private["governance_digest"] = report["target_governance_digest"]
            private_path.write_text(json.dumps(private, indent=2, ensure_ascii=False), encoding="utf-8")

            task = _read_json(stage / "public/task.json")
            prompt_path = stage / "original_prompt.txt"
            if not prompt_path.exists():
                prompt_path.write_text(str(task["description"]), encoding="utf-8")
            lineage_path = stage / "meeting_lineage.json"
            if not lineage_path.exists():
                public = _read_json(stage / "public/meeting_manifest.json")
                lineage = {
                    "schema_version": 1,
                    "meetings": [{
                        "meeting_id": report["meeting_id"],
                        "title": public.get("title", task.get("title", "未命名会议")),
                        "meeting_type": public["meeting_type"],
                        "parent_meeting_id": public.get("parent_meeting_id"),
                        "inherited_material_categories": (
                            ["EVIDENCE", "FINAL_DOCUMENT"]
                            if private.get("inheritance_mode") == "both"
                            else [str(private["inheritance_mode"]).upper()]
                            if private.get("inheritance_mode") else []
                        ),
                    }],
                }
                lineage_path.write_text(
                    json.dumps(lineage, indent=2, ensure_ascii=False), encoding="utf-8"
                )

            snapshot_root = stage / "human_private/configuration_snapshot"
            snapshot_root.mkdir(parents=True, exist_ok=True)
            config_bytes = config_source.read_bytes()
            (snapshot_root / "ensemble.toml").write_bytes(config_bytes)
            model_source = _model_config_source(config_source)
            model_bytes = model_source.read_bytes() if model_source else None
            if model_bytes is not None:
                (snapshot_root / "model_config.toml").write_bytes(model_bytes)
            ref = SessionConfigurationReference(
                meeting_id=report["meeting_id"],
                config_path=str(config_source),
                config_sha256=hashlib.sha256(config_bytes).hexdigest(),
                snapshot_path="human_private/configuration_snapshot/ensemble.toml",
                model_config_path=str(model_source) if model_source else None,
                model_config_sha256=(hashlib.sha256(model_bytes).hexdigest() if model_bytes else None),
                model_config_snapshot_path=(
                    "human_private/configuration_snapshot/model_config.toml"
                    if model_bytes is not None else None
                ),
            )
            ref_path = stage / "human_private/session_configuration.json"
            if ref_path.exists():
                old_ref = _read_json(ref_path)
                legacy_ref_path = stage / "human_private/v06_migration/legacy_session_configuration.json"
                legacy_ref_path.parent.mkdir(parents=True, exist_ok=True)
                legacy_ref_path.write_text(
                    json.dumps(old_ref, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            ref_path.write_text(ref.model_dump_json(indent=2), encoding="utf-8")

            report.update({
                "migrated_at": datetime.now(timezone.utc).isoformat(),
                "target_software_version": __version__,
                "config_sha256": ref.config_sha256,
                "model_config_sha256": ref.model_config_sha256,
                "historical_artifacts_rewritten": False,
            })
            migration_path = stage / "human_private/v06_migration/report.json"
            migration_path.parent.mkdir(parents=True, exist_ok=True)
            migration_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            repo.events.append(
                "MEETING_MIGRATED_FROM_V06",
                {
                    "meeting_id": report["meeting_id"],
                    "source_tree_sha256": report["source_tree_sha256"],
                    "legacy_governance_digest": report["legacy_governance_digest"],
                    "target_governance_digest": report["target_governance_digest"],
                    "migration_report_path": "human_private/v06_migration/report.json",
                },
                actor="orchestrator",
            )
            repo.events.verify()
            if target.exists():
                raise FileExistsError(f"目标目录已存在：{target}")
            stage.rename(target)
            return report
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise
