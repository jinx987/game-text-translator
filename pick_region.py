"""
pick_region.py - draw the capture box around your game's dialogue text, once.

Run it while the game is showing a dialogue line:

    python pick_region.py

Drag a rectangle over the text you want translated. The screen dims while you
drag, a preview shows exactly what the OCR will see (and what it read), and
"Use this" writes the four numbers into capture.py for you.

Run it again whenever the game window moves, you change resolution, or you
switch to fullscreen. Restart main.py afterwards - it reads REGION at startup.
"""

import ctypes
import re
import tkinter as tk

import mss
from PIL import Image, ImageTk

import capture

CAPTURE_FILE = capture.__file__

ACCENT = "#00ff9c"
DANGER = "#ff6b6b"
BG = "#14141a"
FG = "#f2f2f5"


def _virtual_screen() -> dict:
    """The whole desktop, including monitors to the left/above the main one."""
    metrics = ctypes.windll.user32.GetSystemMetrics
    return {
        "left": metrics(76),    # SM_XVIRTUALSCREEN
        "top": metrics(77),     # SM_YVIRTUALSCREEN
        "width": metrics(78),   # SM_CXVIRTUALSCREEN
        "height": metrics(79),  # SM_CYVIRTUALSCREEN
    }


def grab(region: dict) -> Image.Image:
    """Screenshot any region of the screen as a Pillow image."""
    with mss.mss() as screen:
        shot = screen.grab(region)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def save_region(left: int, top: int, width: int, height: int) -> None:
    """Rewrite the REGION line in capture.py, leaving the rest of the file alone."""
    # newline="" keeps whatever line endings the file already has.
    with open(CAPTURE_FILE, "r", encoding="utf-8", newline="") as handle:
        source = handle.read()

    new_line = f'REGION = {{"left": {left}, "top": {top}, "width": {width}, "height": {height}}}'
    updated, count = re.subn(r"REGION = \{[^}]*\}", new_line, source, count=1)
    if not count:
        raise RuntimeError("could not find the REGION line in capture.py")

    with open(CAPTURE_FILE, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)


class RegionPicker:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.desktop = _virtual_screen()
        self.canvas = None
        self.photo = None
        self.rect = None
        self.start = None
        self.selection = None
        self.root.bind("<Escape>", lambda _event: self.root.destroy())
        self._show_picker()

    # ----------------------------------------------------------- selection --
    def _show_picker(self) -> None:
        self.selection = None
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.35)
        self.root.geometry(
            f"{self.desktop['width']}x{self.desktop['height']}"
            f"{self.desktop['left']:+d}{self.desktop['top']:+d}"
        )
        self.root.configure(bg=BG)

        self.canvas = tk.Canvas(self.root, bg=BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.desktop["width"] // 2, 60,
            text="Drag a box around the dialogue text"
                 "        Esc = cancel",
            fill=ACCENT, font=("Segoe UI", 18, "bold"),
        )
        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)

        # Move the mouse to the middle of the screen so the drag feels natural.
        self.root.update_idletasks()
        self.root.lift()

    def _drag_start(self, event) -> None:
        self.start = (event.x_root, event.y_root)
        if self.rect is not None:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline=ACCENT, width=2, fill="",
        )

    def _drag_move(self, event) -> None:
        if self.start is None or self.rect is None:
            return
        self.canvas.coords(
            self.rect,
            self.start[0] - self.desktop["left"], self.start[1] - self.desktop["top"],
            event.x, event.y,
        )

    def _drag_end(self, event) -> None:
        if self.start is None:
            return
        x1, y1 = self.start
        x2, y2 = event.x_root, event.y_root
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        self.start = None

        if width < 20 or height < 10:
            self._show_picker()          # a click, not a drag: just try again
            return

        self.selection = (left, top, width, height)
        self._show_preview()

    # ------------------------------------------------------------- preview --
    def _show_preview(self) -> None:
        left, top, width, height = self.selection
        image = grab({"left": left, "top": top, "width": width, "height": height})

        try:
            text = capture.read_text(image).strip()
            problem = ""
        except Exception as exc:
            text, problem = "", f"OCR failed: {exc}"

        preview = image
        if width > 900:
            ratio = 900 / width
            preview = image.resize((900, max(1, int(height * ratio))))

        self.root.attributes("-alpha", 1.0)
        if self.canvas is not None:
            self.canvas.destroy()
            self.canvas = None

        self.photo = ImageTk.PhotoImage(preview)
        box = tk.Frame(self.root, bg=BG, padx=16, pady=14)
        box.pack(fill="both", expand=True)

        tk.Label(
            box, text=f"What the OCR sees  ({width} x {height} at {left}, {top})",
            bg=BG, fg=FG, font=("Segoe UI", 11, "bold"), anchor="w",
        ).pack(fill="x")
        tk.Label(box, image=self.photo, bg=BG, bd=0).pack(pady=(10, 10))

        tk.Label(
            box,
            text=(text if text else "Nothing readable here - try a tighter box around the text."),
            bg=BG, fg=FG if text else DANGER,
            font=("Segoe UI", 10), wraplength=880, justify="left", anchor="w",
        ).pack(fill="x")
        if problem:
            tk.Label(box, text=problem, bg=BG, fg=DANGER,
                     font=("Segoe UI", 9), wraplength=880, anchor="w").pack(fill="x")

        buttons = tk.Frame(box, bg=BG)
        buttons.pack(fill="x", pady=(14, 0))
        for label, command, color in (
            ("Use this region", self._use, ACCENT),
            ("Pick again", self._show_picker, FG),
            ("Cancel", self.root.destroy, FG),
        ):
            tk.Button(
                buttons, text=label, command=command,
                bg="#26262f", fg=color, activebackground="#333340", activeforeground=color,
                relief="flat", padx=14, pady=6, font=("Segoe UI", 10, "bold"),
            ).pack(side="left", padx=(0, 8))

        self.root.update_idletasks()
        window_width = min(960, max(preview.width + 40, 520))
        self.root.geometry(
            f"{window_width}x{box.winfo_reqheight() + 40}"
            f"{self.desktop['left'] + (self.desktop['width'] - window_width) // 2:+d}"
            f"{self.desktop['top'] + max(40, (self.desktop['height'] - 500) // 2):+d}"
        )

    def _use(self) -> None:
        left, top, width, height = self.selection
        save_region(left, top, width, height)
        print(f"Saved to capture.py: left={left} top={top} width={width} height={height}")
        print("Restart main.py to use it.")
        self.root.destroy()


def main() -> None:
    capture.enable_dpi_awareness()
    try:
        capture.check_tesseract()
    except RuntimeError as exc:
        print(f"WARNING: {exc}")

    root = tk.Tk()
    root.title("Pick capture region")
    RegionPicker(root)
    root.mainloop()


if __name__ == "__main__":
    main()
