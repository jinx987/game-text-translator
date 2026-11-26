"""
main.py - the overlay window, the hotkeys and the live mode.

  capture.py     does the screenshot + OCR        (no network)
  translator.py  does the translation             (the only network file)
  main.py        does everything you see and press

Hotkeys (modifier combos, so games do not steal plain function keys):
  Ctrl+Alt+T   translate once
  Ctrl+Alt+L   live mode on/off (reads the region by itself, over and over)
  Ctrl+Alt+M   move mode on/off (while on, click-through is paused so you can drag)
  Ctrl+Alt+D   show/hide the last captured crop (debug: what the OCR actually saw)
  Ctrl+Alt+Q   quit

The window is placed exactly on top of the capture region, so the English text
appears right where the original dialogue was. Windows is told to leave this
window out of screenshots (SetWindowDisplayAffinity), so the OCR keeps seeing
the game underneath instead of reading its own translation.

The game's language does not have to be set anywhere: the OCR tries the scripts
it has data for, and the translation service detects the language itself.
Extra OCR language packs are downloaded once, in the background, at startup.
"""

import ctypes
import os
import queue
import re
import threading
import time
import tkinter as tk
import traceback

import keyboard
from PIL import Image, ImageTk

import capture
import translator

# ---------------------------------------------------------------- settings --
HOTKEY_TRANSLATE = "ctrl+alt+t"
HOTKEY_LIVE = "ctrl+alt+l"
HOTKEY_MOVE = "ctrl+alt+m"
HOTKEY_DEBUG = "ctrl+alt+d"
HOTKEY_QUIT = "ctrl+alt+q"

LIVE_INTERVAL_MS = 400      # live mode looks at the screen every 0.4 s
RETRY_DELAY_MS = 2000       # wait before trying a failed translation again

# Games type dialogue out one character at a time. Translating that half-written
# sentence is what produces invented words and stray numbers, so a newly read
# text has to look identical a moment later before it is translated.
SETTLE_SECONDS = 0.18
SETTLE_MAX_SECONDS = 2.0    # ...but never hold a line back longer than this

BACKGROUND = "#111114"
WINDOW_ALPHA = 0.92         # nearly solid, so the Japanese behind is hidden
TEXT_COLOR = "#f2f2f5"
GREY = "#8f8f9c"
WARNING = "#ffb86b"
LIVE_COLOR = "#5cd68b"

# Fonts: the translation is Latin so it uses a clean UI font; the original
# Japanese line needs a font that actually HAS kanji, or Windows substitutes
# something that makes the characters look squashed and wrong.
TRANSLATION_FONT = "Segoe UI Semibold"
ORIGINAL_FONT = "Yu Gothic UI"

# Biggest first: the overlay picks the largest size that still fits the box, so
# the English looks like the Japanese dialogue it replaces instead of a caption.
FONT_SIZES = (23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11)

HINT_TEXT = "Ctrl+Alt+T translate · L live · M move · D crop · Q quit"

MIN_WIDTH = 320             # used only if the region is tiny
MIN_HEIGHT = 96

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log")

# OCR of a game box sometimes comes back as stray marks (wrong box, stylised
# font). Translating that produces nonsense, so we refuse it and say why.
# Any letter in any script counts: Latin, Japanese, Korean, Cyrillic...
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

# Windows style bits used to make the window click-through.
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080

# Windows 10 2004+: screenshots of the screen do not contain this window.
WDA_EXCLUDEFROMCAPTURE = 0x11


class Overlay:
    def __init__(self) -> None:
        # Hotkey callbacks run on another thread, so they only drop messages
        # here; the Tk main loop reads them in _drain_events().
        self.events: "queue.Queue[tuple]" = queue.Queue()

        self.busy = False
        self.live = False
        self.move_mode = False
        self.last_ocr_text = None
        self.last_translation = ""
        self.last_crop = None          # the exact pixels the last OCR used
        self.last_signature = None     # pixel bytes, to skip unchanged screens
        self.pending_text = ""         # text waiting to be confirmed as finished
        self.pending_since = 0.0
        self.pending_checks = 0
        self.queued = False            # a hotkey pressed while a job was running
        self.force_next = False        # set by a manual press: do not wait
        self.blank_reported = False
        self.crop_visible = False
        self.crop_photo = None         # Tk needs us to hold on to the image
        self.excluded = False          # True when screenshots ignore this window
        self._capture_mode_reported = False
        self._drag_offset = (0, 0)

        # The window covers the capture box exactly, so the translation lands
        # on top of the dialogue instead of floating next to it.
        region = capture.REGION
        self.width = max(MIN_WIDTH, region["width"])
        self.height = max(MIN_HEIGHT, region["height"])

        self.root = tk.Tk()
        self.root.title("Game Translator")
        self.root.overrideredirect(True)              # no title bar
        self.root.attributes("-topmost", True)        # always on top
        self.root.attributes("-alpha", WINDOW_ALPHA)
        self.root.configure(bg=BACKGROUND)
        start_x, start_y = self._start_position()
        self.root.geometry(f"{self.width}x{self.height}+{start_x}+{start_y}")

        # Everything is placed with grid instead of pack: rows that are empty
        # (status line, original text) can then be removed from the layout and
        # give their height to the translation.
        self.frame = tk.Frame(self.root, bg=BACKGROUND, padx=18, pady=8)
        self.frame.pack(fill="both", expand=True)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(1, weight=1)

        header = tk.Frame(self.frame, bg=BACKGROUND)
        header.grid(row=0, column=0, sticky="ew")

        self.live_label = tk.Label(
            header, text="", bg=BACKGROUND, fg=LIVE_COLOR,
            font=("Segoe UI", 8, "bold"), anchor="w",
        )
        self.live_label.pack(side="left")

        self.hint_label = tk.Label(
            header, text=HINT_TEXT, bg=BACKGROUND, fg=GREY,
            font=("Segoe UI", 8), anchor="e",
        )
        self.hint_label.pack(side="right")

        self.translation_label = tk.Label(
            self.frame, text="Press Ctrl+Alt+T to translate", bg=BACKGROUND, fg=TEXT_COLOR,
            font=(TRANSLATION_FONT, FONT_SIZES[0]), wraplength=self.width - 44,
            anchor="w", justify="left",
        )
        self.translation_label.grid(row=1, column=0, sticky="nsew", pady=(2, 0))

        self.original_label = tk.Label(
            self.frame, text="", bg=BACKGROUND, fg=GREY,
            font=(ORIGINAL_FONT, 10), wraplength=self.width - 44,
            anchor="w", justify="left",
        )

        self.status_label = tk.Label(
            self.frame, text="", bg=BACKGROUND, fg=WARNING,
            font=("Segoe UI", 9), wraplength=self.width - 44,
            anchor="w", justify="left",
        )

        # Debug picture of the last capture. It is created now but only shown
        # when you press Ctrl+Alt+D.
        self.crop_label = tk.Label(
            self.frame, bg=BACKGROUND, bd=1, relief="solid",
        )

        for widget in (
            self.root, self.frame, header, self.live_label, self.hint_label,
            self.translation_label, self.original_label, self.status_label,
        ):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        # Click-through and screenshot-invisibility need the real window, so
        # they are applied on the first map and once more shortly after start.
        self.root.bind("<Map>", self._on_map)
        self.root.report_callback_exception = self._tk_error
        self.root.after(300, self._on_map)
        self.root.after(50, self._drain_events)
        self.root.after(1500, self._keep_on_top)
        self._register_hotkeys()

    def _start_position(self) -> tuple:
        """Sit exactly on the capture box, which is by definition on screen."""
        region = capture.REGION
        return max(0, region["left"]), max(0, region["top"])

    def _keep_on_top(self) -> None:
        """Games often steal the top-most flag, so put ourselves back on top."""
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except tk.TclError:
            return
        self.root.after(1500, self._keep_on_top)

    # ------------------------------------------------------------- hotkeys --
    def _register_hotkeys(self) -> None:
        try:
            keyboard.add_hotkey(HOTKEY_TRANSLATE, lambda: self.events.put(("translate",)))
            keyboard.add_hotkey(HOTKEY_LIVE, lambda: self.events.put(("live",)))
            keyboard.add_hotkey(HOTKEY_MOVE, lambda: self.events.put(("move",)))
            keyboard.add_hotkey(HOTKEY_DEBUG, lambda: self.events.put(("crop",)))
            keyboard.add_hotkey(HOTKEY_QUIT, lambda: self.events.put(("quit",)))
        except Exception as exc:
            self._report(exc, "Hotkeys unavailable - try running as administrator")

    # ------------------------------------------------------ window plumbing --
    def _hwnd(self) -> int:
        """The real top-level Windows handle (winfo_id() is the inner child).

        Tk creates the outer frame slightly later than the inner window, so we
        ask Tk for the frame handle each time instead of caching one.
        """
        try:
            return int(self.root.wm_frame(), 16)
        except ValueError:                       # window not created yet
            return ctypes.windll.user32.GetParent(self.root.winfo_id())

    def _on_map(self, _event=None) -> None:
        self._set_click_through(not self.move_mode)
        self._exclude_from_capture()

    def _set_click_through(self, enabled: bool) -> None:
        """WS_EX_TRANSPARENT makes Windows send clicks straight to the game."""
        try:
            handle = self._hwnd()
            style = ctypes.windll.user32.GetWindowLongW(handle, GWL_EXSTYLE)
            style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
            if enabled:
                style |= WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            ctypes.windll.user32.SetWindowLongW(handle, GWL_EXSTYLE, style)
        except Exception as exc:
            self._report(exc, "Could not set click-through")

    def _exclude_from_capture(self) -> None:
        """Keep this window out of screenshots, so OCR reads the game not us."""
        try:
            result = ctypes.windll.user32.SetWindowDisplayAffinity(
                self._hwnd(), WDA_EXCLUDEFROMCAPTURE
            )
            self.excluded = bool(result)
        except Exception:
            self.excluded = False

        if self._capture_mode_reported:
            return
        self._capture_mode_reported = True
        if self.excluded:
            print("Capture mode: window hidden from screenshots (fast, no flicker).",
                  flush=True)
        else:
            print("Capture mode: window will hide briefly for each capture "
                  "(older Windows).", flush=True)

    def _apply_geometry(self) -> None:
        """Fixed box in front of the dialogue; only the crop view makes it taller."""
        extra = 0
        if self.crop_visible:
            self.root.update_idletasks()
            extra = self.crop_label.winfo_reqheight() + 8
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{self.width}x{self.height + extra}+{x}+{y}")

    def _set_status(self, text: str) -> None:
        """Show a warning/status line, or give the row back when it is empty."""
        if text == self.status_label.cget("text"):
            return                       # unchanged: do not redo the layout
        self.status_label.config(text=text)
        if text.strip():
            self.status_label.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        else:
            self.status_label.grid_remove()

    def _set_original(self, text: str) -> None:
        """Show the Japanese that was read, grey and small, under the English."""
        self.original_label.config(text=text)
        if text.strip():
            self.original_label.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        else:
            self.original_label.grid_remove()

    def _fit_text(self) -> None:
        """Use the largest font that still fits inside the covered box."""
        self.root.update_idletasks()
        other_rows = self.frame.winfo_reqheight() - self.translation_label.winfo_reqheight()
        available = self.height - other_rows - 8
        for size in FONT_SIZES:
            self.translation_label.config(font=(TRANSLATION_FONT, size))
            self.root.update_idletasks()
            if self.translation_label.winfo_reqheight() <= available:
                return

    def _drag_start(self, event) -> None:
        if not self.move_mode:
            return
        self._drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )

    def _drag_move(self, event) -> None:
        if not self.move_mode:
            return
        x = event.x_root - self._drag_offset[0]
        y = event.y_root - self._drag_offset[1]
        self.root.geometry(f"+{x}+{y}")

    def _grab_while_hidden(self) -> "Image.Image":
        """Fallback for older Windows: hide for a moment, shoot, come back."""
        self.root.withdraw()
        try:
            self.root.update()
            time.sleep(0.06)
            return capture.capture_region()
        finally:
            self.root.deiconify()
            self.root.update()

    # ---------------------------------------------------------- main thread --
    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]

                if kind == "result":
                    _, translation, original = event
                    self.last_translation = translation
                    self.translation_label.config(text=translation)
                    self._set_original(original)
                    self.hint_label.config(text="")
                    self._set_status("")
                    self._fit_text()
                elif kind == "warning":
                    self._set_status(event[1])
                    self._fit_text()
                elif kind == "status":
                    self._set_status(event[1])
                    self._fit_text()
                elif kind == "translate":
                    self._request_translate()
                elif kind == "live":
                    self._toggle_live()
                elif kind == "move":
                    self._toggle_move_mode()
                elif kind == "crop":
                    self._toggle_crop()
                elif kind == "refresh_crop":
                    if self.crop_visible:
                        self._show_crop()
                elif kind == "retry":
                    # Forget the last pixels, or the unchanged-screen shortcut
                    # would skip the retry entirely.
                    self.last_signature = None
                    self.root.after(RETRY_DELAY_MS, self._start_job)
                elif kind == "quit":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(50, self._drain_events)

    def _request_translate(self) -> None:
        """A deliberate Ctrl+Alt+T press: always do the work, tell the user."""
        if self.busy:
            # Never throw the key press away just because a live read is in
            # progress - that is what makes the hotkey look dead.
            self.queued = True
            return
        self.force_next = True
        self.last_signature = None       # same picture? read it again anyway
        self._set_status("Reading the screen...")
        self._fit_text()
        self._start_job()

    def _toggle_live(self) -> None:
        self.live = not self.live
        self.live_label.config(text="● LIVE" if self.live else "")
        self._set_status(
            "Live mode ON - it now reads the box by itself" if self.live
            else "Live mode OFF - press Ctrl+Alt+L to start it again"
        )
        self._fit_text()
        if self.live:
            self._live_tick()

    def _live_tick(self) -> None:
        if not self.live:
            return
        self._start_job()
        self.root.after(LIVE_INTERVAL_MS, self._live_tick)

    def _toggle_crop(self) -> None:
        """Show or hide a picture of the last captured region.

        This is the debug view: whatever the OCR was fed, shrunk to fit the
        overlay. If it says "OCR read no Japanese here", press Ctrl+Alt+D to
        see whether the box is on the dialogue or on something else.
        """
        if self.crop_visible:
            self.crop_label.grid_remove()
            self.crop_visible = False
        else:
            if self.last_crop is None:
                self._set_status("No capture yet - press Ctrl+Alt+T first")
                self._fit_text()
                return
            self._show_crop()
        self._apply_geometry()

    def _show_crop(self) -> None:
        # The caption sits above the picture, so the warning in the status
        # line (e.g. "OCR read no Japanese here") stays readable.
        width, height = self.last_crop.size
        self.crop_photo = ImageTk.PhotoImage(self._crop_thumbnail(self.last_crop))
        self.crop_label.config(
            image=self.crop_photo,
            text=f"Last capture {width}x{height} px - Ctrl+Alt+D to hide",
            compound="top", fg=GREY, font=("Segoe UI", 8),
        )
        self.crop_label.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.crop_visible = True

    def _crop_thumbnail(self, image):
        """Shrink the capture to the overlay's width so it fits on screen."""
        target_width = self.width - 40
        ratio = target_width / max(1, image.width)
        size = (target_width, max(1, round(image.height * ratio)))
        return image.resize(size, Image.LANCZOS)

    def _toggle_move_mode(self) -> None:
        self.move_mode = not self.move_mode
        self._set_click_through(not self.move_mode)
        self.root.configure(cursor="fleur" if self.move_mode else "")
        self._set_status(
            "Move mode ON - drag the window where you want it, then Ctrl+Alt+M"
            if self.move_mode else "Move mode off - clicks pass through to the game"
        )
        self._fit_text()

    # ------------------------------------------------------------- worker ---
    def _start_job(self) -> None:
        if self.busy:
            return

        image = None
        if not self.excluded:
            # No screenshot exclusion: take the picture from the main thread
            # with the window hidden, then hand it to the worker.
            try:
                image = self._grab_while_hidden()
            except Exception as exc:
                self._report(exc, "Screen capture failed")
                return

        self.busy = True
        threading.Thread(target=self._run_job, args=(image,), daemon=True).start()

    def _run_job(self, image=None) -> None:
        """One screenshot -> OCR -> translate, on a background thread."""
        try:
            if image is None:
                image = capture.capture_region()

            # Same pixels as last time: nothing changed, so do not OCR or
            # translate again. This is what makes live mode feel instant.
            signature = image.tobytes()
            if signature == self.last_signature:
                return
            self.last_signature = signature
            self.last_crop = image
            self.events.put(("refresh_crop",))

            # One flat colour means the screenshot came back black, which is
            # what exclusive fullscreen looks like to a screen grab.
            darkest, lightest = image.convert("L").getextrema()
            if lightest - darkest < 12:
                if not self.blank_reported:
                    self.blank_reported = True
                    self.events.put((
                        "warning",
                        "Screenshot is blank - run the game in borderless windowed "
                        "mode, not fullscreen.",
                    ))
                return
            self.blank_reported = False

            started = time.perf_counter()
            text = capture.read_text(image)
            ocr_seconds = time.perf_counter() - started

            if not text:
                self.events.put(("status", "No text found in the capture region."))
                return

            # Junk means the box is on the wrong thing (or the font is
            # unreadable). Say that instead of showing a nonsense translation.
            if len(LETTER_RE.findall(text)) < 2:
                if text != self.last_ocr_text:
                    self.last_ocr_text = text
                    self.events.put((
                        "warning",
                        f"OCR could not read the text here: {text[:40]}  "
                        f"(Ctrl+Alt+D shows the crop)",
                    ))
                return

            # Same text as last time: do not translate, do not redraw.
            if text == self.last_ocr_text:
                if self.force_next:
                    self.force_next = False
                    self.events.put(("status", ""))   # clear "Reading..."
                return

            # Live mode in a game that types text out: translate only once the
            # box has stopped changing, or the fragment gets invented words.
            if self.live and not self.force_next:
                if text != self.pending_text:
                    self.pending_text = text
                    self.pending_since = time.monotonic()
                    self.pending_checks = 0
                self.pending_checks += 1

                if self.pending_checks == 1:
                    # Cheapest possible "has the typing stopped?" test: look at
                    # the pixels again shortly after. No second OCR needed.
                    time.sleep(SETTLE_SECONDS)
                    if capture.capture_region().tobytes() != self.last_signature:
                        return              # still typing - next tick reads it again
                elif (self.pending_checks < 3
                      and time.monotonic() - self.pending_since < SETTLE_MAX_SECONDS):
                    return                  # something is animating: one more look
            self.force_next = False
            self.pending_text = ""

            try:
                started = time.perf_counter()
                translation = translator.translate(text)
                print(f"[{time.strftime('%H:%M:%S')}] {ocr_seconds:.2f}s OCR + "
                      f"{time.perf_counter() - started:.2f}s translate -> {translation}",
                      flush=True)
            except translator.TranslationError as exc:
                # Keep whatever is on screen, warn quietly, try again shortly.
                self.last_ocr_text = None
                self.events.put(("warning", f"Translation service busy - retrying... ({exc})"))
                self.events.put(("retry",))
                return

            self.last_ocr_text = text
            self.events.put(("result", translation, text))

        except Exception as exc:
            self._report(exc, "Screen/OCR problem")
        finally:
            self.busy = False
            self.force_next = False
            if self.queued:
                # A hotkey was pressed while we were busy: honour it now.
                self.queued = False
                self.events.put(("translate",))

    # -------------------------------------------------------------- errors --
    def _report(self, exc, prefix) -> None:
        """Show the problem on screen, in the terminal and in error.log."""
        message = f"{prefix}: {exc}"
        print(message, flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__)
        self._log(message)
        self.events.put(("warning", message))

    def _tk_error(self, exc_type, exc_value, exc_tb) -> None:
        """Tkinter callbacks otherwise fail silently; log them instead."""
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        print(text, flush=True)
        self._log(f"UI error: {exc_value}")
        try:
            self._set_status(f"UI problem: {exc_value}")
        except tk.TclError:
            pass

    def _log(self, message) -> None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
        except OSError:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    capture.enable_dpi_awareness()
    try:
        capture.check_tesseract()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)

    # Japanese and English are what the OCR needs to be useful at all, so a
    # fresh clone fetches them before the overlay opens. The remaining scripts
    # arrive in the background and the OCR uses whatever has arrived so far.
    if not {"jpn", "eng"} <= capture.available_languages():
        print("OCR: downloading Japanese and English language data (first run)",
              flush=True)
        translator.ensure_ocr_languages(capture.TESSDATA_DIR, codes=("jpn", "eng"))
    threading.Thread(
        target=translator.ensure_ocr_languages,
        args=(capture.TESSDATA_DIR,),
        daemon=True,
    ).start()

    region = capture.REGION
    print("Game translator is running.", flush=True)
    print(f"OCR languages installed: {', '.join(sorted(capture.available_languages()))}",
          flush=True)
    print(f"Capture region: left={region['left']} top={region['top']} "
          f"width={region['width']} height={region['height']}", flush=True)
    print(f"hotkeys: {HOTKEY_TRANSLATE} / {HOTKEY_LIVE} / {HOTKEY_MOVE} / "
          f"{HOTKEY_DEBUG} (crop) / {HOTKEY_QUIT}", flush=True)
    print(f"Problems are written to: {LOG_FILE}", flush=True)

    Overlay().run()


if __name__ == "__main__":
    main()
