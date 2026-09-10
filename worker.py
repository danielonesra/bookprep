from PySide6.QtCore import QObject, QRunnable, Signal


class WorkerSignals(QObject):
    finished = Signal(object)
    error = Signal(str)
    progress = Signal(int, int)


class Worker(QRunnable):
    """Runs `fn(*args, **kwargs)` on a background thread from the global
    QThreadPool. If `fn` accepts a `progress_cb` keyword, one is supplied
    automatically that forwards to `signals.progress`."""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            if "progress_cb" in self.fn.__code__.co_varnames:
                self.kwargs.setdefault("progress_cb", self._progress)
            result = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(result)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.signals.error.emit(str(e))

    def _progress(self, i, n):
        self.signals.progress.emit(i, n)
