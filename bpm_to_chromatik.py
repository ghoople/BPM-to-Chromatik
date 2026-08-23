#!/usr/bin/env python3
"""
bpm_to_chromatik.py - Detect BPM from an audio input device using aubio's
tempo tracker and stream it over OSC in real time. This is the shared
engine (BeatDetector, OSCSink) used by bpm_to_chromatik_gui.py, also usable
standalone as a CLI - the address/normalization flags below default to
Resolume's convention but work with any OSC receiver, Chromatik included.

This replicates the core signal chain of d00mfish/BPM-to-OSC (audio in ->
aubio.tempo("default", ...) -> OSC out) without the wxPython/PyAudio GUI,
as a plain CLI script.

Sends, by default:
  <bpm-address>   <float 0-1>   normalized BPM, (bpm - 20) / 480, matching
                                 Resolume's tempo parameter range (20-500 BPM)
Optionally also:
  <beat-address>  (bang)        sent on every detected beat, for phase-lock

Dependencies: sounddevice, python-osc, numpy, aubio
aubio must come from Homebrew (`brew install aubio`), not pip - see README.
"""

import argparse
import sys
import time

import numpy as np
import sounddevice as sd
from aubio import tempo
from pythonosc.udp_client import SimpleUDPClient


def list_devices():
    print(sd.query_devices())


def resolve_device(name_or_index):
    if name_or_index is None:
        return None
    try:
        return int(name_or_index)
    except ValueError:
        pass
    needle = name_or_index.lower()
    for idx, dev in enumerate(sd.query_devices()):
        if needle in dev["name"].lower() and dev["max_input_channels"] > 0:
            return idx
    raise SystemExit(
        f"No input-capable device matching '{name_or_index}' found. "
        f"Run with --list-devices to see options."
    )


class OSCSink:
    """Mirrors osc_client.py's Resolume normalization behavior."""

    def __init__(self, host, port, bpm_address, beat_address, raw, min_bpm, max_bpm,
                 beat_in_bar_address=None):
        self.client = SimpleUDPClient(host, port)
        self.bpm_address = bpm_address
        self.beat_address = beat_address
        self.beat_in_bar_address = beat_in_bar_address
        self.raw = raw
        self.min_bpm = min_bpm
        self.max_bpm = max_bpm

    def send_bpm(self, bpm):
        if not (self.min_bpm < bpm < self.max_bpm):
            return
        if self.raw:
            self.client.send_message(self.bpm_address, float(bpm))
        else:
            normalized = (float(bpm) - 20.0) / 480.0
            self.client.send_message(self.bpm_address, normalized)

    def send_beat(self):
        if self.beat_address:
            self.client.send_message(self.beat_address, 1)

    def send_beat_in_bar(self, position):
        """position: 1-indexed beat number within the bar (1 = downbeat).
        Chromatik's /lx/tempo/beat-within-bar both triggers the beat and
        sets its position, so this can replace send_beat() entirely."""
        if self.beat_in_bar_address:
            self.client.send_message(self.beat_in_bar_address, int(position))


class BeatDetector:
    """Thin wrapper around aubio's tempo tracker - same algorithm and default
    buffer/hop sizes as the original BPM-to-OSC's beatfinder.py."""

    def __init__(self, samplerate=44100, buf_size=128):
        self.samplerate = samplerate
        self.buf_size = buf_size
        self.tempo = tempo("default", buf_size * 2, buf_size, samplerate)

    def process(self, mono_block):
        """mono_block: 1-D float32 numpy array of length buf_size.
        Returns (beat_detected: bool, bpm: float)."""
        is_beat = self.tempo(mono_block)[0]
        return bool(is_beat), self.tempo.get_bpm()


SPINNER = "▶◀"


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", help="Input device name substring or index "
                                     "(default: system default input)")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--host", default="127.0.0.1", help="OSC target host (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=7000, help="OSC target port (default: 7000)")
    p.add_argument("--bpm-address", default="/composition/tempocontroller/tempo",
                    help="OSC address for the BPM value (default: Resolume's tempo address)")
    p.add_argument("--beat-address", default=None,
                    help="OSC address to bang on every detected beat (default: none)")
    p.add_argument("--raw", action="store_true",
                    help="Send raw BPM instead of Resolume's normalized (bpm-20)/480 range")
    p.add_argument("--min-bpm", type=float, default=20.0)
    p.add_argument("--max-bpm", type=float, default=200.0)
    p.add_argument("--samplerate", type=int, default=44100)
    p.add_argument("--buf-size", type=int, default=128, help="aubio hop size (default: 128)")
    p.add_argument("--quiet", action="store_true", help="Don't print BPM to the console")
    args = p.parse_args()

    if args.list_devices:
        list_devices()
        return

    device = resolve_device(args.device)
    dev_info = sd.query_devices(device, "input")
    print(f"Capturing: {dev_info['name']} @ {args.samplerate} Hz")
    print(f"Sending OSC to {args.host}:{args.port}  bpm -> {args.bpm_address}"
          + (f"  beat -> {args.beat_address}" if args.beat_address else ""))
    print("Ctrl+C to stop.\n")

    detector = BeatDetector(samplerate=args.samplerate, buf_size=args.buf_size)
    osc = OSCSink(args.host, args.port, args.bpm_address, args.beat_address,
                  args.raw, args.min_bpm, args.max_bpm)

    spin_state = 0

    def callback(indata, frames, time_info, status):
        nonlocal spin_state
        if status:
            print(status, file=sys.stderr)
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        mono = np.ascontiguousarray(mono, dtype=np.float32)

        beat, bpm = detector.process(mono)
        if beat:
            osc.send_beat()
            if args.min_bpm < bpm < args.max_bpm:
                osc.send_bpm(bpm)
                if not args.quiet:
                    spin_state = (spin_state + 1) % len(SPINNER)
                    print(f"{SPINNER[spin_state]}\t{bpm:.1f} BPM", flush=True)

    try:
        with sd.InputStream(device=device, channels=1, samplerate=args.samplerate,
                             blocksize=args.buf_size, dtype="float32",
                             callback=callback):
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
