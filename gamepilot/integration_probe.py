"""Exercise the real GUI/worker/overlay pipeline with fixtures and NO OS input."""
import json
import time
import traceback
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
from PIL import Image
from config import ROOT, DATA, load_config
from ui import App
from logger import Session


def run(output, fixtures):
    from main import configure_logging, guard_streams
    guard_streams()
    configure_logging()
    report = {'scope':'OFFLINE_FULL_GUI_WORKER_PIPELINE', 'os_input_events':0,
              'live_game_test':'NOT_RUN', 'verified_steps':[], 'click_records':[], 'passed':False}
    root = tk.Tk()
    app = App(root)
    app.config = {**load_config(), 'region':[0, 0, 1705, 1066]}
    app.session = Session(Path(output).parent/'probe_session.json')
    images = {}
    sequence = ('P5','P3','P4')
    for state in sequence:
        images[state] = Image.open(Path(fixtures)/app.matcher.manifest[state]['source']).convert('RGB')
    index = [0]
    def snapshot(region):
        return images[sequence[index[0] % 3]].copy(), (0, 0)
    class NoInputBackend:
        def __init__(self, target_feedback=None):
            self.feedback = target_feedback
        def wait_for_game(self, *a, **kw):
            return 7
        def prepare(self, *a):
            return 7
        def preview_target(self, result, gate):
            self.feedback(result, gate)
        def click(self, point, region, window):
            report['click_records'].append({'state':sequence[index[0] % 3], 'point':point})
            index[0] += 1
    class NoKeyboardWatch:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    original_post = app.post_flow
    def post(event):
        if event['kind'] == 'flow_action':
            report['verified_steps'].append(event['result']['status'])
            if len(report['verified_steps']) >= 9:
                app.gate.cancel()
        original_post(event)
    app.post_flow = post
    app.privilege_gate = lambda: True
    app.warm_ocr_async = lambda: None
    started = time.monotonic()
    def finish():
        report['elapsed_seconds'] = round(time.monotonic()-started,3)
        report['passed'] = (len(report['verified_steps']) == 9
                            and all(s == 'VERIFIED' for s in report['verified_steps'])
                            and len(report['click_records']) == 9 and not report.get('error'))
        app.close()
    def monitor():
        if app.thread and not app.thread.is_alive():
            finish()
        elif time.monotonic()-started > 25:
            report['error'] = 'full worker pipeline timeout'
            app.gate.cancel()
            root.after(100, monitor)
        else:
            root.after(30, monitor)
    def ui_error(kind, error, trace):
        report['error'] = ''.join(traceback.format_exception(kind,error,trace))
        app.gate.cancel()
    root.report_callback_exception = ui_error
    with patch('ui.capture',snapshot), patch('windows_message.WindowMessageMouse',NoInputBackend), patch('windows_input.EmergencyWatch',NoKeyboardWatch):
        root.after(150, app.buttons[0].invoke)
        root.after(250, monitor)
        root.mainloop()
    Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['passed'] else 1
