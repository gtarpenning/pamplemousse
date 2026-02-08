# pamplemousse — macOS menu bar Pomodoro timer
# pip install rumps pyobjc-framework-Cocoa pyobjc-framework-Quartz rich && python pamplemousse.py

import os
import shutil
import signal
import subprocess
import sys
import time
from enum import Enum, auto
from pathlib import Path

import rumps
from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSButton,
    NSColor,
    NSEvent,
    NSFloatingWindowLevel,
    NSFont,
    NSGraphicsContext,
    NSImage,
    NSScreen,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSObject, NSTimer as FoundationTimer
from Quartz import CGEventSourceSecondsSinceLastEventType

# CGEventSource constants
_CG_EVENT_SOURCE_STATE_HID = 1
_CG_EVENT_KEY_DOWN = 10

DEFAULT_WORK_MINS = 25
DEFAULT_BREAK_MINS = 5
WORK_DURATION_OPTIONS = [1, 15, 20, 25, 30, 45, 60]
BREAK_DURATION_OPTIONS = [1, 3, 5, 10, 15, 20]

RED_OVERLAY_COLOR = (1.0, 0.0, 0.0, 0.35)
GREEN_OVERLAY_COLOR = (0.0, 0.7, 0.0, 0.30)
PUNISHMENT_SECS = 3
TIMER_FONT_SIZE = 200
MOUSE_THRESHOLD = 3
ARM_DELAY = 1.5

# NSEventMask bits for mouse movement
MOUSE_MOVE_MASK = (1 << 5) | (1 << 6) | (1 << 7) | (1 << 27)

# NSWindowCollectionBehavior flags
CAN_JOIN_ALL_SPACES = 1 << 0
FULL_SCREEN_AUXILIARY = 1 << 4

# AppKit constants
NS_TEXT_ALIGN_CENTER = 2
NS_FONT_WEIGHT_BOLD = 0.4


class TimerState(Enum):
    IDLE = auto()
    RUNNING = auto()
    PAUSED = auto()


# -- Pomodoro app -------------------------------------------------------------


class PomodoroApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Pomodoro", title="🍅")
        self.work_mins = DEFAULT_WORK_MINS
        self.break_mins = DEFAULT_BREAK_MINS
        self.timer = rumps.Timer(self.tick, 1)
        self.seconds_left = 0
        self.state = TimerState.IDLE
        self._break_overlay = None
        self._green_overlay = None
        self._total_work_seconds = self.work_mins * 60
        self._blink_state = False
        self._last_work_tick = 0.0

        self.start_button = rumps.MenuItem("Start", callback=self.start)
        self.stop_button = rumps.MenuItem("Stop")

        self.work_menu = self._build_duration_menu(
            "Work Duration", WORK_DURATION_OPTIONS, self.work_mins, self.set_work,
        )
        self.break_menu = self._build_duration_menu(
            "Break Duration", BREAK_DURATION_OPTIONS, self.break_mins, self.set_break,
        )

        self.settings_menu = rumps.MenuItem("Settings")
        self.settings_menu[self.work_menu.title] = self.work_menu
        self.settings_menu[self.break_menu.title] = self.break_menu

        self.time_left_item = rumps.MenuItem("")

        self.menu = [
            self.time_left_item,
            self.start_button,
            self.stop_button,
            None,
            self.settings_menu,
        ]
        self.time_left_item._menuitem.setHidden_(True)

    @staticmethod
    def _build_duration_menu(
        title: str,
        options: list[int],
        default: int,
        callback,
    ) -> rumps.MenuItem:
        menu = rumps.MenuItem(title)
        for mins in options:
            item = rumps.MenuItem(f"{mins} min", callback=callback)
            if mins == default:
                item.state = True
            menu.add(item)
        return menu

    # -- Start / Pause / Resume / Stop state machine --------------------------

    def start(self, _sender) -> None:
        if self.state == TimerState.IDLE:
            self._start_work_session()
        elif self.state == TimerState.RUNNING:
            self.timer.stop()
            self.state = TimerState.PAUSED
            self.start_button.title = "Resume"
        else:
            self._last_work_tick = time.time()
            self.timer.start()
            self.state = TimerState.RUNNING
            self.start_button.title = "Pause"

    def stop(self, _sender) -> None:
        self.timer.stop()
        if self._break_overlay:
            self._break_overlay.dismiss()
            self._break_overlay = None
        if self._green_overlay:
            g = self._green_overlay
            self._green_overlay = None
            g.on_dismiss = lambda: None
            g.dismiss()
        self._clear_tomato_icon()
        self.time_left_item._menuitem.setHidden_(True)
        self.title = "🍅"
        self.state = TimerState.IDLE
        self.start_button.title = "Start"
        self.start_button.set_callback(self.start)
        self.stop_button.set_callback(None)

    # -- Work timer tick -------------------------------------------------------

    def tick(self, _sender) -> None:
        now = time.time()
        gap = now - self._last_work_tick
        self._last_work_tick = now
        if gap >= self.break_mins * 60:
            self.timer.stop()
            self._clear_tomato_icon()
            self.time_left_item._menuitem.setHidden_(True)
            self._start_work_session()
            return
        self.seconds_left -= 1
        if self.seconds_left <= 0:
            self.timer.stop()
            self._clear_tomato_icon()
            self.time_left_item._menuitem.setHidden_(True)
            self._start_break_overlay()
        else:
            self.time_left_item.title = _fmt(self.seconds_left) + " remaining"
            self.title = ""
            self._update_tomato_icon()

    def _update_tomato_icon(self):
        fraction = self.seconds_left / self._total_work_seconds
        if self.seconds_left <= 60:
            if self.seconds_left % 2 == 0:
                self._blink_state = not self._blink_state
            self._clear_tomato_icon()
            self.title = "🍅" if self._blink_state else _fmt(self.seconds_left)
            return
        image = _create_tomato_icon(fraction)
        try:
            self._nsapp.nsstatusitem.button().setImage_(image)
        except AttributeError:
            pass

    def _clear_tomato_icon(self):
        try:
            self._nsapp.nsstatusitem.button().setImage_(None)
        except AttributeError:
            pass

    # -- Break / green overlay lifecycle ---------------------------------------

    def _start_break_overlay(self) -> None:
        self.title = "☕"
        self.start_button.title = "On Break"
        self.start_button.set_callback(None)
        self._break_overlay = BreakOverlay(
            self.break_mins * 60,
            on_complete=self._on_break_complete,
            on_skip=self._on_break_skipped,
        )
        self._break_overlay.show()

    def _on_break_complete(self) -> None:
        self._break_overlay = None
        self.title = "✓"
        self._green_overlay = GreenOverlay(on_dismiss=self._on_green_dismissed)
        self._green_overlay.show()

    def _on_break_skipped(self) -> None:
        self._break_overlay = None
        self._start_work_session()

    def _on_green_dismissed(self) -> None:
        self._green_overlay = None
        self._start_work_session()

    def _start_work_session(self) -> None:
        self._total_work_seconds = self.work_mins * 60
        self.seconds_left = self._total_work_seconds
        self.title = ""
        self.state = TimerState.RUNNING
        self._blink_state = False
        self._last_work_tick = time.time()
        self.start_button.title = "Pause"
        self.start_button.set_callback(self.start)
        self.stop_button.set_callback(self.stop)
        self.time_left_item.title = _fmt(self.seconds_left) + " remaining"
        self.time_left_item._menuitem.setHidden_(False)
        self._update_tomato_icon()
        self.timer.start()

    # -- Settings callbacks ---------------------------------------------------

    def _set_duration(self, sender, menu: rumps.MenuItem, attr: str) -> None:
        mins = int(sender.title.split()[0])
        setattr(self, attr, mins)
        for item in menu.values():
            item.state = item.title == sender.title

    def set_work(self, sender) -> None:
        old_total = self._total_work_seconds
        self._set_duration(sender, self.work_menu, "work_mins")
        new_total = self.work_mins * 60
        self._total_work_seconds = new_total
        if self.state in (TimerState.RUNNING, TimerState.PAUSED):
            elapsed = old_total - self.seconds_left
            self.seconds_left = max(new_total - elapsed, 0)
            self._update_tomato_icon()

    def set_break(self, sender) -> None:
        old_total = self.break_mins * 60
        self._set_duration(sender, self.break_menu, "break_mins")
        if self._break_overlay and self._break_overlay._active:
            elapsed = old_total - self._break_overlay.seconds_left
            self._break_overlay.seconds_left = max(self.break_mins * 60 - elapsed, 0)
            self._break_overlay._deadline = time.time() + self._break_overlay.seconds_left
            if self._break_overlay._label:
                self._break_overlay._label.setStringValue_(
                    _fmt(self._break_overlay.seconds_left),
                )


# -- Break overlay (red tint + large countdown + mouse/keyboard punishment) ---


class BreakOverlay:
    def __init__(self, break_seconds: int, on_complete, on_skip):
        self.seconds_left = break_seconds
        self.on_complete = on_complete
        self.on_skip = on_skip
        self._tint_windows: list[NSWindow] = []
        self._timer_window = None
        self._button_window = None
        self._label = None
        self._button_target = None
        self._tick_timer = None
        self._mouse_monitor = None
        self._punished_until = 0.0
        self._last_mouse_pos = None
        self._active = False
        self._deadline = 0.0
        self._last_tick_time = 0.0

    def show(self):
        self._active = True
        self._deadline = time.time() + self.seconds_left
        self._last_tick_time = time.time()

        for screen in NSScreen.screens():
            self._tint_windows.append(
                _create_tint_window(screen.frame(), RED_OVERLAY_COLOR),
            )

        self._timer_window, self._label = _create_timer_window(
            _fmt(self.seconds_left),
        )

        self._button_target = _ButtonTarget.alloc().init()
        self._button_target._py_callback = self._skip
        self._button_window = _create_button_window(
            "I hate myself", self._button_target,
        )

        self._tick_timer = (
            FoundationTimer.scheduledTimerWithTimeInterval_repeats_block_(
                1.0, True, lambda _: self._tick(),
            )
        )

        FoundationTimer.scheduledTimerWithTimeInterval_repeats_block_(
            ARM_DELAY, False, lambda _: self._arm_mouse(),
        )

    def _arm_mouse(self):
        if not self._active:
            return
        self._last_mouse_pos = NSEvent.mouseLocation()
        self._mouse_monitor = (
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                MOUSE_MOVE_MASK, self._on_mouse_move,
            )
        )

    def _on_mouse_move(self, event):
        if not self._active:
            return
        pos = NSEvent.mouseLocation()
        if self._last_mouse_pos:
            dx = abs(pos.x - self._last_mouse_pos.x)
            dy = abs(pos.y - self._last_mouse_pos.y)
            if dx > MOUSE_THRESHOLD or dy > MOUSE_THRESHOLD:
                self._punished_until = time.time() + PUNISHMENT_SECS
                if self._label:
                    self._label.setTextColor_(NSColor.redColor())
        self._last_mouse_pos = pos

    def _tick(self):
        if not self._active:
            return
        now = time.time()
        elapsed = now - self._last_tick_time
        self._last_tick_time = now
        since_key = CGEventSourceSecondsSinceLastEventType(
            _CG_EVENT_SOURCE_STATE_HID, _CG_EVENT_KEY_DOWN,
        )
        if since_key < 1.0:
            self._punished_until = now + PUNISHMENT_SECS
            if self._label:
                self._label.setTextColor_(NSColor.redColor())
        if now < self._punished_until:
            self._deadline += elapsed
            return
        if self._label:
            self._label.setTextColor_(NSColor.whiteColor())
        self.seconds_left = max(0, int(self._deadline - now))
        if self._label:
            self._label.setStringValue_(_fmt(self.seconds_left))
        if self.seconds_left <= 0:
            callback = self.on_complete
            self.dismiss()
            callback()

    def _skip(self):
        if not self._active:
            return
        callback = self.on_skip
        self.dismiss()
        callback()

    def dismiss(self):
        self._active = False
        if self._tick_timer:
            self._tick_timer.invalidate()
            self._tick_timer = None
        if self._mouse_monitor:
            NSEvent.removeMonitor_(self._mouse_monitor)
            self._mouse_monitor = None
        for w in self._tint_windows:
            w.orderOut_(None)
        self._tint_windows.clear()
        if self._timer_window:
            self._timer_window.orderOut_(None)
            self._timer_window = None
        if self._button_window:
            self._button_window.orderOut_(None)
            self._button_window = None
        self._label = None
        self._button_target = None


# -- Green overlay (break done — waiting for mouse to restart) ----------------


class GreenOverlay:
    def __init__(self, on_dismiss):
        self.on_dismiss = on_dismiss
        self._tint_windows: list[NSWindow] = []
        self._timer_window = None
        self._label = None
        self._mouse_monitor = None
        self._active = False

    def show(self):
        self._active = True

        for screen in NSScreen.screens():
            self._tint_windows.append(
                _create_tint_window(screen.frame(), GREEN_OVERLAY_COLOR),
            )

        self._timer_window, self._label = _create_timer_window("00:00")

        FoundationTimer.scheduledTimerWithTimeInterval_repeats_block_(
            ARM_DELAY, False, lambda _: self._arm_mouse(),
        )

    def _arm_mouse(self):
        if not self._active:
            return
        self._mouse_monitor = (
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                MOUSE_MOVE_MASK, self._on_mouse_move,
            )
        )

    def _on_mouse_move(self, event):
        if not self._active:
            return
        callback = self.on_dismiss
        self.dismiss()
        callback()

    def dismiss(self):
        if not self._active:
            return
        self._active = False
        if self._mouse_monitor:
            NSEvent.removeMonitor_(self._mouse_monitor)
            self._mouse_monitor = None
        for w in self._tint_windows:
            w.orderOut_(None)
        self._tint_windows.clear()
        if self._timer_window:
            self._timer_window.orderOut_(None)
            self._timer_window = None
        self._label = None


# -- AppKit helpers -----------------------------------------------------------


class _ButtonTarget(NSObject):
    def pressed_(self, sender):
        cb = getattr(self, "_py_callback", None)
        if cb:
            cb()


def _fmt(seconds: int) -> str:
    m, s = divmod(max(seconds, 0), 60)
    return f"{m:02d}:{s:02d}"


def _create_tomato_icon(fraction: float) -> NSImage:
    s = 18
    image = NSImage.alloc().initWithSize_((s, s))
    image.lockFocus()

    cx, cy = 9.0, 7.0
    r = 6.5
    body_rect = ((cx - r, cy - r), (r * 2, r * 2))

    if fraction >= 0.99:
        NSColor.redColor().setFill()
        NSBezierPath.bezierPathWithOvalInRect_(body_rect).fill()
    elif fraction > 0.01:
        NSColor.colorWithRed_green_blue_alpha_(0.6, 0.0, 0.0, 0.2).setFill()
        NSBezierPath.bezierPathWithOvalInRect_(body_rect).fill()

        NSGraphicsContext.saveGraphicsState()
        NSBezierPath.bezierPathWithOvalInRect_(body_rect).addClip()

        end_angle = 90 - fraction * 360
        pie = NSBezierPath.bezierPath()
        pie.moveToPoint_((cx, cy))
        pie.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            (cx, cy), r, 90, end_angle, True,
        )
        pie.closePath()
        NSColor.redColor().setFill()
        pie.fill()

        NSGraphicsContext.restoreGraphicsState()
    else:
        NSColor.colorWithRed_green_blue_alpha_(0.6, 0.0, 0.0, 0.2).setStroke()
        path = NSBezierPath.bezierPathWithOvalInRect_(body_rect)
        path.setLineWidth_(1.0)
        path.stroke()

    NSColor.colorWithRed_green_blue_alpha_(0.2, 0.65, 0.2, 1.0).setFill()
    stem = NSBezierPath.bezierPath()
    stem.moveToPoint_((cx, cy + r))
    stem.lineToPoint_((cx - 2, cy + r + 3.5))
    stem.lineToPoint_((cx, cy + r + 1.5))
    stem.lineToPoint_((cx + 2, cy + r + 3.5))
    stem.closePath()
    stem.fill()

    image.unlockFocus()
    image.setTemplate_(False)
    return image


def _create_tint_window(frame, color) -> NSWindow:
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
    )
    win.setLevel_(NSFloatingWindowLevel + 1)
    win.setOpaque_(False)
    win.setIgnoresMouseEvents_(True)
    win.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(*color))
    win.setCollectionBehavior_(CAN_JOIN_ALL_SPACES | FULL_SCREEN_AUXILIARY)
    win.setContentView_(NSView.alloc().initWithFrame_(frame))
    win.orderFrontRegardless()
    return win


def _screen_with_mouse() -> NSScreen:
    mouse = NSEvent.mouseLocation()
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= mouse.x <= f.origin.x + f.size.width
                and f.origin.y <= mouse.y <= f.origin.y + f.size.height):
            return s
    return NSScreen.mainScreen()


def _create_timer_window(text: str) -> tuple[NSWindow, NSTextField]:
    scr = _screen_with_mouse().frame()
    w, h = 900, 300
    x = scr.origin.x + (scr.size.width - w) / 2 - 180
    y = scr.origin.y + (scr.size.height - h) / 2

    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (w, h)), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
    )
    win.setLevel_(NSFloatingWindowLevel + 2)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setIgnoresMouseEvents_(True)
    win.setCollectionBehavior_(CAN_JOIN_ALL_SPACES | FULL_SCREEN_AUXILIARY)

    label = NSTextField.alloc().initWithFrame_(((0, 0), (w, h)))
    label.setStringValue_(text)
    label.setFont_(
        NSFont.monospacedDigitSystemFontOfSize_weight_(
            TIMER_FONT_SIZE, NS_FONT_WEIGHT_BOLD,
        ),
    )
    label.setTextColor_(NSColor.whiteColor())
    label.setDrawsBackground_(False)
    label.setBordered_(False)
    label.setEditable_(False)
    label.setSelectable_(False)
    label.setAlignment_(NS_TEXT_ALIGN_CENTER)

    win.contentView().addSubview_(label)
    win.orderFrontRegardless()
    return win, label


def _create_button_window(title: str, target: _ButtonTarget) -> NSWindow:
    main = NSScreen.mainScreen().frame()
    bw, bh = 180, 40
    x = main.origin.x + main.size.width - bw - 40
    y = main.origin.y + 40

    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (bw, bh)), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
    )
    win.setLevel_(NSFloatingWindowLevel + 3)
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0, 0, 0, 0.7))
    win.setIgnoresMouseEvents_(False)
    win.setCollectionBehavior_(CAN_JOIN_ALL_SPACES | FULL_SCREEN_AUXILIARY)

    button = NSButton.alloc().initWithFrame_(((0, 0), (bw, bh)))
    button.setTitle_(title)
    button.setTarget_(target)
    button.setAction_("pressed:")
    button.setBezelStyle_(1)

    win.contentView().addSubview_(button)
    win.orderFrontRegardless()
    return win


# -- Process management -------------------------------------------------------

PLIST_LABEL = "com.pamplemousse"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
PID_FILE = Path.home() / ".pamplemousse.pid"


def _install_launch_agent() -> None:
    if LAUNCH_AGENT.exists():
        return
    import plistlib

    exe = shutil.which("pamplemousse") or sys.executable
    args = [exe, "--run"] if exe != sys.executable else [exe, __file__, "--run"]
    plist = {"Label": PLIST_LABEL, "ProgramArguments": args, "RunAtLoad": True}
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(plist, f)


def _get_running_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return None


def _stop(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)
    PID_FILE.unlink(missing_ok=True)


def _spawn() -> None:
    exe = shutil.which("pamplemousse")
    cmd = [exe, "--run"] if exe else [sys.executable, __file__, "--run"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))


def _run_app() -> None:
    PID_FILE.write_text(str(os.getpid()))
    try:
        PomodoroApp().run()
    finally:
        PID_FILE.unlink(missing_ok=True)


def main():
    if "--run" in sys.argv:
        _run_app()
        return

    from rich import print as rprint

    _install_launch_agent()
    pid = _get_running_pid()
    if pid:
        answer = input("pamplemousse already running, stop it? [y/N] ")
        if answer.lower() == "y":
            _stop(pid)
            rprint("[red]stopped[/red]")
        return

    _spawn()
    rprint("[green]pamplemousse started[/green]")


if __name__ == "__main__":
    PomodoroApp().run()
