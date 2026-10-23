import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lifecycle import AlreadyRunning, SingleInstance
import main


class DuplicateStartTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32','Windows mutex')
    def test_real_mutex_released_and_duplicate_is_distinct(self):
        name = 'Local\\GamePilot_Test_' + uuid4().hex
        with SingleInstance(name):
            with self.assertRaises(AlreadyRunning):
                with SingleInstance(name):
                    self.fail('second owner must not start')
        with SingleInstance(name):
            pass

    def test_duplicate_start_is_informational_and_never_runs_worker(self):
        with patch.object(sys,'argv',['GamePilot','--no-admin']), patch('main.configure_logging'), patch('main.enable_dpi_awareness'), patch('lifecycle.SingleInstance') as lock, patch('tkinter.Tk') as root, patch('tkinter.messagebox.showinfo') as notice, patch('ui.run') as run:
            lock.return_value.__enter__.side_effect = AlreadyRunning('old window is open')
            main.main()
            run.assert_not_called()
            notice.assert_called_once()
            root.return_value.destroy.assert_called_once()
