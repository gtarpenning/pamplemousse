# pamplemousse — macOS menu bar Pomodoro timer
# pip install rumps pyobjc-framework-Cocoa rich && python pamplemousse.py

import os
import shutil
import signal
import subprocess
import sys
from enum import Enum, auto
from pathlib import Path

import rumps
from AppKit import (
    NSBackingStoreBuffered,
    NSColor,
    NSFloatingWindowLevel,
    NSScreen,
    NSView,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSTimer as FoundationTimer

DEFAULT_WORK_MINS = 25
DEFAULT_BREAK_MINS = 5
WORK_DURATION_OPTIONS = [15, 20, 25, 30, 45, 60]
BREAK_DURATION_OPTIONS = [3, 5, 10, 15, 20]

FLASH_COLOR = (1.0, 0.0, 0.0, 0.35)  # RGBA
DEFAULT_FLASH_SECS = 1.0
FLASH_DURATION_OPTIONS = [0.5, 1, 2, 3]
REMINDER_INTERVAL = 60  # seconds between reminder flashes

# NSWindowCollectionBehavior flags
CAN_JOIN_ALL_SPACES = 1 << 0
FULL_SCREEN_AUXILIARY = 1 << 4


class TimerState(Enum):
    IDLE = auto()
    RUNNING = auto()
    PAUSED = auto()


class PomodoroApp(rumps.App):
    def __init__(self) -> None:
        super().__init__("Pomodoro", title="🍅")
        self.work_mins = DEFAULT_WORK_MINS
        self.break_mins = DEFAULT_BREAK_MINS
        self.timer = rumps.Timer(self.tick, 1)
        self.seconds_left = 0
        self.on_break = False
        self.state = TimerState.IDLE

        self.flash_enabled = True
        self.flash_secs = DEFAULT_FLASH_SECS
        self.flash_reminder_2 = False
        self.flash_reminder_3 = False

        self.start_button = rumps.MenuItem("Start", callback=self.start)
        # Stop is disabled until a session is actively running
        self.stop_button = rumps.MenuItem("Stop")

        self.work_menu = self._build_duration_menu(
            "Work Duration", WORK_DURATION_OPTIONS, self.work_mins, self.set_work,
        )
        self.break_menu = self._build_duration_menu(
            "Break Duration", BREAK_DURATION_OPTIONS, self.break_mins, self.set_break,
        )
        self.flash_menu = self._build_flash_menu()

        self.settings_menu = rumps.MenuItem("Settings")
        self.settings_menu[self.work_menu.title] = self.work_menu
        self.settings_menu[self.break_menu.title] = self.break_menu
        self.settings_menu[self.flash_menu.title] = self.flash_menu

        self.menu = [
            self.start_button,
            self.stop_button,
            None,
            self.settings_menu,
        ]

    def _build_flash_menu(self) -> rumps.MenuItem:
        menu = rumps.MenuItem("Screen Flash")

        self._flash_toggle = rumps.MenuItem("Enabled", callback=self._toggle_flash)
        self._flash_toggle.state = True

        self._flash_dur_menu = rumps.MenuItem("Duration")
        for secs in FLASH_DURATION_OPTIONS:
            label = f"{secs:g} sec"
            item = rumps.MenuItem(label, callback=self._set_flash_duration)
            if secs == self.flash_secs:
                item.state = True
            self._flash_dur_menu.add(item)

        self._reminder_2_toggle = rumps.MenuItem(
            "Reminder at +1 min", callback=self._toggle_reminder_2,
        )
        self._reminder_3_toggle = rumps.MenuItem(
            "Reminder at +2 min", callback=self._toggle_reminder_3,
        )

        menu[self._flash_toggle.title] = self._flash_toggle
        menu[self._flash_dur_menu.title] = self._flash_dur_menu
        menu[self._reminder_2_toggle.title] = self._reminder_2_toggle
        menu[self._reminder_3_toggle.title] = self._reminder_3_toggle
        return menu

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

    @staticmethod
    def format_time(total_seconds: int) -> str:
        m, s = divmod(max(total_seconds, 0), 60)
        return f"{m:02d}:{s:02d}"

    # -- Core pomodoro cycle --------------------------------------------------

    def _start_next_session(self) -> None:
        """Alternate between work and break sessions automatically."""
        if self.on_break:
            rumps.notification("Pomodoro", "Break over!", "Time to focus 🍅")
            self.on_break = False
            self.seconds_left = self.work_mins * 60
        else:
            rumps.notification("Pomodoro", "Work session done!", "Take a break ☕")
            self.on_break = True
            self.seconds_left = self.break_mins * 60
        self.title = self.format_time(self.seconds_left)

    def _fire_flash(self) -> None:
        """Flash immediately, then schedule reminder flashes if enabled."""
        flash_screen_red(self.flash_secs)
        if self.flash_reminder_2:
            FoundationTimer.scheduledTimerWithTimeInterval_repeats_block_(
                REMINDER_INTERVAL, False, lambda _: flash_screen_red(self.flash_secs),
            )
        if self.flash_reminder_3:
            FoundationTimer.scheduledTimerWithTimeInterval_repeats_block_(
                REMINDER_INTERVAL * 2, False, lambda _: flash_screen_red(self.flash_secs),
            )

    def tick(self, _sender) -> None:
        self.seconds_left -= 1
        if self.seconds_left <= 0:
            self.timer.stop()
            if self.flash_enabled:
                self._fire_flash()
            self._start_next_session()
            self.timer.start()
        else:
            prefix = "☕ " if self.on_break else ""
            self.title = f"{prefix}{self.format_time(self.seconds_left)}"

    # -- Start / Pause / Resume / Stop state machine --------------------------

    def start(self, _sender) -> None:
        if self.state == TimerState.IDLE:
            self.on_break = False
            self.seconds_left = self.work_mins * 60
            self.title = self.format_time(self.seconds_left)
            self.timer.start()
            self.state = TimerState.RUNNING
            self.start_button.title = "Pause"
            self.stop_button.set_callback(self.stop)
        elif self.state == TimerState.RUNNING:
            self.timer.stop()
            self.state = TimerState.PAUSED
            self.start_button.title = "Resume"
        else:
            self.timer.start()
            self.state = TimerState.RUNNING
            self.start_button.title = "Pause"

    def stop(self, _sender) -> None:
        self.timer.stop()
        self.title = "🍅"
        self.on_break = False
        self.state = TimerState.IDLE
        self.start_button.title = "Start"
        self.stop_button.set_callback(None)

    # -- Settings callbacks ---------------------------------------------------

    def _set_duration(self, sender, menu: rumps.MenuItem, attr: str) -> None:
        mins = int(sender.title.split()[0])
        setattr(self, attr, mins)
        for item in menu.values():
            item.state = item.title == sender.title

    def set_work(self, sender) -> None:
        self._set_duration(sender, self.work_menu, "work_mins")

    def set_break(self, sender) -> None:
        self._set_duration(sender, self.break_menu, "break_mins")

    def _toggle_flash(self, sender) -> None:
        self.flash_enabled = not self.flash_enabled
        sender.state = self.flash_enabled

    def _set_flash_duration(self, sender) -> None:
        self.flash_secs = float(sender.title.split()[0])
        for item in self._flash_dur_menu.values():
            item.state = item.title == sender.title

    def _toggle_reminder_2(self, sender) -> None:
        self.flash_reminder_2 = not self.flash_reminder_2
        sender.state = self.flash_reminder_2

    def _toggle_reminder_3(self, sender) -> None:
        self.flash_reminder_3 = not self.flash_reminder_3
        sender.state = self.flash_reminder_3


# -- Screen flash overlay -----------------------------------------------------


def _create_overlay_window(frame) -> NSWindow:
    """Create a translucent red overlay window covering the given screen frame."""
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False,
    )
    win.setLevel_(NSFloatingWindowLevel + 1)
    win.setOpaque_(False)
    win.setIgnoresMouseEvents_(True)
    win.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(*FLASH_COLOR))
    win.setCollectionBehavior_(CAN_JOIN_ALL_SPACES | FULL_SCREEN_AUXILIARY)
    win.setContentView_(NSView.alloc().initWithFrame_(frame))
    win.orderFrontRegardless()
    return win


def flash_screen_red(duration: float = DEFAULT_FLASH_SECS) -> None:
    """Flash a translucent red overlay on all screens, then auto-dismiss."""
    windows = [_create_overlay_window(s.frame()) for s in NSScreen.screens()]

    def dismiss():
        for w in windows:
            w.orderOut_(None)

    FoundationTimer.scheduledTimerWithTimeInterval_repeats_block_(
        duration, False, lambda _: dismiss(),
    )


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
