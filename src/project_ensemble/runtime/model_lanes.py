from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import nullcontext
from typing import Any, TypeVar

from project_ensemble.errors import ForcedModelReplacementRequested
from project_ensemble.runtime.progress import ProgressReporter, TaskProgressItem


WorkT = TypeVar("WorkT")
ResultT = TypeVar("ResultT")
ModelKey = tuple[str, str]


def run_bounded_model_lanes(
    lanes: Mapping[ModelKey, Sequence[WorkT]],
    worker: Callable[[WorkT], ResultT],
    concurrency_limit: Callable[[str, str], int],
    *,
    on_result: Callable[[ResultT], None] | None = None,
    progress: ProgressReporter | None = None,
    batch_title: str | None = None,
    progress_items: Sequence[TaskProgressItem] | None = None,
    live_model_key: Callable[[WorkT, ModelKey], ModelKey] | None = None,
) -> list[ResultT]:
    """Run model lanes concurrently, with a separate bound for every model.

    Input and returned-result order remain deterministic even when work within a
    lane completes out of order. The caller controls private durability through
    ``on_result`` and remains responsible for the public release barrier.
    """

    if progress is not None:
        items: list[TaskProgressItem] = list(progress_items or ())
        compatible = True
        if progress_items is None:
            for lane in lanes.values():
                for work in lane:
                    if not isinstance(work, Mapping):
                        compatible = False
                        break
                    participant_id = work.get("representative_id")
                    runtime = work.get("runtime")
                    if not isinstance(participant_id, str) or not isinstance(runtime, Mapping):
                        compatible = False
                        break
                    items.append(
                        TaskProgressItem(
                            task_id=f"{participant_id}:{len(items)}",
                            participant_id=participant_id,
                            provider_id=str(runtime.get("provider_id", "")) or None,
                            model_id=str(runtime.get("model_id", "")) or None,
                            persona=str(
                                runtime.get("persona") or runtime.get("rendering_role") or ""
                            ) or None,
                        )
                    )
                if not compatible:
                    break
        register = getattr(progress, "task_batch_started", None)
        if compatible and items and callable(register):
            if batch_title is None:
                register(items)
            else:
                register(items, title=batch_title)

    if not lanes:
        return []

    live_controls = getattr(progress, "live_batch_control_callback", None)
    if callable(live_controls):
        ordered_keys = list(lanes)
        pending = [
            (key, index, item)
            for key in ordered_keys
            for index, item in enumerate(lanes[key])
        ]
        completed: dict[tuple[ModelKey, int], ResultT] = {}
        in_flight: dict = {}
        active_by_model: dict[ModelKey, int] = {}
        first_error: Exception | None = None
        defer = getattr(progress, "defer_model_replacement", None)
        guard = defer() if callable(defer) else nullcontext()
        with guard:
            with ThreadPoolExecutor(max_workers=len(pending)) as executor:
                while pending or in_flight:
                    if getattr(progress, "safe_exit_pending", lambda: False)():
                        raise KeyboardInterrupt
                    if getattr(progress, "force_control_pending", lambda: False)():
                        raise ForcedModelReplacementRequested(
                            "Human force-stopped active model calls in this batch"
                        )
                    if getattr(progress, "control_request_pending", lambda: False)():
                        menu = getattr(progress, "interactive_menu", None)
                        with (menu() if callable(menu) else nullcontext()):
                            live_controls()
                        # A batch-menu force stop must be observed before
                        # admitting any further work to a model lane.
                        if getattr(progress, "force_control_pending", lambda: False)():
                            raise ForcedModelReplacementRequested(
                                "Human force-stopped active model calls in this batch"
                            )
                    index = 0
                    while index < len(pending):
                        original_key, position, item = pending[index]
                        key = (live_model_key(item, original_key)
                               if live_model_key is not None else original_key)
                        limit = max(1, int(concurrency_limit(*key)))
                        if active_by_model.get(key, 0) >= limit:
                            index += 1
                            continue
                        future = executor.submit(worker, item)
                        in_flight[future] = (original_key, position, key)
                        active_by_model[key] = active_by_model.get(key, 0) + 1
                        pending.pop(index)
                    if not in_flight:
                        continue
                    finished, _ = wait(tuple(in_flight), timeout=0.2,
                                       return_when=FIRST_COMPLETED)
                    for future in finished:
                        original_key, position, key = in_flight.pop(future)
                        active_by_model[key] -= 1
                        try:
                            result = future.result()
                        except Exception as exc:
                            first_error = first_error or exc
                            continue
                        if on_result is not None:
                            on_result(result)
                        completed[(original_key, position)] = result
        if first_error is not None:
            raise first_error
        return [
            completed[(key, index)]
            for key in ordered_keys
            for index in range(len(lanes[key]))
        ]

    def run_lane(key: ModelKey, lane: Sequence[WorkT]) -> list[ResultT]:
        provider_id, model_id = key
        limit = max(1, min(len(lane), concurrency_limit(provider_id, model_id)))
        if limit == 1:
            results = []
            for item in lane:
                result = worker(item)
                if on_result is not None:
                    on_result(result)
                results.append(result)
            return results
        indexed: dict[int, ResultT] = {}
        first_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=limit) as executor:
            futures = {
                executor.submit(worker, item): index
                for index, item in enumerate(lane)
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:  # keep staging other completed sealed work
                    if first_error is None:
                        first_error = exc
                    continue
                if on_result is not None:
                    on_result(result)
                indexed[futures[future]] = result
        if first_error is not None:
            raise first_error
        return [indexed[index] for index in range(len(lane))]

    ordered_keys = list(lanes)
    completed: dict[ModelKey, list[ResultT]] = {}
    with ThreadPoolExecutor(max_workers=len(ordered_keys)) as executor:
        futures = {
            executor.submit(run_lane, key, lanes[key]): key for key in ordered_keys
        }
        for future in as_completed(futures):
            completed[futures[future]] = future.result()
    return [item for key in ordered_keys for item in completed[key]]


def run_bounded_representative_lanes(
    records: Sequence[dict[str, Any]],
    worker: Callable[[dict[str, Any]], ResultT],
    concurrency_limit: Callable[[str, str], int],
    *,
    on_result: Callable[[ResultT], None] | None = None,
    progress: ProgressReporter | None = None,
    batch_title: str | None = None,
    progress_records: Sequence[dict[str, Any]] | None = None,
    completed_participant_ids: Collection[str] = (),
    runtime_key: Callable[[dict[str, Any]], ModelKey] | None = None,
    completion_requires_commit: bool = False,
    exploration_layout: bool = False,
    exploration_auto_desk_updates: bool = True,
    exploration_actor_title: str = "代表 · 规划方案",
    exploration_desk_title: str = "Research Desk · 探索检索",
    exploration_initial_desk_state: str | None = None,
    exploration_initial_desk_detail: str | None = None,
) -> list[ResultT]:
    """Run Representative work in deterministic provider/model lanes.

    Independent model lanes run concurrently.  Within one model lane, the
    configured simultaneous-request limit applies and the stable persona order
    is used as the submission order.  Callers retain control of the release
    barrier and can use ``on_result`` to durably stage each validated sealed
    submission before the whole collection completes.
    """

    persona_order = {
        "systems_integrator": 0,
        "pragmatic_minimalist": 1,
        "exploratory_synthesist": 2,
        "librarian": 3,
        "science_bookkeeper": 4,
        "citation_bookkeeper": 5,
    }
    lanes: dict[ModelKey, list[dict[str, Any]]] = {}
    for record in records:
        runtime = record["runtime"]
        key = (
            runtime_key(record)
            if runtime_key is not None
            else (str(runtime["provider_id"]), str(runtime["model_id"]))
        )
        lanes.setdefault(key, []).append(record)
    for lane in lanes.values():
        lane.sort(
            key=lambda record: (
                persona_order.get(
                    str(
                        record["runtime"].get("persona")
                        or record["runtime"].get("rendering_role")
                    ),
                    len(persona_order),
                ),
                str(record["representative_id"]),
            )
        )
    progress_items = None
    if progress_records is not None:
        completed_ids = set(completed_participant_ids)
        progress_items = []
        for index, record in enumerate(progress_records):
            runtime = record["runtime"]
            provider_id, model_id = (
                runtime_key(record)
                if runtime_key is not None
                else (str(runtime["provider_id"]), str(runtime["model_id"]))
            )
            participant_id = str(record["representative_id"])
            restored = participant_id in completed_ids
            progress_items.append(
                TaskProgressItem(
                    task_id=f"{participant_id}:{index}",
                    participant_id=participant_id,
                    provider_id=provider_id or None,
                    model_id=model_id or None,
                    persona=str(
                        runtime.get("persona") or runtime.get("rendering_role") or ""
                    ) or None,
                    initial_state="completed" if restored else "pending",
                    initial_detail="已恢复完整结果" if restored else None,
                    initial_desk_state=exploration_initial_desk_state,
                    initial_desk_detail=exploration_initial_desk_detail,
                    completion_requires_commit=completion_requires_commit,
                    exploration_layout=exploration_layout,
                    exploration_auto_desk_updates=exploration_auto_desk_updates,
                    exploration_actor_title=exploration_actor_title,
                    exploration_desk_title=exploration_desk_title,
                )
            )
    return run_bounded_model_lanes(
        lanes,
        worker,
        concurrency_limit,
        on_result=on_result,
        progress=progress,
        batch_title=batch_title,
        progress_items=progress_items,
        live_model_key=(
            (lambda record, original: (
                runtime_key(record) if runtime_key is not None else
                getattr(progress, "live_runtime_key", lambda _record: original)(record)
            ))
            if progress is not None else None
        ),
    )
