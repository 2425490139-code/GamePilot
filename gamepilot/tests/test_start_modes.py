"""Execution opt-in and read-only mode switching; no OS input."""
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui import App
from actions import ActionGate


class StartModeTests(unittest.TestCase):
    def app(self):
        app = App.__new__(App)
        app.root, app.status, app.real_enabled = MagicMock(), MagicMock(), MagicMock()
        app.closing, app.thread, app.pending_start = False, None, None
        app.gate = ActionGate()
        app.start = MagicMock()
        return app

    def test_execution_button_explicitly_opts_in(self):
        app = self.app()
        app.start_automation()
        app.real_enabled.set.assert_called_once_with(True)
        app.start.assert_called_once()

    def test_preview_button_never_opts_into_clicking(self):
        app = self.app()
        app.start_preview()
        app.real_enabled.set.assert_called_once_with(False)
        app.start.assert_called_once()

    def test_preview_is_stopped_before_execution_starts(self):
        app = self.app()
        app.thread, app.worker_real = MagicMock(), False
        app.thread.is_alive.return_value = True
        app.start_automation()
        self.assertTrue(app.gate.event.is_set())
        app.start.assert_not_called()
        app.finish_mode_switch()
        app.start.assert_not_called()
        app.thread.is_alive.return_value = False
        app.finish_mode_switch()
        app.real_enabled.set.assert_called_once_with(True)
        app.start.assert_called_once()

    def test_cancelled_pending_switch_does_not_restart(self):
        app = self.app()
        app.pending_start = None
        app.finish_mode_switch()
        app.start.assert_not_called()

    def test_running_execution_is_not_duplicated(self):
        app = self.app()
        app.thread, app.worker_real = MagicMock(), True
        app.thread.is_alive.return_value = True
        app.start_automation()
        app.start.assert_not_called()
        self.assertIsNone(app.pending_start)
