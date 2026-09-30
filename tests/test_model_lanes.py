import threading
import time
from contextlib import contextmanager

import pytest

from project_ensemble.runtime.model_lanes import (
    run_bounded_model_lanes,
    run_bounded_representative_lanes,
)


def test_model_lane_uses_per_model_concurrency_and_preserves_result_order():
    lanes = {("provider", "model"): list(range(6))}
    guard = threading.Lock()
    active = 0
    peak = 0

    def worker(value):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return value * 10

    results = run_bounded_model_lanes(
        lanes,
        worker,
        lambda _provider_id, _model_id: 2,
    )

    assert peak == 2
    assert results == [0, 10, 20, 30, 40, 50]


def test_unknown_model_concurrency_can_remain_serial():
    lanes = {("provider", "model"): [1, 2, 3]}
    guard = threading.Lock()
    active = 0
    peak = 0

    def worker(value):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with guard:
            active -= 1
        return value

    assert run_bounded_model_lanes(lanes, worker, lambda *_args: 1) == [1, 2, 3]
    assert peak == 1


def test_live_representative_lane_reassigns_unstarted_call_after_ctrl_r():
    first_started = threading.Event()
    second_started = threading.Event()
    switched = threading.Event()
    runtime = {"R-1": ("provider", "old"), "R-2": ("provider", "old")}

    class Progress:
        def task_batch_started(self, _items, *, title=None):
            pass

        def control_request_pending(self):
            return first_started.is_set() and not switched.is_set()

        @contextmanager
        def interactive_menu(self):
            yield

        @contextmanager
        def defer_model_replacement(self):
            yield

        def live_batch_control_callback(self):
            runtime["R-2"] = ("provider", "new")
            switched.set()

        def live_runtime_key(self, record):
            return runtime[record["representative_id"]]

    records = [
        {"representative_id": rid,
         "runtime": {"provider_id": "provider", "model_id": "old", "persona": "librarian"}}
        for rid in ("R-1", "R-2")
    ]

    def worker(record):
        rid = record["representative_id"]
        if rid == "R-1":
            first_started.set()
            assert second_started.wait(timeout=3)
        else:
            assert switched.is_set()
            second_started.set()
        return rid

    result = run_bounded_representative_lanes(
        records, worker, lambda *_args: 1, progress=Progress(),
    )
    assert result == ["R-1", "R-2"]
    assert second_started.is_set()


def test_representative_lanes_parallelize_models_but_serialize_same_model():
    records = [
        {
            "representative_id": "R-A-BUILDER",
            "runtime": {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "persona": "systems_integrator",
            },
        },
        {
            "representative_id": "R-A-MONITOR",
            "runtime": {
                "provider_id": "provider-a",
                "model_id": "model-a",
                "persona": "pragmatic_minimalist",
            },
        },
        {
            "representative_id": "R-B-BUILDER",
            "runtime": {
                "provider_id": "provider-b",
                "model_id": "model-b",
                "persona": "systems_integrator",
            },
        },
    ]
    guard = threading.Lock()
    active_by_model = {"model-a": 0, "model-b": 0}
    peak_by_model = {"model-a": 0, "model-b": 0}
    total_active = 0
    total_peak = 0

    def worker(record):
        nonlocal total_active, total_peak
        model_id = record["runtime"]["model_id"]
        with guard:
            active_by_model[model_id] += 1
            peak_by_model[model_id] = max(
                peak_by_model[model_id], active_by_model[model_id]
            )
            total_active += 1
            total_peak = max(total_peak, total_active)
        time.sleep(0.03)
        with guard:
            active_by_model[model_id] -= 1
            total_active -= 1
        return record["representative_id"]

    results = run_bounded_representative_lanes(
        records,
        worker,
        lambda _provider_id, _model_id: 1,
    )

    assert peak_by_model == {"model-a": 1, "model-b": 1}
    assert total_peak == 2
    assert results == ["R-A-BUILDER", "R-A-MONITOR", "R-B-BUILDER"]


def test_three_four_person_model_lanes_can_start_twelve_sealed_calls_together():
    personas = (
        "systems_integrator",
        "pragmatic_minimalist",
        "exploratory_synthesist",
        "librarian",
    )
    records = [
        {
            "representative_id": f"R-{model}-{index}",
            "runtime": {
                "provider_id": "provider",
                "model_id": model,
                "persona": persona,
            },
        }
        for model in ("a", "b", "c")
        for index, persona in enumerate(personas)
    ]
    all_started = threading.Barrier(12, timeout=5)

    def worker(record):
        all_started.wait()
        return record["representative_id"]

    assert len(run_bounded_representative_lanes(records, worker, lambda *_: 4)) == 12


def test_model_lane_stages_completed_result_before_later_failure():
    staged = []

    def worker(value):
        if value == 2:
            raise RuntimeError("provider failed")
        return value

    with pytest.raises(RuntimeError, match="provider failed"):
        run_bounded_model_lanes(
            {("provider", "model"): [1, 2]},
            worker,
            lambda *_args: 1,
            on_result=staged.append,
        )

    assert staged == [1]


def test_concurrent_model_lane_stages_other_successes_when_one_call_fails():
    staged = []

    def worker(value):
        if value == 2:
            raise RuntimeError("provider failed")
        time.sleep(0.01)
        return value

    with pytest.raises(RuntimeError, match="provider failed"):
        run_bounded_model_lanes(
            {("provider", "model"): [1, 2, 3]},
            worker,
            lambda *_args: 3,
            on_result=staged.append,
        )

    assert sorted(staged) == [1, 3]


def test_representative_lanes_register_all_pending_tasks_before_work_starts():
    class Progress:
        def __init__(self):
            self.batches = []
            self.titles = []

        def task_batch_started(self, tasks, *, title=None):
            self.batches.append(list(tasks))
            self.titles.append(title)

    progress = Progress()
    records = [
        {
            "representative_id": "R-A",
            "runtime": {
                "provider_id": "gemini",
                "model_id": "flash",
                "persona": "systems_integrator",
            },
        },
        {
            "representative_id": "R-B",
            "runtime": {
                "provider_id": "glm",
                "model_id": "flash",
                "persona": "librarian",
            },
        },
    ]

    assert run_bounded_representative_lanes(
        records,
        lambda record: record["representative_id"],
        lambda *_args: 1,
        progress=progress,
        batch_title="RM-01 · round1 · 全体审阅",
    ) == ["R-A", "R-B"]
    assert [[item.participant_id for item in batch] for batch in progress.batches] == [
        ["R-A", "R-B"]
    ]
    assert progress.titles == ["RM-01 · round1 · 全体审阅"]


def test_representative_lanes_register_restored_and_pending_rows():
    class Progress:
        def __init__(self):
            self.batch = []

        def task_batch_started(self, tasks, *, title=None):
            self.batch = list(tasks)

    records = [
        {
            "representative_id": rid,
            "runtime": {
                "provider_id": "provider",
                "model_id": "model",
                "persona": "librarian",
            },
        }
        for rid in ("R-A", "R-B")
    ]
    progress = Progress()

    assert run_bounded_representative_lanes(
        records[1:],
        lambda record: record["representative_id"],
        lambda *_args: 1,
        progress=progress,
        progress_records=records,
        completed_participant_ids={"R-A"},
    ) == ["R-B"]
    assert [item.participant_id for item in progress.batch] == ["R-A", "R-B"]
    assert [item.initial_state for item in progress.batch] == ["completed", "pending"]


def test_representative_lanes_group_replacements_by_effective_runtime():
    records = [
        {
            "representative_id": rid,
            "runtime": {
                "provider_id": "frozen",
                "model_id": frozen_model,
                "persona": "librarian",
            },
        }
        for rid, frozen_model in (("R-A", "old-a"), ("R-B", "old-b"))
    ]
    guard = threading.Lock()
    active = 0
    peak = 0

    def worker(record):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with guard:
            active -= 1
        return record["representative_id"]

    results = run_bounded_representative_lanes(
        records,
        worker,
        lambda provider, model: 1 if (provider, model) == ("new", "shared") else 99,
        runtime_key=lambda _record: ("new", "shared"),
    )
    assert results == ["R-A", "R-B"]
    assert peak == 1
