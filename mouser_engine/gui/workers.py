"""Ejecución de consultas en segundo plano para que la ventana no se congele."""

from __future__ import annotations

import threading
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class WorkerSignals(QObject):
    progress = Signal(int, int, str)
    result = Signal(object)
    error = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    """Ejecuta `fn` en el pool de hilos.

    Si `fn` acepta los argumentos `progress` y/o `cancel`, se le entregan una función de
    progreso y un threading.Event para cancelar.
    """

    def __init__(self, fn: Callable, *args, with_progress: bool = False, with_cancel: bool = False,
                 **kwargs):
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self.cancel_event = threading.Event()
        if with_progress:
            self.kwargs["progress"] = self._emit_progress
        if with_cancel:
            self.kwargs["cancel"] = self.cancel_event

    def _emit_progress(self, done: int, total: int, message: str) -> None:
        self.signals.progress.emit(done, total, message)

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:  # noqa: D401 - método de QRunnable
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # noqa: BLE001 - se informa en la interfaz
            exc.traceback_text = traceback.format_exc()  # type: ignore[attr-defined]
            self.signals.error.emit(exc)
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class WorkerPool:
    """Mantiene referencias a los workers activos hasta que terminan."""

    def __init__(self, pool: QThreadPool | None = None):
        self.pool = pool or QThreadPool.globalInstance()
        self._active: set[Worker] = set()

    def start(self, worker: Worker) -> Worker:
        self._active.add(worker)
        worker.signals.finished.connect(lambda w=worker: self._active.discard(w))
        self.pool.start(worker)
        return worker

    def cancel_all(self) -> None:
        for worker in list(self._active):
            worker.cancel()

    def wait(self, msecs: int = 30000) -> bool:
        return self.pool.waitForDone(msecs)

    @property
    def busy(self) -> bool:
        return bool(self._active)
