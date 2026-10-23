"""Offline Windows OCR bridge; no model download, no network calls.

A single PowerShell host is kept warm because starting it costs ~0.4s per
call and a click needs two or three readings. The one-shot path is kept as a
fallback so a broken host can never make the app stop working.
"""
import atexit
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from config import ROOT, REFERENCE_SIZE

# Reference-space rectangles that contain only the foreground stage line.
# A slightly wider secondary box is used when the primary one reads nothing, so
# a one-frame animation or a 1px layout shift cannot cost a whole click.
STAGE_CROPS = {
    "P1": ((640, 240, 1140, 321), (600, 225, 1200, 345)),
    "P2": ((45, 82, 330, 129), (30, 70, 420, 145)),
}
DIGITS = "０１２３４５６７８９"
FULLWIDTH = "１２３４５６７／："
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def parse_stage(text):
    compact = re.sub(r"\s+", "", text).translate(str.maketrans(FULLWIDTH, "1234567/:"))
    # Require explicit stage context. Ticket counts and calendar days cannot trigger P6.
    values = {int(n) for n in re.findall(r"(?:乐园阶段|阶段)[:：]?([1-7])/7", compact)}
    return values.pop() if len(values) == 1 else None


def parse_stage_loose(text):
    """Fallback for P2: its crop holds only the stage line, so a bare n/7 is unambiguous."""
    compact = re.sub(r"\s+", "", text).translate(str.maketrans(FULLWIDTH, "1234567/:"))
    values = {int(n) for n in re.findall(r"(?<![0-9])([1-7])/7", compact)}
    return values.pop() if len(values) == 1 else None


def parse_day(text):
    compact = re.sub(r"\s+", "", text).translate(str.maketrans(DIGITS, "0123456789"))
    values = {int(n) for n in re.findall(r"第([0-9]{1,2})天", compact)}
    return values.pop() if len(values) == 1 and 0 not in values else None


def recognize_stage(image, state, config):
    if not config["ocr_enabled"]:
        return {"stage": None, "text": "", "error": "OCR 已关闭", "source": None}
    if state not in ("P1", "P2"):
        return {"stage": None, "text": "", "error": None, "source": None}
    texts = []
    for box in STAGE_CROPS[state]:
        result = recognize_text(image.crop(box), config)
        if result["error"]:
            return {**result, "stage": None, "source": None}
        texts.append(result["text"])
        stage = parse_stage(result["text"])
        if stage is not None:
            return {**result, "stage": stage, "source": "strict"}
        if state == "P2":
            stage = parse_stage_loose(result["text"])
            if stage is not None:
                return {**result, "stage": stage, "source": "loose"}
    return {"text": texts[0], "error": None, "stage": None, "source": None}


def recognize_day(image, config):
    crop = image.resize(REFERENCE_SIZE).crop((45, 128, 330, 174))
    result = recognize_text(crop, config)
    return {**result, "day": parse_day(result["text"]) if not result["error"] else None}


class OcrHost:
    """One long-lived PowerShell process serving image path -> text requests."""

    def __init__(self, timeout):
        self.timeout = timeout
        self.proc = None
        self.queue = None
        self.lock = threading.RLock()
        self.folder = Path(tempfile.mkdtemp(prefix="gamepilot_ocr_"))
        self.counter = 0
        self.failures = 0

    def _start(self):
        self.proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ROOT / "tools" / "ocr_server.ps1")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
        self.queue = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self.queue), daemon=True).start()

    @staticmethod
    def _pump(proc, answers):
        try:
            for line in proc.stdout:
                answers.put(line)
        finally:
            answers.put(None)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def read(self, crop):
        """Return (text, error). Raises nothing; a broken host is reported."""
        with self.lock:
            if not self.alive():
                self._start()
            self.counter += 1
            path = self.folder / f"c{self.counter:06d}.png"
            try:
                crop.save(path)
            except OSError as exc:
                return None, str(exc)
            try:
                self.proc.stdin.write(str(path) + "\n")
                self.proc.stdin.flush()
                line = self.queue.get(timeout=self.timeout)
            except Exception as exc:
                self.close()
                return None, f"OCR 服务异常：{exc}"
            if line is None:
                self.close()
                return None, "OCR 服务已退出"
            try:
                data = json.loads(line)
            except ValueError as exc:
                return None, f"OCR 输出无法解析：{exc}"
            if not data.get("ok"):
                return None, data.get("error") or "OCR 失败"
            return data.get("text") or "", None

    def close(self):
        self.failures += 1
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.terminate()
            except Exception:
                pass
            self.proc = None


_HOST = None
_HOST_LOCK = threading.Lock()


def host(config):
    global _HOST
    if os.environ.get("GAMEPILOT_OCR_HOST") == "0":
        return None
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = OcrHost(config.get("ocr_timeout_seconds", 12))
        return _HOST


@atexit.register
def _close_host():
    global _HOST
    if _HOST is not None:
        _HOST.close()
        _HOST = None


def _one_shot(prepared, config):
    """Cold path: spawn a single PowerShell run and read one answer."""
    try:
        with tempfile.TemporaryDirectory(prefix="gamepilot_ocr_") as folder:
            path = Path(folder) / "crop.png"
            prepared.save(path)
            proc = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "tools" / "ocr.ps1"), "-ImagePath", str(path)
            ], capture_output=True, encoding="utf-8", errors="replace",
                timeout=config["ocr_timeout_seconds"], creationflags=CREATE_NO_WINDOW)
            if proc.returncode:
                raise RuntimeError(proc.stderr.strip()[-1000:])
            return json.loads(proc.stdout.lstrip("\ufeff"))["text"], None
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def recognize_text(crop, config, attempts=None):
    if not config["ocr_enabled"]:
        return {"text": "", "error": "OCR 已关闭"}
    attempts = max(1, attempts or config.get("ocr_attempts", 2))
    prepared = crop.resize((crop.width * 2, crop.height * 2))
    server = host(config)
    if server is not None:
        text = ""
        for _ in range(attempts):
            text, error = server.read(prepared)
            if error is None:
                if text.strip():
                    return {"text": text, "error": None}
                continue
            break
        if error is None:
            return {"text": text, "error": None}
        if server.failures >= 3:
            # Host keeps dying: stop paying its startup cost and use one-shots.
            os.environ["GAMEPILOT_OCR_HOST"] = "0"
    text, error = _one_shot(prepared, config)
    return {"text": text or "", "error": error}
