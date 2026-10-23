"""Bounded logs and per-operation evidence, stored locally."""
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
from uuid import uuid4


def configure(root):
    root = Path(root)
    root.mkdir(exist_ok=True, parents=True)
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(root / "gamepilot.log", maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class Session:
    """Persist P6 latch across pause, stop and app restart; explicit stage 1 rearms."""
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.data = {"p6_armed": True, "p6_triggered_at": None, "p6_result": None,
                     "last_action": "尚未执行", "restart_count": 0}
        if self.path and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or type(loaded.get("p6_armed")) is not bool or type(loaded.get("restart_count")) is not int:
                    raise ValueError("Invalid session schema")
                self.data.update(loaded)
            except (OSError, ValueError, TypeError):
                self.data["p6_armed"] = False
                self.data["p6_result"] = "会话记录损坏，待确认阶段 1 后重新允许 P6"

    def update(self, **changes):
        with self.lock:
            self.data.update(changes)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.path)

    def trigger(self):
        with self.lock:
            if not self.data["p6_armed"]:
                return False
            self.update(p6_armed=False, p6_triggered_at=datetime.now().astimezone().isoformat(),
                        p6_result="执行中")
            return True

    def rearm(self):
        if not self.data["p6_armed"]:
            self.update(p6_armed=True)

    def snapshot(self):
        with self.lock:
            return self.data.copy()


class Evidence:
    def __init__(self, parent, prefix):
        self.folder = Path(parent) / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}"
        self.folder.mkdir(parents=True, exist_ok=False)
        self.result = {"started_at": datetime.now().astimezone().isoformat(), "events": []}

    def event(self, name, **data):
        event = {"time": datetime.now().astimezone().isoformat(), "event": name, **data}
        self.result["events"].append(event)
        logging.info("flow %s", json.dumps(event, ensure_ascii=False))

    def image(self, name, image):
        if image is not None:
            # Evidence only needs to be readable, not smallest: level 1 encodes
            # a 2560x1600 frame in ~0.09s instead of ~0.21s on the click path.
            image.save(self.folder / f"{name}.png", compress_level=1)

    def finish(self, **result):
        self.result.update(result, finished_at=datetime.now().astimezone().isoformat(), report_dir=str(self.folder))
        (self.folder / "result.json").write_text(json.dumps(self.result, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.result
