"""[overlay] table: on-screen text when a knob or note moves a DCS-BIOS control or DCS-BIOS (dis)connects.
Needs DCS in borderless window mode, exclusive fullscreen draws over it."""
import ctypes
import queue
import tkinter as tk
from dataclasses import dataclass

from midi import MidiEvent, Table, get

# position -> where the box sits in the free space of the primary monitor, 0 = left/top, 1 = right/bottom
ANCHORS: dict[str, tuple[float, float]] = {
    "NW": (0, 0), "N": (0.5, 0), "NE": (1, 0),
    "W": (0, 0.5), "C": (0.5, 0.5), "E": (1, 0.5),
    "SW": (0, 1), "S": (0.5, 1), "SE": (1, 1),
}
PUMP_S = 0.02  # tk event processing period
# ponytail: hardcoded look, config keys if it needs tuning
FONT = ("Segoe UI", 18, "bold")
ALPHA = 220  # box opacity 0-255
# when hidden: invisible, but unlike 0 Windows still composites it, see Overlay.__init__
HIDDEN_ALPHA = 1
GWL_EXSTYLE = -20
# layered (translucent), click-through, never takes focus from DCS, no taskbar button
EX_STYLE = 0x80000 | 0x20 | 0x8000000 | 0x80  # WS_EX_LAYERED | TRANSPARENT | NOACTIVATE | TOOLWINDOW
LWA_ALPHA = 2
SWP_MOVE = 0x1 | 0x4 | 0x10  # SWP_NOSIZE | NOZORDER | NOACTIVATE


@dataclass(frozen=True)
class OverlayConfig:
    position: str  # ANCHORS key
    offset_x: int  # pixels, + = right
    offset_y: int  # pixels, + = down
    timeout_s: float


def parse(cfg: Table) -> OverlayConfig | None:
    """Off without an [overlay] table."""
    o = get(cfg, "overlay", dict, None, "")
    if o is None:
        return None
    position = get(o, "position", str, "N", "[overlay]").upper()
    if position not in ANCHORS:
        raise ValueError(f"[overlay] position must be one of {' '.join(ANCHORS)}, got '{position}'")
    timeout_s = get(o, "timeout_s", float, 1.5, "[overlay]")
    if timeout_s <= 0:
        raise ValueError(f"[overlay] timeout_s must be above 0, got {timeout_s}")
    return OverlayConfig(position, get(o, "offset_x", int, 0, "[overlay]"), get(o, "offset_y", int, 0, "[overlay]"),
                         timeout_s)


def place(conf: OverlayConfig, screen: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """Top-left corner of the box on the screen."""
    fx, fy = ANCHORS[conf.position]
    return round((screen[0] - box[0]) * fx) + conf.offset_x, round((screen[1] - box[1]) * fy) + conf.offset_y


class Overlay:
    """Topmost click-through text box. show() runs on the MIDI thread and only queues the text,
    tick() owns tk on the main thread since tk isn't thread-safe."""

    def __init__(self, conf: OverlayConfig) -> None:
        self.conf = conf
        self.texts: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.left = 0.0  # seconds until hidden, 0 = hidden
        self.pump = 0.0
        self.user32 = ctypes.windll.user32
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # offsets in real pixels, crisp text under display scaling
        before = self.user32.GetForegroundWindow()
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True, "-alpha", 0.0)
        self.label = tk.Label(self.root, font=FONT, fg="white", bg="#202020", padx=16, pady=8)
        self.label.pack()
        self.root.update()
        self.hwnd = self.user32.GetParent(self.root.winfo_id())  # tk's frame window around the widgets
        self.user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, self.user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) | EX_STYLE)
        # never really hidden: a window appearing over DCS, or disappearing (hidden or alpha 0), switches
        # Windows between passing DCS frames straight to the screen and compositing, which blinks a frame.
        # The Windows volume popup blinks the same way.
        # ponytail: DCS stays composited while this runs, the flip back to direct output is lost
        # (matters with G-Sync/FreeSync); fine unless VRR stutters, then hide for real and live with the blink
        self.user32.SetLayeredWindowAttributes(self.hwnd, 0, HIDDEN_ALPHA, LWA_ALPHA)
        if self.user32.GetForegroundWindow() != before:  # tk's first map may grab focus from the terminal
            self.user32.SetForegroundWindow(before)

    def show(self, text: str) -> None:
        self.texts.put(text)

    def on_midi(self, ev: MidiEvent) -> None:
        """Text comes from show(), not from MIDI."""

    def tick(self, dt: float) -> None:
        self.pump += dt
        if self.pump < PUMP_S:
            return
        elapsed, self.pump = self.pump, 0.0
        text = None
        while not self.texts.empty():  # several changes since the last pump: latest wins
            text = self.texts.get()
        if text is not None:
            self.label.config(text=text)
            self.root.update_idletasks()  # resize to the new text before placing
            x, y = place(self.conf, (self.root.winfo_screenwidth(), self.root.winfo_screenheight()),
                         (self.root.winfo_reqwidth(), self.root.winfo_reqheight()))
            # moved right away, tk's geometry() would apply only after the box turned visible
            self.user32.SetWindowPos(self.hwnd, 0, x, y, 0, 0, SWP_MOVE)
            self.user32.SetLayeredWindowAttributes(self.hwnd, 0, ALPHA, LWA_ALPHA)
            self.left = self.conf.timeout_s
        elif self.left > 0:
            self.left -= elapsed
            if self.left <= 0:
                self.user32.SetLayeredWindowAttributes(self.hwnd, 0, HIDDEN_ALPHA, LWA_ALPHA)
        self.root.update()

    def close(self) -> None:
        self.root.destroy()
