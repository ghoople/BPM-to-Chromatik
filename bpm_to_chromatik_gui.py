#!/usr/bin/env python3
"""
bpm_to_chromatik_gui.py - Tkinter control panel for BPM to Chromatik: pick
an audio input, set an OSC target, watch the live BPM, hit Start/Stop.

Run it directly (e.g. from a VS Code Python session):
    ./.venv/bin/python bpm_to_chromatik_gui.py
"""

import heapq
import itertools
import queue
import statistics
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox

import numpy as np
import sounddevice as sd

from bpm_to_chromatik import BeatDetector, OSCSink

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3030
DEFAULT_ADDRESS = "/lx/tempo/bpm"
DEFAULT_BEAT_IN_BAR_ADDRESS = "/lx/tempo/beat-within-bar"
SAMPLERATE = 44100
BUF_SIZE = 128
MIN_BPM = 20.0
MAX_BPM = 200.0
BEATS_PER_BAR = 4
TAP_SEQUENCE_GAP = 2.0       # seconds; a gap this big starts a fresh tap sequence
TAP_IDLE_TIMEOUT_MS = 5 * 60 * 1000  # revert to Algorithm after 5 min with no taps
MAX_DELAY_MS = 1000
CALIBRATION_TAPS = 8
CALIBRATION_MIN_BEATS = 4    # detected beats needed before a tap can be measured
CALIBRATION_EARLY_TOLERANCE = 0.05   # seconds; a tap this far ahead of the beat still counts
CALIBRATION_MAX_SPREAD = 0.04        # seconds; warn if tap offsets scatter more than this

ALGO_COLOR = "#2b7de0"
TAP_COLOR = "#e0432b"
MANUAL_COLOR = "#8a4fdb"
IDLE_COLOR = "#444444"


class DelayedSender:
    """Runs callables after a delay on a worker thread. A time-ordered heap
    (not a FIFO) so changing the delay live can't reorder messages."""

    def __init__(self):
        self._heap = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._closed = False
        threading.Thread(target=self._run, daemon=True).start()

    def schedule(self, delay_s, fn):
        with self._cond:
            if self._closed:
                return
            heapq.heappush(self._heap, (time.monotonic() + delay_s, next(self._seq), fn))
            self._cond.notify()

    def clear(self):
        with self._cond:
            self._heap.clear()

    def close(self):
        with self._cond:
            self._closed = True
            self._heap.clear()
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while not self._closed:
                    if not self._heap:
                        self._cond.wait()
                        continue
                    wait = self._heap[0][0] - time.monotonic()
                    if wait <= 0:
                        break
                    self._cond.wait(wait)
                if self._closed:
                    return
                _, _, fn = heapq.heappop(self._heap)
            try:
                fn()
            except Exception:
                pass  # a failed send must not kill the worker


def tap_offset(tap_time, beat_times, current_delay_s, max_delay_s=MAX_DELAY_MS / 1000):
    """How late (seconds) a tap landed after the detected beats, i.e. the
    playback lag. None if there aren't enough beats to tell.

    The nearest detected beat is only unambiguous modulo the beat period, so
    among the equivalent offsets (base + k * period) pick the plausible one
    closest to the delay currently set."""
    if len(beat_times) < CALIBRATION_MIN_BEATS:
        return None
    beats = list(beat_times)
    period = statistics.median(b - a for a, b in zip(beats, beats[1:]))
    if period <= 0:
        return None
    base = tap_time - min(beats, key=lambda b: abs(tap_time - b))
    candidates = [base + k * period for k in range(-3, 4)]
    plausible = [c for c in candidates
                 if -CALIBRATION_EARLY_TOLERANCE <= c <= max_delay_s]
    if not plausible:
        return base
    return min(plausible, key=lambda c: abs(c - current_delay_s))


def summarize_offsets(offsets):
    """(median, spread) of tap offsets; spread is the median absolute deviation."""
    median = statistics.median(offsets)
    spread = statistics.median(abs(o - median) for o in offsets)
    return median, spread


class App:
    def __init__(self, root):
        self.root = root
        root.title("BPM to Chromatik")
        root.resizable(False, False)

        self.stream = None
        self.osc = None
        self.events = queue.Queue()
        self.devices = []  # list of (index, name) for input-capable devices

        self.bar_position = 1  # 1-indexed position within the bar; 1 = downbeat
        self._bar_lock = threading.Lock()

        self.sender = None  # DelayedSender, alive while running
        self._delay_s = 0.0  # plain attr, read from the audio thread
        self.beat_times = deque(maxlen=16)  # detection times, audio thread -> GUI
        self._beat_lock = threading.Lock()
        self._calibrating = False
        self._calib_offsets = []
        self._calib_last_tap = None

        self._active_source = "algorithm"  # plain attr, read from background threads
        self.tap_times = deque(maxlen=8)
        self.tapped_bpm = None
        self._metronome_stop = None
        self._tap_timeout_id = None

        self._build_widgets()
        self._refresh_devices()
        self._poll_events()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI ----------------------------------------------------------

    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}

        ttk.Label(self.root, text="BPM to Chromatik",
                  font=("Helvetica", 14, "bold")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 0))

        audio_frame = ttk.LabelFrame(self.root, text="Audio Input")
        audio_frame.grid(row=1, column=0, sticky="ew", **pad)
        audio_frame.columnconfigure(0, weight=1)

        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(audio_frame, textvariable=self.device_var,
                                          state="readonly", width=40)
        self.device_combo.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=6)
        ttk.Button(audio_frame, text="Refresh", command=self._refresh_devices).grid(
            row=0, column=1, padx=(0, 8), pady=6)

        osc_frame = ttk.LabelFrame(self.root, text="OSC Target")
        osc_frame.grid(row=2, column=0, sticky="ew", **pad)
        osc_frame.columnconfigure(1, weight=1)

        ttk.Label(osc_frame, text="Host").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        ttk.Entry(osc_frame, textvariable=self.host_var).grid(
            row=0, column=1, sticky="ew", padx=(0, 8), pady=4)

        ttk.Label(osc_frame, text="Port").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        ttk.Entry(osc_frame, textvariable=self.port_var).grid(
            row=1, column=1, sticky="ew", padx=(0, 8), pady=4)

        ttk.Label(osc_frame, text="Address").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        self.address_var = tk.StringVar(value=DEFAULT_ADDRESS)
        ttk.Entry(osc_frame, textvariable=self.address_var).grid(
            row=2, column=1, sticky="ew", padx=(0, 8), pady=4)

        ttk.Label(osc_frame, text="Beat-in-Bar Address").grid(
            row=3, column=0, sticky="w", padx=8, pady=(0, 6))
        self.beat_in_bar_address_var = tk.StringVar(value=DEFAULT_BEAT_IN_BAR_ADDRESS)
        ttk.Entry(osc_frame, textvariable=self.beat_in_bar_address_var).grid(
            row=3, column=1, sticky="ew", padx=(0, 8), pady=(0, 6))

        bpm_frame = ttk.Frame(self.root)
        bpm_frame.grid(row=3, column=0, sticky="ew", **pad)
        bpm_frame.columnconfigure(0, weight=1)
        bpm_frame.columnconfigure(1, weight=1)

        self.algo_bpm_var = tk.StringVar(value="--")
        ttk.Label(bpm_frame, textvariable=self.algo_bpm_var,
                  font=("Helvetica", 36, "bold"), anchor="center").grid(
            row=0, column=0, sticky="ew")
        ttk.Label(bpm_frame, text="Algorithm BPM", anchor="center").grid(
            row=1, column=0, sticky="ew")

        self.tapped_bpm_var = tk.StringVar(value="--")
        ttk.Label(bpm_frame, textvariable=self.tapped_bpm_var,
                  font=("Helvetica", 36, "bold"), anchor="center").grid(
            row=0, column=1, sticky="ew")
        ttk.Label(bpm_frame, text="Tapped BPM", anchor="center").grid(
            row=1, column=1, sticky="ew")

        self.beat_dot = tk.Canvas(bpm_frame, width=20, height=20, highlightthickness=0)
        self.beat_dot.grid(row=0, column=2, padx=(8, 0))
        self._dot_id = self.beat_dot.create_oval(2, 2, 18, 18, fill=IDLE_COLOR, outline="")

        source_frame = ttk.LabelFrame(self.root, text="Driving Source")
        source_frame.grid(row=4, column=0, sticky="ew", **pad)
        source_frame.columnconfigure(2, weight=1)

        self.active_source_var = tk.StringVar(value="algorithm")
        self.active_source_var.trace_add("write", self._on_source_change)

        self.algo_radio = ttk.Radiobutton(source_frame, text="Algorithm",
                                           variable=self.active_source_var,
                                           value="algorithm", state="disabled")
        self.algo_radio.grid(row=0, column=0, sticky="w", padx=8, pady=6)

        self.tapped_radio = ttk.Radiobutton(source_frame, text="Tapped",
                                             variable=self.active_source_var,
                                             value="tapped", state="disabled")
        self.tapped_radio.grid(row=0, column=1, sticky="w", padx=8, pady=6)

        self.tap_button = ttk.Button(source_frame, text="TAP",
                                      command=self._tap, state="disabled")
        self.tap_button.grid(row=0, column=2, sticky="e", padx=8, pady=6)

        bar_frame = ttk.Frame(self.root)
        bar_frame.grid(row=5, column=0, sticky="ew", **pad)
        bar_frame.columnconfigure(0, weight=1)

        self.bar_position_var = tk.StringVar(value=f"Beat -- of {BEATS_PER_BAR}")
        ttk.Label(bar_frame, textvariable=self.bar_position_var).grid(row=0, column=0, sticky="w")

        self.mark_beat_button = ttk.Button(bar_frame, text="This is Beat 1",
                                            command=self._mark_beat_one, state="disabled")
        self.mark_beat_button.grid(row=0, column=1, sticky="e")

        delay_frame = ttk.LabelFrame(self.root, text="Sync Delay (Algorithm only)")
        delay_frame.grid(row=6, column=0, sticky="ew", **pad)
        delay_frame.columnconfigure(3, weight=1)

        ttk.Label(delay_frame, text="Delay (ms)").grid(row=0, column=0, padx=8, pady=6)
        self.delay_var = tk.StringVar(value="0")
        self.delay_var.trace_add("write", self._on_delay_change)
        ttk.Spinbox(delay_frame, from_=0, to=MAX_DELAY_MS, increment=5, width=6,
                    textvariable=self.delay_var).grid(row=0, column=1, padx=(0, 8), pady=6)

        self.calibrate_button = ttk.Button(delay_frame, text="Calibrate",
                                            command=self._toggle_calibration, state="disabled")
        self.calibrate_button.grid(row=0, column=2, padx=(0, 8), pady=6)

        self.calib_var = tk.StringVar(value="")
        ttk.Label(delay_frame, textvariable=self.calib_var).grid(
            row=0, column=3, sticky="w", padx=(0, 8))

        control_frame = ttk.Frame(self.root)
        control_frame.grid(row=7, column=0, sticky="ew", **pad)
        control_frame.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(control_frame, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

        self.start_button = ttk.Button(control_frame, text="Start", command=self._toggle)
        self.start_button.grid(row=0, column=1, sticky="e")

    def _refresh_devices(self):
        self.devices = [
            (idx, dev["name"]) for idx, dev in enumerate(sd.query_devices())
            if dev["max_input_channels"] > 0
        ]
        names = [name for _, name in self.devices]
        self.device_combo["values"] = names
        if names and not self.device_var.get():
            try:
                default_idx = sd.default.device[0]
                default_name = next((n for i, n in self.devices if i == default_idx), names[0])
            except Exception:
                default_name = names[0]
            self.device_var.set(default_name)

    # ---- audio + OSC ---------------------------------------------------

    def _selected_device_index(self):
        name = self.device_var.get()
        for idx, dev_name in self.devices:
            if dev_name == name:
                return idx
        return None

    def _toggle(self):
        if self.stream is None:
            self._start()
        else:
            self._stop()

    def _start(self):
        device = self._selected_device_index()
        if device is None:
            messagebox.showerror("BPM to Chromatik", "Pick an audio input device first.")
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("BPM to Chromatik", "Port must be a number.")
            return
        host = self.host_var.get().strip()
        address = self.address_var.get().strip()
        if not host or not address:
            messagebox.showerror("BPM to Chromatik", "Host and address are required.")
            return
        beat_in_bar_address = self.beat_in_bar_address_var.get().strip()
        if not beat_in_bar_address:
            messagebox.showerror("BPM to Chromatik", "Beat-in-bar address is required.")
            return

        detector = BeatDetector(samplerate=SAMPLERATE, buf_size=BUF_SIZE)
        self.bar_position = 1
        with self._beat_lock:
            self.beat_times.clear()
        self._calibrating = False
        self.tap_times.clear()
        self.tapped_bpm = None
        self.algo_bpm_var.set("--")
        self.tapped_bpm_var.set("--")
        self._active_source = "algorithm"
        self.active_source_var.set("algorithm")  # triggers _on_source_change (safe, osc unset)

        self.osc = OSCSink(host, port, address, beat_address=None,
                            beat_in_bar_address=beat_in_bar_address,
                            raw=True, min_bpm=MIN_BPM, max_bpm=MAX_BPM)
        sender = self.sender = DelayedSender()

        def callback(indata, frames, time_info, status):
            mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
            mono = np.ascontiguousarray(mono, dtype=np.float32)
            beat, bpm = detector.process(mono)
            if not beat:
                return
            # recorded before any delay so calibration is independent of it
            with self._beat_lock:
                self.beat_times.append(time.monotonic())
            if self._active_source == "algorithm":
                with self._bar_lock:
                    position = self.bar_position
                    self.bar_position = self.bar_position % BEATS_PER_BAR + 1

                def emit(bpm=bpm, position=position):
                    osc = self.osc
                    # Tapped may have taken over while this beat was waiting
                    if osc is None or self._active_source != "algorithm":
                        return
                    osc.send_beat_in_bar(position)
                    if MIN_BPM < bpm < MAX_BPM:
                        osc.send_bpm(bpm)
                    self.events.put(("algorithm", bpm, position))

                sender.schedule(self._delay_s, emit)
            else:
                # still report the algorithm's BPM for comparison, but don't
                # touch the shared bar counter or send anything - Tapped is driving
                self.events.put(("algorithm", bpm, None))

        try:
            self.stream = sd.InputStream(device=device, channels=1, samplerate=SAMPLERATE,
                                          blocksize=BUF_SIZE, dtype="float32",
                                          callback=callback)
            self.stream.start()
        except Exception as exc:
            self.stream = None
            self.osc = None
            messagebox.showerror("BPM to Chromatik", f"Couldn't open input device:\n{exc}")
            return

        self.device_combo.state(["disabled"])
        self.start_button.config(text="Stop")
        self.mark_beat_button.state(["!disabled"])
        self.algo_radio.state(["!disabled"])
        self.tapped_radio.state(["!disabled"])
        self.tap_button.state(["!disabled"])
        self.calibrate_button.state(["!disabled"])
        self.calib_var.set("")
        self.bar_position_var.set(f"Beat -- of {BEATS_PER_BAR}")
        self.status_var.set(f"Running -> {host}:{port}{address}")

    def _stop(self):
        self._stop_metronome()
        self._cancel_tap_timeout()
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if self.sender is not None:
            self.sender.close()
            self.sender = None
        self.osc = None
        self._end_calibration()
        self.calibrate_button.state(["disabled"])
        self.device_combo.state(["!disabled"])
        self.start_button.config(text="Start")
        self.mark_beat_button.state(["disabled"])
        self.algo_radio.state(["disabled"])
        self.tapped_radio.state(["disabled"])
        self.tap_button.state(["disabled"])
        self.status_var.set("Stopped")
        self.algo_bpm_var.set("--")
        self.tapped_bpm_var.set("--")
        self.bar_position_var.set(f"Beat -- of {BEATS_PER_BAR}")
        self._flash_dot(IDLE_COLOR, hold=False)
        self._active_source = "algorithm"
        self.active_source_var.set("algorithm")

    def _mark_beat_one(self):
        if self.osc is None:
            return
        if self.sender is not None:
            self.sender.clear()  # drop delayed beats carrying the old bar position
        with self._bar_lock:
            self.bar_position = 2
        self.osc.send_beat_in_bar(1)
        self.bar_position_var.set(f"Beat 1 of {BEATS_PER_BAR}")
        self._flash_dot(MANUAL_COLOR)

    # ---- sync delay ------------------------------------------------------

    def _on_delay_change(self, *_args):
        try:
            ms = float(self.delay_var.get())
        except ValueError:
            return  # half-typed value; keep the last good delay
        self._delay_s = min(max(ms, 0.0), MAX_DELAY_MS) / 1000.0

    def _toggle_calibration(self):
        if self._calibrating:
            self._end_calibration()
            self.calib_var.set("Calibration cancelled")
        elif self.osc is not None:
            self._calibrating = True
            self._calib_offsets = []
            self._calib_last_tap = None
            self.calibrate_button.config(text="Cancel")
            self.calib_var.set(f"Tap along with the speakers: 0/{CALIBRATION_TAPS}")

    def _end_calibration(self):
        self._calibrating = False
        self.calibrate_button.config(text="Calibrate")

    def _calibration_tap(self, now):
        if self._calib_last_tap is not None and now - self._calib_last_tap > TAP_SEQUENCE_GAP:
            self._calib_offsets = []  # long pause: start the run over
        self._calib_last_tap = now
        with self._beat_lock:
            beats = list(self.beat_times)
        offset = tap_offset(now, beats, self._delay_s)
        if offset is None:
            self.calib_var.set("Not enough beats detected yet - keep tapping")
            return
        self._calib_offsets.append(offset)
        if len(self._calib_offsets) < CALIBRATION_TAPS:
            self.calib_var.set(
                f"Tap along with the speakers: {len(self._calib_offsets)}/{CALIBRATION_TAPS}")
            return
        median, spread = summarize_offsets(self._calib_offsets)
        self._end_calibration()
        ms = int(round(min(max(median, 0.0), MAX_DELAY_MS / 1000) * 1000))
        self.delay_var.set(str(ms))
        message = f"Delay set to {ms} ms (\u00b1{spread * 1000:.0f})"
        if median < 0:
            message = f"Taps were ahead of the beat; delay set to 0 ms (\u00b1{spread * 1000:.0f})"
        elif spread > CALIBRATION_MAX_SPREAD:
            message += " - taps were scattered, consider retrying"
        self.calib_var.set(message)

    # ---- tap tempo -------------------------------------------------------

    def _tap(self):
        if self.osc is None:
            return
        now = time.monotonic()
        if self._calibrating:
            self._calibration_tap(now)
            return
        if self.tap_times and (now - self.tap_times[-1]) > TAP_SEQUENCE_GAP:
            self.tap_times.clear()
        self.tap_times.append(now)

        if len(self.tap_times) >= 2:
            times = list(self.tap_times)
            intervals = [b - a for a, b in zip(times, times[1:])]
            avg_interval = sum(intervals) / len(intervals)
            self.tapped_bpm = 60.0 / avg_interval
            self.tapped_bpm_var.set(f"{self.tapped_bpm:.1f}")

        # the tap itself is a beat, sent immediately regardless of who was
        # previously driving
        with self._bar_lock:
            position = self.bar_position
            self.bar_position = self.bar_position % BEATS_PER_BAR + 1
        self.osc.send_beat_in_bar(position)
        if self.tapped_bpm is not None:
            self.osc.send_bpm(self.tapped_bpm)
        self.bar_position_var.set(f"Beat {position} of {BEATS_PER_BAR}")
        self._flash_dot(TAP_COLOR)

        # switch to Tapped (triggers _on_source_change: starts the metronome,
        # (re)starts the 5-minute idle timeout) even if already selected -
        # a Tk write-trace fires on every set(), so repeated taps keep
        # restarting the metronome at the freshly-tapped tempo.
        self.active_source_var.set("tapped")

    def _on_source_change(self, *_args):
        source = self.active_source_var.get()
        self._active_source = source
        if source == "tapped":
            if self.sender is not None:
                self.sender.clear()
            self._start_metronome()
            self._reset_tap_timeout()
        else:
            self._stop_metronome()
            self._cancel_tap_timeout()

    def _start_metronome(self):
        self._stop_metronome()
        if self.tapped_bpm is None or self.osc is None:
            return
        interval = 60.0 / self.tapped_bpm
        stop_event = threading.Event()
        self._metronome_stop = stop_event

        def loop():
            while not stop_event.wait(interval):
                if self._active_source != "tapped" or self.osc is None:
                    continue
                with self._bar_lock:
                    position = self.bar_position
                    self.bar_position = self.bar_position % BEATS_PER_BAR + 1
                self.osc.send_beat_in_bar(position)
                self.osc.send_bpm(self.tapped_bpm)
                self.events.put(("tapped", self.tapped_bpm, position))

        threading.Thread(target=loop, daemon=True).start()

    def _stop_metronome(self):
        if self._metronome_stop is not None:
            self._metronome_stop.set()
            self._metronome_stop = None

    def _reset_tap_timeout(self):
        self._cancel_tap_timeout()
        self._tap_timeout_id = self.root.after(TAP_IDLE_TIMEOUT_MS, self._tap_timeout_expired)

    def _cancel_tap_timeout(self):
        if self._tap_timeout_id is not None:
            self.root.after_cancel(self._tap_timeout_id)
            self._tap_timeout_id = None

    def _tap_timeout_expired(self):
        self._tap_timeout_id = None
        self.active_source_var.set("algorithm")

    # ---- GUI-thread polling ---------------------------------------------

    def _flash_dot(self, color, hold=True):
        self.beat_dot.itemconfig(self._dot_id, fill=color)
        if hold:
            self.root.after(120, lambda: self.beat_dot.itemconfig(self._dot_id, fill=IDLE_COLOR))

    def _poll_events(self):
        events = []
        try:
            while True:
                events.append(self.events.get_nowait())
        except queue.Empty:
            pass

        for source, bpm, position in events:
            if bpm is not None and MIN_BPM < bpm < MAX_BPM:
                if source == "algorithm":
                    self.algo_bpm_var.set(f"{bpm:.1f}")
                else:
                    self.tapped_bpm_var.set(f"{bpm:.1f}")
            if position is not None:
                self.bar_position_var.set(f"Beat {position} of {BEATS_PER_BAR}")
                self._flash_dot(ALGO_COLOR if source == "algorithm" else TAP_COLOR)

        self.root.after(50, self._poll_events)

    def _on_close(self):
        self._stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
