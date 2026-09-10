import sys
import faulthandler
import atexit

# Enable Python's built-in crash handler as early as possible. This can't
# prevent a native crash, but if one still happens somewhere it gives a
# best-effort C-level stack trace instead of dying completely silently.
faulthandler.enable()

from PySide6.QtWidgets import QApplication

from main_window import MainWindow
import pdf_render_worker


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("BookPrep")
    app.setStyle("Fusion")

    # Start the isolated PDF-rendering subprocess now, from the main
    # thread, before any QThreadPool workers exist — forking a new
    # process while other threads are already running is itself a known
    # source of subtle instability, so we do it while startup is still
    # simple.
    pdf_render_worker.init_executor()
    atexit.register(pdf_render_worker.shutdown_executor)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
