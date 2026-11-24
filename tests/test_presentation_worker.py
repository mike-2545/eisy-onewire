from __future__ import annotations

import threading
import unittest

from eisy_onewire.presentation_worker import (
    PresentationCommandWorker,
    PresentationIntent,
    PresentationWorkerShutdownTimeout,
)
from eisy_onewire.temperature_presentation import TemperatureUnit


class Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str, *args: object) -> None:
        self.errors.append(message % args)


class PresentationCommandWorkerTests(unittest.TestCase):
    def test_queue_is_bounded_and_shutdown_cancels_pending_work(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        processed = []

        def process(intent) -> None:
            processed.append(intent)
            entered.set()
            release.wait(1.0)

        worker = PresentationCommandWorker(
            Logger(), process, queue_capacity=1, shutdown_timeout_seconds=1.0
        )
        worker.start()
        first = PresentationIntent.unit(TemperatureUnit.CELSIUS)
        second = PresentationIntent.precision(2)
        third = PresentationIntent.precision(3)
        self.assertTrue(worker.enqueue(first))
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(worker.enqueue(second))
        self.assertFalse(worker.enqueue(third))

        stopped = threading.Event()

        def stop() -> None:
            worker.stop()
            stopped.set()

        stopper = threading.Thread(target=stop)
        stopper.start()
        self.assertFalse(stopped.wait(0.05))
        release.set()
        stopper.join(1.0)

        self.assertTrue(stopped.is_set())
        self.assertEqual(processed, [first])
        self.assertFalse(worker.enqueue(third))

    def test_shutdown_timeout_fails_closed_until_worker_quiesces(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        logger = Logger()

        def process(intent) -> None:
            del intent
            entered.set()
            release.wait(1.0)

        worker = PresentationCommandWorker(
            logger, process, shutdown_timeout_seconds=0.02
        )
        worker.start()
        self.assertTrue(worker.enqueue(PresentationIntent.precision(3)))
        self.assertTrue(entered.wait(1.0))

        with self.assertRaises(PresentationWorkerShutdownTimeout):
            worker.stop()
        self.assertFalse(worker.enqueue(PresentationIntent.precision(1)))
        self.assertIn("interface shutdown is unsafe", logger.errors[-1])

        release.set()
        worker.stop()


if __name__ == "__main__":
    unittest.main()
