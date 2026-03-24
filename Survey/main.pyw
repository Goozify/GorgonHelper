import sys
import asyncio
from PyQt5.QtWidgets import QApplication
import qasync
from server import SurveyServer


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("PG Survey Helper")
    app.setStyle("Fusion")

    # Prevent Qt from quitting the app when an overlay window is closed/hidden.
    # Without this, closing the RegionSelector (or any overlay) would stop
    # qasync's event loop with "Event loop stopped before Future completed".
    app.setQuitOnLastWindowClosed(False)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    server = SurveyServer()

    with loop:
        loop.run_until_complete(server.run())


if __name__ == "__main__":
    main()
