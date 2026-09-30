import threading

import pytest

from project_ensemble.runtime.draining_pool import DrainingThreadPoolExecutor


def test_ctrl_c_announces_drain_and_cancels_queued_work():
    entered = threading.Event()
    release = threading.Event()
    notice = threading.Event()
    queued_started = threading.Event()

    def active():
        entered.set()
        assert release.wait(3)

    def queued():
        queued_started.set()

    timer = threading.Timer(0.05, release.set)
    with pytest.raises(KeyboardInterrupt):
        with DrainingThreadPoolExecutor(max_workers=1, on_interrupt=notice.set) as pool:
            active_future = pool.submit(active)
            assert entered.wait(1)
            queued_future = pool.submit(queued)
            timer.start()
            raise KeyboardInterrupt
    timer.join(1)
    assert notice.is_set()
    assert active_future.done()
    assert queued_future.cancelled()
    assert not queued_started.is_set()
