# BPM to Chromatik

Detects BPM from an audio input device using aubio's tempo tracker and
streams it over OSC to [Chromatik](https://chromatik.co), so its lights
stay locked to whatever's actually playing instead of a manually-set
tempo. Built in the spirit of
[d00mfish/BPM-to-OSC](https://github.com/d00mfish/BPM-to-OSC) — same
underlying beat-detection algorithm (`aubio.tempo("default", ...)`), but a
plain Python script instead of a wxPython/PyAudio app, since those don't
build on modern Apple Silicon.

Two pieces:
- **`bpm_to_chromatik_gui.py`** — the actual tool. A small Tkinter control
  panel: pick an input, watch the BPM, tap in by hand when the algorithm
  struggles.
- **`bpm_to_chromatik.py`** — the shared engine (`BeatDetector`, `OSCSink`)
  the GUI is built on, also runnable standalone as a CLI if you just want
  audio-in-BPM-out without the UI.

## Why this exists

`aubio` (the PyPI package) is effectively unmaintained and won't build
from source against modern NumPy/Python on Apple Silicon. Homebrew's
`aubio` formula, however, builds and ships working Python bindings against
its own Python — so instead of fighting pip, this project's virtualenv is
built on **Homebrew's Python** with `--system-site-packages` so it can see
Homebrew's already-working `aubio` + `numpy`, while `sounddevice` and
`python-osc` (both pure-Python-friendly, no build issues) install normally
into the venv via [uv](https://docs.astral.sh/uv/).

That's also why `aubio` and `numpy` are deliberately missing from
`pyproject.toml`: listing them would make uv install PyPI copies into the
venv that shadow Homebrew's working ones. And `pyproject.toml` sets
`python-preference = "only-system"` so uv never swaps in one of its own
downloaded Pythons (which can't see Homebrew's packages).

## Setup

```bash
brew install aubio            # provides the C library + working Python bindings
brew install python-tk@3.14   # only needed for the GUI (Homebrew's Python doesn't bundle Tk)
uv venv --python /opt/homebrew/bin/python3.14 --system-site-packages
uv sync                       # installs sounddevice + python-osc from pyproject.toml
```

Verify aubio is visible from the venv:

```bash
uv run python -c "import aubio; print(aubio.version)"
```

If that ever fails with `No module named 'aubio'` (e.g. after a Homebrew
Python upgrade), delete `.venv` and re-run the `uv venv` + `uv sync` steps
above — a plain `uv sync` alone recreates the venv *without*
`--system-site-packages`.

## GUI

```bash
uv run python bpm_to_chromatik_gui.py
```

Pick an audio input from the dropdown, set the OSC host/port/addresses
(defaults are Chromatik's: `127.0.0.1:3030`, `/lx/tempo/bpm`,
`/lx/tempo/beat-within-bar`), hit Start.

It always sends two things for whichever source is currently driving:
- `/lx/tempo/bpm <float>` — the BPM number
- `/lx/tempo/beat-within-bar <int>` — a 1-indexed beat position (`1, 2, 3,
  4, 1, 2, 3...`) sent on *every* beat. Chromatik's OSC handler treats this
  address as both a beat trigger and a phase-lock signal in one message —
  this is what actually locks the lights to the music, the same way
  manually tapping does. The BPM value alone only sets the speed of
  Chromatik's clock; it doesn't say *where* the beat falls.

Expect ~1-2 seconds of silence after hitting Start before the first pulse —
aubio's tracker needs a few beats to lock onto the tempo, the same way
tap-tempo needs a couple of taps before it's dialed in.

### Two BPM sources, one driving at a time

The GUI shows both an **Algorithm BPM** (live from aubio) and a **Tapped
BPM** (from the TAP button) side by side, with a radio toggle for which one
is actually being sent over OSC. The non-driving source keeps updating its
display in the background so you can see whether it's recovered, but
nothing it detects gets sent until you switch to it.

### TAP button

For songs the algorithm struggles with (this is where gentle, non-
percussive material lives — the whole reason this exists instead of a
plain energy-threshold detector). Tapping in rhythm:

- computes BPM from the average interval between taps (a gap of more than
  2s starts a fresh tap sequence rather than averaging in a stray tap)
- each tap is itself sent as a beat immediately, so your first tap can
  double as the downbeat
- automatically switches the driving source to Tapped
- once you stop tapping, a background metronome keeps sending
  beat-within-bar pulses at the tapped tempo — otherwise Chromatik's OSC
  clock would stall the instant you stop tapping
- that hold lasts **5 minutes** of no further taps, then automatically
  reverts to Algorithm. Tap again anytime to re-establish tempo and reset
  the 5-minute window; or switch the radio back to Algorithm manually at
  any time (e.g. right after a song change).

### Downbeat

aubio only detects beat instants — it has no notion of which beat is "1";
that's a different, harder problem (musical meter estimation) that the
original BPM-to-OSC didn't solve either, it just had a manual "Resync Bar"
button. This does the same thing: hit **This is Beat 1** on the beat you
hear as the downbeat, and it sends beat-within-bar `1` immediately and
resets the counter (fixed at 4/4) so the next beats continue `2, 3, 4,
1...` from there. Works regardless of which source (Algorithm or Tapped)
is currently driving.

### Chromatik's Tempo clock source: set it to OSC, not Internal

This isn't optional — it's a hard gate in Chromatik's own `Tempo.java`:
beat and beat-within-bar messages are only ever processed when clock
source is OSC; in Internal mode that code path is skipped entirely, the
`trigger()` call that would apply them never even runs. `/lx/tempo/bpm`
alone (unconditional in either mode) only sets the clock's speed, not
where the beat falls, so phase-lock, the TAP button, and "This is Beat 1"
are all no-ops in Internal mode. The tradeoff: in OSC mode, the clock only
advances on a received beat message, so a dropped/late packet stalls the
lights until the next one arrives — that's the price of actual sync.

## CLI usage

For scripting, or targeting something other than Chromatik (the
`OSCSink`/`BeatDetector` classes are shared, but the CLI defaults to
Resolume's normalized tempo convention rather than Chromatik's):

```bash
uv run python bpm_to_chromatik.py --list-devices
uv run python bpm_to_chromatik.py --device "Samson GoMic" --host 127.0.0.1 --port 7000
```

`--device` matches a substring of the device name (case-insensitive) or an
exact index from `--list-devices`; omit it to use the system default
input.

To capture whatever is playing on your Mac rather than a live mic, install
a loopback device and select it the same way:

```bash
brew install blackhole-2ch
# Audio MIDI Setup -> create a Multi-Output Device with BlackHole + your
# speakers, set it as your system output, then:
uv run python bpm_to_chromatik.py --device "BlackHole"
```

### Flags

- `--host` / `--port` — OSC target (default `127.0.0.1:7000`)
- `--bpm-address` — OSC address for the BPM value (default: Resolume's tempo address)
- `--beat-address` — if set, sends a bang here on every detected beat (for phase-lock)
- `--raw` — send unnormalized BPM instead of Resolume's `(bpm-20)/480`
- `--min-bpm` / `--max-bpm` — sane-range clamp before sending (default `20`-`200`)
- `--buf-size` — aubio hop size in samples (default `128`, same as the original)
- `--quiet` — suppress the per-beat console line

Run `uv run python bpm_to_chromatik.py --help` for the full list.
