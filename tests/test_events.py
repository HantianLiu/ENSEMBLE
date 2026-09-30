import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from project_ensemble.storage.events import HashChainEventLog
from project_ensemble.errors import EventChainError


def test_hash_chain_detects_tamper(tmp_path):
    p = tmp_path / "events.jsonl"
    log = HashChainEventLog(p)
    log.append("A", {"x": 1}, actor="system")
    log.append("B", {"x": 2}, actor="chair")
    assert log.verify()
    lines = p.read_text().splitlines()
    obj = json.loads(lines[0])
    obj["payload"]["x"] = 999
    lines[0] = json.dumps(obj)
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(EventChainError):
        log.verify()


def test_parallel_model_lanes_preserve_event_hash_chain(tmp_path):
    path = tmp_path / "events.jsonl"
    log = HashChainEventLog(path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(log.append, "LANE_EVENT", {"index": index}, actor="test")
            for index in range(40)
        ]
        for future in futures:
            future.result()

    assert log.verify()
    assert len(path.read_text().splitlines()) == 40
