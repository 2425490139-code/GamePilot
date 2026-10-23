"""Small red target outline, drawn by Tk's UI thread without taking focus."""
import tkinter as tk


def target_geometry(box, padding=6):
    if (len(box) != 4 or not all(type(value) is int for value in box)
            or box[2] <= box[0] or box[3] <= box[1]):
        raise ValueError("红框目标范围无效")
    left, top, right, bottom = box
    return left - padding, top - padding, right - left + padding * 2, bottom - top + padding * 2


class TargetOverlay:
    def __init__(self, root):
        self.root, self.window, self.canvas = root, None, None

    def create(self):
        if self.window is not None:
            return
        self.window = tk.Toplevel(self.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(background="#ff00ff")
        self.window.wm_attributes("-transparentcolor", "#ff00ff")
        self.window.wm_attributes("-disabled", True)
        self.window.wm_attributes("-topmost", True)
        self.canvas = tk.Canvas(self.window, background="#ff00ff", highlightthickness=0, borderwidth=0)
        self.canvas.pack(fill="both", expand=True)
        self.window.update_idletasks()

    def show(self, box):
        left, top, width, height = target_geometry(box)
        self.create()
        self.window.geometry(f"{width}x{height}{left:+d}{top:+d}")
        self.canvas.delete("all")
        # Draw outside the actual button, leaving its content unobstructed.
        self.canvas.create_rectangle(3, 3, width - 3, height - 3, outline="#ff2020", width=3)
        self.window.deiconify()
        self.window.update_idletasks()

    def hide(self):
        if self.window is not None:
            self.window.withdraw()
            self.window.update_idletasks()

    def close(self):
        if self.window is not None:
            self.window.destroy()
            self.window, self.canvas = None, None
