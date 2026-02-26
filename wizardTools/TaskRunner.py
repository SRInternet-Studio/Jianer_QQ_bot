from PySide6.QtCore import QRunnable, QObject, Signal, QTimer, QThread

class TaskSignals(QObject):
    finished = Signal(object)
    runned = Signal(str)

class TaskRunner(QRunnable):
    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.signals = TaskSignals()  # 信号对象
        self.setAutoDelete(True)  # 启用自动删除

    def run(self):
        QThread.currentThread().setObjectName(self.name or f"Worker-{id(self)}")
        self.signals.runned.emit("Running Started")
        try:
            result = self.func(*self.args, **self.kwargs)
            self.signals.finished.emit(result)
        except Exception as e:
            self.signals.finished.emit(f"Error: {e}")
        finally:
            QTimer.singleShot(0, self.signals.deleteLater)

    def set_name(self, name):
        self.name = name