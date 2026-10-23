"""Preview/single-step GUI or read-only offline screenshot report."""
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from PIL import Image
from config import ROOT, DATA, load_config
from matcher import Matcher, annotate
from capture import enable_dpi_awareness


def guard_streams():
    """A windowed .exe has no stdout; print() must not crash it."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def configure_logging():
    from logger import configure
    configure(DATA / "reports")


def offline(paths, output, with_ocr):
    output.mkdir(parents=True, exist_ok=True)
    matcher = Matcher(load_config())
    results = []
    for path in paths:
        with Image.open(path) as im:
            image = im.convert("RGB")
        result = matcher.detect(image, with_ocr=with_ocr)
        result["file"] = str(path.resolve())
        annotate(image, result).save(output / f"{path.stem}_识别.png")
        results.append(result)
        logging.info("offline %s", json.dumps(result, ensure_ascii=False))
        print(f"{path.name}: {result['state']}, 阶段={result['stage']}, OCR错误={result['ocr']['error']}")
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def main():
    guard_streams()
    enable_dpi_awareness()
    configure_logging()
    parser = argparse.ArgumentParser(description="GamePilot（默认仅识别，实际操作须在界面开启）")
    parser.add_argument("--offline", type=Path, help="离线识别 PNG 文件或目录")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-ocr", action="store_true", help="仅做模板识别，报告中阶段保持未知")
    parser.add_argument("--no-admin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.offline:
        paths = [args.offline] if args.offline.is_file() else sorted(args.offline.glob("*.png"))
        if not paths:
            parser.error("没有找到 PNG 截图")
        offline(paths, args.output or (DATA / "reports" / "offline"), not args.no_ocr)
    else:
        if not args.no_admin:
            from elevation import relaunch_as_admin
            if relaunch_as_admin():
                # The elevated copy owns the GUI; this one must step aside.
                return
        from ui import run
        from lifecycle import SingleInstance, AlreadyRunning
        try:
            with SingleInstance():
                run()
        except AlreadyRunning as exc:
            logging.info("existing_instance %s", exc)
            from tkinter import Tk, messagebox
            notice = Tk()
            notice.withdraw()
            try:
                messagebox.showinfo("请先退出旧版 GamePilot", str(exc), parent=notice)
            finally:
                notice.destroy()


if __name__ == "__main__":
    main()
