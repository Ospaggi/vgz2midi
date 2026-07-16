#!/usr/bin/env python3
"""
VGZ/VGM to Standard MIDI File converter.

The converter extracts note timing and pitch from common VGM sound-chip
register writes. It does not emulate the original chip timbre or PCM samples,
so the generated MIDI is intended for editing, analysis, and arrangement.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import math
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SAMPLE_RATE = 44_100
DEFAULT_BPM = 120.0
MIN_DETECTED_BPM = 40.0
MAX_DETECTED_BPM = 240.0
DEFAULT_PPQN = 480
DEFAULT_PITCH_BEND_RANGE = 2
DEFAULT_MIDI_VOLUME = 100
MAX_PITCH_BEND_RANGE = 24
PITCH_CONTINUITY_WINDOW_SAMPLES = int(SAMPLE_RATE * 0.060)
PITCH_FINE_STEP_SEMITONES = 0.75
PITCH_FAST_STEP_SEMITONES = 2.0
SUPPORTED_EXTENSIONS = {".vgm", ".vgz"}


def u16le(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        return 0
    return struct.unpack_from("<H", data, offset)[0]


def u32le(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        return 0
    return struct.unpack_from("<I", data, offset)[0]


def clean_clock(value: int, default: int) -> int:
    # Bits 30 and 31 can be dual-chip/variant flags.
    value &= 0x3FFFFFFF
    return value or default


def frequency_to_midi_pitch(freq: float) -> Optional[float]:
    """Convert a frequency to a fractional MIDI note number."""
    if not math.isfinite(freq) or freq <= 0.0:
        return None
    pitch = 69.0 + 12.0 * math.log2(freq / 440.0)
    if -12.0 <= pitch <= 139.0:
        return pitch
    return None


def frequency_to_midi_note(freq: float) -> Optional[int]:
    pitch = frequency_to_midi_pitch(freq)
    if pitch is None:
        return None
    note = int(round(pitch))
    return note if 0 <= note <= 127 else None


def midi_note_frequency(note: float) -> float:
    return 440.0 * (2.0 ** ((note - 69.0) / 12.0))


def encode_vlq(value: int) -> bytes:
    value = max(0, int(value))
    buffer = value & 0x7F
    result = bytearray([buffer])
    while value >> 7:
        value >>= 7
        buffer = (value & 0x7F) | 0x80
        result.insert(0, buffer)
    return bytes(result)


def meta_event(meta_type: int, payload: bytes) -> bytes:
    return bytes([0xFF, meta_type]) + encode_vlq(len(payload)) + payload


def safe_text(value: str, limit: int = 127) -> bytes:
    return value.encode("utf-8", errors="replace")[:limit]


@dataclass
class BPMDetection:
    bpm: float
    confidence: float
    method: str


def _merge_onsets(events: Iterable["TimedMidiEvent"]) -> List[Tuple[int, int]]:
    """Return near-simultaneous note-on events as (sample, strength) groups."""
    raw: List[int] = []
    for event in events:
        if len(event.data) >= 3 and (event.data[0] & 0xF0) == 0x90 and event.data[2] > 0:
            raw.append(max(0, event.sample))
    if not raw:
        return []

    raw.sort()
    merge_window = int(SAMPLE_RATE * 0.010)  # Chord register writes can be a few milliseconds apart.
    merged: List[Tuple[int, int]] = []
    group_start = raw[0]
    group_sum = raw[0]
    group_count = 1
    for sample in raw[1:]:
        if sample - group_start <= merge_window:
            group_sum += sample
            group_count += 1
        else:
            merged.append((int(round(group_sum / group_count)), group_count))
            group_start = sample
            group_sum = sample
            group_count = 1
    merged.append((int(round(group_sum / group_count)), group_count))
    return merged


def _nearest_onset_match(samples: Sequence[int], target: float) -> Tuple[float, int]:
    index = bisect.bisect_left(samples, target)
    candidates: List[Tuple[float, int]] = []
    if index < len(samples):
        candidates.append((abs(samples[index] - target), index))
    if index > 0:
        candidates.append((abs(samples[index - 1] - target), index - 1))
    return min(candidates) if candidates else (float("inf"), -1)


def detect_bpm_from_events(
    tracks: Iterable["MidiTrackState"],
    loop_samples: int = 0,
) -> BPMDetection:
    """Estimate musical tempo from extracted note onsets.

    VGM does not normally store BPM. This heuristic combines an onset-pair tempo
    histogram, beat-period matching, and loop-length bar fitting. The result is
    intentionally reported with a confidence value because half/double-tempo
    ambiguity cannot always be resolved from register writes alone.
    """
    all_events = [event for track in tracks for event in track.events]
    merged = _merge_onsets(all_events)
    if len(merged) < 6:
        return BPMDetection(DEFAULT_BPM, 0.0, "fallback: insufficient note onsets")

    # Keep runtime bounded for long captures while preserving the beginning and end.
    max_onsets = 2400
    if len(merged) > max_onsets:
        step = (len(merged) - 1) / float(max_onsets - 1)
        merged = [merged[int(round(i * step))] for i in range(max_onsets)]

    samples = [sample for sample, _ in merged]
    strengths = [strength for _, strength in merged]
    resolution = 0.5
    bin_count = int(round((MAX_DETECTED_BPM - MIN_DETECTED_BPM) / resolution)) + 1
    histogram = [0.0] * bin_count

    # Pair distances vote for plausible beat counts. Nearby pairs are more reliable.
    beat_hypotheses = (
        (0.25, 0.20),
        (1.0 / 3.0, 0.16),
        (0.5, 0.48),
        (2.0 / 3.0, 0.22),
        (0.75, 0.18),
        (1.0, 1.00),
        (1.5, 0.32),
        (2.0, 0.72),
        (3.0, 0.34),
        (4.0, 0.46),
        (6.0, 0.20),
        (8.0, 0.14),
    )
    max_pair_seconds = 8.0
    for i, start in enumerate(samples):
        for j in range(i + 1, min(len(samples), i + 17)):
            delta_seconds = (samples[j] - start) / SAMPLE_RATE
            if delta_seconds <= 0.025:
                continue
            if delta_seconds > max_pair_seconds:
                break
            pair_weight = math.sqrt(strengths[i] * strengths[j]) / math.sqrt(j - i)
            for beats, hypothesis_weight in beat_hypotheses:
                bpm = 60.0 * beats / delta_seconds
                if MIN_DETECTED_BPM <= bpm <= MAX_DETECTED_BPM:
                    center = int(round((bpm - MIN_DETECTED_BPM) / resolution))
                    vote = pair_weight * hypothesis_weight
                    for offset, smooth in ((-2, 0.12), (-1, 0.55), (0, 1.0), (1, 0.55), (2, 0.12)):
                        index = center + offset
                        if 0 <= index < bin_count:
                            histogram[index] += vote * smooth

    max_hist = max(histogram) or 1.0

    def periodicity_score(bpm: float) -> float:
        beat = SAMPLE_RATE * 60.0 / bpm
        tolerance = max(SAMPLE_RATE * 0.018, beat * 0.055)
        total = 0.0
        matched = 0.0
        # Sample at most 800 anchors to avoid quadratic behavior.
        anchor_step = max(1, len(samples) // 800)
        for i in range(0, len(samples), anchor_step):
            base_weight = math.sqrt(strengths[i])
            for multiple, weight in ((0.5, 0.30), (1.0, 1.00), (2.0, 0.62), (4.0, 0.25)):
                target = samples[i] + beat * multiple
                if target > samples[-1]:
                    continue
                total += base_weight * weight
                distance, matched_index = _nearest_onset_match(samples, target)
                if distance <= tolerance and matched_index >= 0:
                    strength_similarity = min(strengths[i], strengths[matched_index]) / max(
                        strengths[i], strengths[matched_index]
                    )
                    accent_factor = 0.45 + 0.55 * strength_similarity
                    matched += (
                        base_weight
                        * weight
                        * (1.0 - distance / tolerance)
                        * accent_factor
                    )
        return matched / total if total else 0.0

    def loop_fit_score(bpm: float) -> float:
        if loop_samples <= 0:
            return 0.0
        beats = loop_samples * bpm / (SAMPLE_RATE * 60.0)
        if beats < 2.0:
            return 0.0
        # Most looped game tracks span a whole number of 4/4 bars.
        bars = beats / 4.0
        error = abs(bars - round(bars))
        return max(0.0, 1.0 - error / 0.18)

    candidates: List[Tuple[float, float]] = []
    for index, hist_value in enumerate(histogram):
        bpm = MIN_DETECTED_BPM + index * resolution
        hist_score = hist_value / max_hist
        period_score = periodicity_score(bpm)
        loop_score = loop_fit_score(bpm)
        # Mild preference for the conventional 70-180 BPM range resolves many
        # half/double-tempo ties without blocking genuinely slow or fast songs.
        if 70.0 <= bpm <= 180.0:
            range_prior = 1.0
        else:
            distance = min(abs(bpm - 70.0), abs(bpm - 180.0))
            range_prior = max(0.0, 1.0 - distance / 80.0)
        score = 0.58 * hist_score + 0.30 * period_score + 0.08 * loop_score + 0.04 * range_prior
        candidates.append((score, bpm))

    candidates.sort(reverse=True)
    best_score, best_bpm = candidates[0]

    # Prefer an in-range harmonic when its evidence is almost as strong.
    harmonic_options = [best_bpm / 2.0, best_bpm * 2.0]
    score_by_bpm = {round(bpm, 1): score for score, bpm in candidates}
    for option in harmonic_options:
        option = round(option * 2.0) / 2.0
        if 70.0 <= option <= 180.0:
            option_score = score_by_bpm.get(round(option, 1), 0.0)
            if option_score >= best_score * 0.93:
                best_bpm = option
                best_score = option_score
                break

    second_score = next(
        (score for score, bpm in candidates if abs(bpm - best_bpm) >= 3.0),
        0.0,
    )
    separation = max(0.0, (best_score - second_score) / max(best_score, 1e-9))
    event_factor = min(1.0, (len(merged) - 5) / 75.0)
    confidence = max(0.0, min(1.0, (0.55 * best_score + 0.45 * separation) * event_factor))

    return BPMDetection(round(best_bpm, 1), confidence, "automatic onset/loop analysis")


@dataclass(order=True)
class TimedMidiEvent:
    sample: int
    priority: int
    serial: int
    data: bytes = field(compare=False)


@dataclass
class MidiTrackState:
    key: str
    name: str
    channel: int
    port: int
    program: int
    is_drum: bool
    events: List[TimedMidiEvent] = field(default_factory=list)
    active_note: Optional[int] = None
    active_pitch: Optional[float] = None
    active_since: int = 0
    pitch_bend: int = 8192
    controllers: Dict[int, int] = field(default_factory=dict)
    last_pitch_sample: Optional[int] = None
    pitch_group_sample: Optional[int] = None
    pitch_group_serial: int = 0
    pitch_group_active_note: Optional[int] = None
    pitch_group_active_pitch: Optional[float] = None
    pitch_group_active_since: int = 0
    pitch_group_pitch_bend: int = 8192
    pitch_group_last_pitch_sample: Optional[int] = None


class MidiCollector:
    """Collect register-derived musical events and serialize them as SMF type 1."""

    PRIORITY_CONTROL = 0
    PRIORITY_NOTE_OFF = 1
    PRIORITY_PITCH = 2
    PRIORITY_NOTE_ON = 3
    PRIORITY_RESET = 2

    def __init__(self, source_name: str, bpm: float = DEFAULT_BPM, ppqn: int = DEFAULT_PPQN, pitch_bend_range: int = DEFAULT_PITCH_BEND_RANGE, midi_volume: int = DEFAULT_MIDI_VOLUME):
        self.source_name = source_name
        self.bpm = max(1.0, min(float(bpm), 999.0))
        self.ppqn = int(ppqn)
        self.pitch_bend_range = max(1, min(MAX_PITCH_BEND_RANGE, int(pitch_bend_range)))
        self.midi_volume = max(0, min(127, int(midi_volume)))
        self.tempo_description = "manual/default"
        self.tracks: Dict[str, MidiTrackState] = {}
        self.markers: List[Tuple[int, str]] = []
        self._channel_cursor = 0
        self._serial = 0
        self._melodic_channels = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15]

    def _next_serial(self) -> int:
        self._serial += 1
        return self._serial

    def _get_track(self, key: str, name: str, program: int, is_drum: bool) -> MidiTrackState:
        track = self.tracks.get(key)
        if track is not None:
            return track
        if is_drum:
            channel = 9
            port = 0
        else:
            logical = self._channel_cursor
            channel = self._melodic_channels[logical % len(self._melodic_channels)]
            port = logical // len(self._melodic_channels)
            self._channel_cursor += 1
        track = MidiTrackState(
            key=key,
            name=name,
            channel=channel,
            port=port,
            program=max(0, min(127, int(program))),
            is_drum=is_drum,
            controllers={7: self.midi_volume, 10: 64, 11: 127, 1: 0},
        )
        self.tracks[key] = track
        return track

    def add_marker(self, sample: int, text: str) -> None:
        marker = (max(0, int(sample)), str(text))
        if marker not in self.markers:
            self.markers.append(marker)

    def _append_channel_event(self, track: MidiTrackState, sample: int, data: bytes, priority: int) -> None:
        track.events.append(TimedMidiEvent(max(0, int(sample)), priority, self._next_serial(), data))

    def _control_change(
        self,
        key: str,
        name: str,
        sample: int,
        controller: int,
        value: int,
        program: int = 80,
        is_drum: bool = False,
    ) -> None:
        controller = max(0, min(127, int(controller)))
        value = max(0, min(127, int(value)))
        track = self._get_track(key, name, program, is_drum)
        if track.controllers.get(controller) == value:
            return
        track.controllers[controller] = value
        status = 0xB0 | track.channel
        self._append_channel_event(track, sample, bytes([status, controller, value]), self.PRIORITY_CONTROL)

    def set_volume(self, key: str, name: str, sample: int, value: int, program: int = 80, is_drum: bool = False) -> None:
        # Keep CC7 as a global channel ceiling while preserving chip dynamics.
        scaled = int(round(max(0, min(127, int(value))) * self.midi_volume / 127.0))
        self._control_change(key, name, sample, 7, scaled, program, is_drum)

    def set_pan(self, key: str, name: str, sample: int, value: int, program: int = 80, is_drum: bool = False) -> None:
        self._control_change(key, name, sample, 10, value, program, is_drum)

    def set_expression(self, key: str, name: str, sample: int, value: int, program: int = 80, is_drum: bool = False) -> None:
        self._control_change(key, name, sample, 11, value, program, is_drum)

    def set_modulation(self, key: str, name: str, sample: int, value: int, program: int = 80, is_drum: bool = False) -> None:
        self._control_change(key, name, sample, 1, value, program, is_drum)

    def _set_pitch_bend(self, track: MidiTrackState, sample: int, value: int, reset: bool = False) -> None:
        value = max(0, min(16383, int(value)))
        if track.pitch_bend == value:
            return
        track.pitch_bend = value
        status = 0xE0 | track.channel
        data = bytes([status, value & 0x7F, (value >> 7) & 0x7F])
        self._append_channel_event(track, sample, data, self.PRIORITY_RESET if reset else self.PRIORITY_PITCH)

    def _bend_for_pitch(self, base_note: int, pitch: float) -> int:
        """Convert a fractional note offset into the asymmetric MIDI 14-bit wheel domain."""
        semitones = max(-self.pitch_bend_range, min(self.pitch_bend_range, pitch - base_note))
        normalized = semitones / float(self.pitch_bend_range)
        if normalized >= 0.0:
            value = 8192 + int(round(normalized * 8191.0))
        else:
            value = 8192 + int(round(normalized * 8192.0))
        return max(0, min(16383, value))

    @staticmethod
    def _is_note_or_pitch_event(event: TimedMidiEvent) -> bool:
        if not event.data:
            return False
        return (event.data[0] & 0xF0) in (0x80, 0x90, 0xE0)

    def _prepare_pitch_group(self, track: MidiTrackState, sample: int) -> None:
        """Coalesce multiple register writes at one VGM timestamp into one final pitch state."""
        sample = max(0, int(sample))
        if track.pitch_group_sample != sample:
            track.pitch_group_sample = sample
            track.pitch_group_serial = self._serial
            track.pitch_group_active_note = track.active_note
            track.pitch_group_active_pitch = track.active_pitch
            track.pitch_group_active_since = track.active_since
            track.pitch_group_pitch_bend = track.pitch_bend
            track.pitch_group_last_pitch_sample = track.last_pitch_sample
            return

        cutoff = track.pitch_group_serial
        track.events[:] = [
            event for event in track.events
            if not (
                event.sample == sample
                and event.serial > cutoff
                and self._is_note_or_pitch_event(event)
            )
        ]
        track.active_note = track.pitch_group_active_note
        track.active_pitch = track.pitch_group_active_pitch
        track.active_since = track.pitch_group_active_since
        track.pitch_bend = track.pitch_group_pitch_bend
        track.last_pitch_sample = track.pitch_group_last_pitch_sample

    def _should_use_pitch_bend(self, track: MidiTrackState, sample: int, pitch: float) -> bool:
        if track.active_note is None or track.active_pitch is None:
            return False
        if abs(pitch - track.active_note) >= self.pitch_bend_range - 1e-6:
            return False

        step = abs(pitch - track.active_pitch)
        if step <= PITCH_FINE_STEP_SEMITONES:
            return True
        if track.last_pitch_sample is None:
            return False
        elapsed = max(0, int(sample) - track.last_pitch_sample)
        return elapsed <= PITCH_CONTINUITY_WINDOW_SAMPLES and step <= PITCH_FAST_STEP_SEMITONES

    def note_on_pitch(
        self,
        key: str,
        name: str,
        sample: int,
        pitch: Optional[float],
        velocity: int = 100,
        program: int = 80,
        retrigger: bool = False,
    ) -> None:
        """Start/retrigger a note, or retain only genuinely continuous pitch motion as Pitch Bend."""
        if pitch is None or not math.isfinite(pitch):
            self.note_off(key, sample)
            return
        pitch = max(0.0, min(127.0, float(pitch)))
        velocity = max(1, min(127, int(velocity)))
        track = self._get_track(key, name, program, False)
        self._prepare_pitch_group(track, sample)

        if track.active_note is not None and not retrigger and self._should_use_pitch_bend(track, sample, pitch):
            bend = self._bend_for_pitch(track.active_note, pitch)
            # About 0.1 cent at the default +/-2 semitone range; avoids register-noise spam.
            if abs(bend - track.pitch_bend) >= 4:
                self._set_pitch_bend(track, sample, bend)
            track.active_pitch = pitch
            track.last_pitch_sample = sample
            return

        if track.active_note is not None:
            self._append_note_off(track, sample, reset_pitch=False, invalidate_pitch_group=False)

        base_note = max(0, min(127, int(round(pitch))))
        bend = self._bend_for_pitch(base_note, pitch)
        self._set_pitch_bend(track, sample, bend)
        status = 0x90 | track.channel
        self._append_channel_event(track, sample, bytes([status, base_note, velocity]), self.PRIORITY_NOTE_ON)
        track.active_note = base_note
        track.active_pitch = pitch
        track.active_since = sample
        track.last_pitch_sample = sample

    def note_on(
        self,
        key: str,
        name: str,
        sample: int,
        note: Optional[int],
        velocity: int = 100,
        program: int = 80,
        is_drum: bool = False,
    ) -> None:
        if not is_drum:
            self.note_on_pitch(key, name, sample, float(note) if note is not None else None, velocity, program)
            return
        if note is None:
            self.note_off(key, sample)
            return
        note = max(0, min(127, int(note)))
        velocity = max(1, min(127, int(velocity)))
        track = self._get_track(key, name, program, True)
        if track.active_note == note:
            return
        if track.active_note is not None:
            self._append_note_off(track, sample)
        status = 0x90 | track.channel
        self._append_channel_event(track, sample, bytes([status, note, velocity]), self.PRIORITY_NOTE_ON)
        track.active_note = note
        track.active_pitch = float(note)
        track.active_since = sample

    def _append_note_off(self, track: MidiTrackState, sample: int, reset_pitch: bool = True, invalidate_pitch_group: bool = True) -> None:
        if track.active_note is None:
            return
        status = 0x80 | track.channel
        self._append_channel_event(track, sample, bytes([status, track.active_note, 0]), self.PRIORITY_NOTE_OFF)
        track.active_note = None
        track.active_pitch = None
        track.last_pitch_sample = None
        if invalidate_pitch_group:
            track.pitch_group_sample = None
        if reset_pitch and not track.is_drum and track.pitch_bend != 8192:
            self._set_pitch_bend(track, sample, 8192, reset=True)

    def note_off(self, key: str, sample: int) -> None:
        track = self.tracks.get(key)
        if track is not None:
            # Treat all note/pitch writes at one VGM timestamp as one transaction.
            # Many OPL/OPN drivers write temporary frequency values around Key Off/On
            # without advancing time; those intermediate values must not become bends.
            self._prepare_pitch_group(track, sample)
            self._append_note_off(track, sample, invalidate_pitch_group=False)

    def stop_all(self, sample: int) -> None:
        for track in self.tracks.values():
            self._append_note_off(track, sample)

    def _sample_to_tick(self, sample: int) -> int:
        ticks_per_second = self.ppqn * self.bpm / 60.0
        return int(round(max(0, sample) * ticks_per_second / SAMPLE_RATE))

    @staticmethod
    def _cc(channel: int, controller: int, value: int) -> bytes:
        return bytes([0xB0 | channel, controller & 0x7F, value & 0x7F])

    def _make_track_chunk(self, track: MidiTrackState) -> bytes:
        body = bytearray()
        body += encode_vlq(0) + meta_event(0x03, safe_text(track.name))
        body += encode_vlq(0) + meta_event(0x21, bytes([track.port & 0x7F]))
        if not track.is_drum:
            body += encode_vlq(0) + bytes([0xC0 | track.channel, track.program])
            for event in (
                self._cc(track.channel, 101, 0),
                self._cc(track.channel, 100, 0),
                self._cc(track.channel, 6, self.pitch_bend_range),
                self._cc(track.channel, 38, 0),
                self._cc(track.channel, 101, 127),
                self._cc(track.channel, 100, 127),
            ):
                body += encode_vlq(0) + event
        for controller, value in ((7, self.midi_volume), (10, 64), (11, 127), (1, 0)):
            body += encode_vlq(0) + self._cc(track.channel, controller, value)

        last_tick = 0
        for event in sorted(track.events):
            tick = self._sample_to_tick(event.sample)
            body += encode_vlq(max(0, tick - last_tick)) + event.data
            last_tick = tick
        body += encode_vlq(0) + meta_event(0x2F, b"")
        return b"MTrk" + struct.pack(">I", len(body)) + body

    def _make_conductor_chunk(self, title: str) -> bytes:
        events: List[Tuple[int, int, bytes]] = []
        events.append((0, 0, meta_event(0x03, safe_text(title))))
        events.append((0, 1, meta_event(0x01, safe_text(
            f"Register-derived MIDI approximation. Tempo: {self.bpm:.1f} BPM ({self.tempo_description}). "
            f"Pitch Bend (+/-{self.pitch_bend_range} semitones), MIDI CC automation, and VGM loop markers are retained when available.", 240))))
        tempo_us = max(1, min(0xFFFFFF, int(round(60_000_000 / self.bpm))))
        events.append((0, 2, bytes([0xFF, 0x51, 0x03]) + tempo_us.to_bytes(3, "big")))
        events.append((0, 3, bytes([0xFF, 0x58, 0x04, 4, 2, 24, 8])))
        for index, (sample, text) in enumerate(sorted(self.markers)):
            events.append((self._sample_to_tick(sample), 10 + index, meta_event(0x06, safe_text(text))))

        conductor = bytearray()
        last_tick = 0
        for tick, order, data in sorted(events, key=lambda item: (item[0], item[1])):
            conductor += encode_vlq(max(0, tick - last_tick)) + data
            last_tick = tick
        conductor += encode_vlq(0) + meta_event(0x2F, b"")
        return b"MTrk" + struct.pack(">I", len(conductor)) + conductor

    def write(self, output_path: Path, title: str = "") -> None:
        title = title.strip() or Path(self.source_name).stem
        conductor_chunk = self._make_conductor_chunk(title)
        track_chunks = [
            self._make_track_chunk(track)
            for track in self.tracks.values()
            if any(
                len(event.data) >= 3
                and (event.data[0] & 0xF0) == 0x90
                and event.data[2] > 0
                for event in track.events
            )
        ]
        if not track_chunks:
            empty = MidiTrackState("empty", "No supported note events", 0, 0, 0, False)
            track_chunks = [self._make_track_chunk(empty)]
        header = b"MThd" + struct.pack(">IHHH", 6, 1, 1 + len(track_chunks), self.ppqn)
        output_path.write_bytes(header + conductor_chunk + b"".join(track_chunks))


class SN76489:
    def __init__(self, collector: MidiCollector, name: str, clock: int, instance: int = 1):
        self.collector = collector
        self.name = name
        self.clock = clock
        self.instance = instance
        self.period = [1, 1, 1]
        self.volume = [15, 15, 15, 15]
        self.pan = [64, 64, 64, 64]
        self.stereo_enabled = [True, True, True, True]
        self.noise_control = 0
        self.latched_channel = 0
        self.latched_is_volume = False

    def _voice_key(self, ch: int) -> str:
        return f"{self.name}#{self.instance}:ch{ch + 1}"

    def _track_name(self, ch: int) -> str:
        label = "Noise" if ch == 3 else f"Tone {ch + 1}"
        return f"{self.name} #{self.instance} {label}"

    def _level(self, ch: int) -> int:
        if not self.stereo_enabled[ch]:
            return 0
        return max(0, min(127, int(round((15 - self.volume[ch]) * 127 / 15))))

    def _refresh_tone(self, ch: int, sample: int) -> None:
        key = self._voice_key(ch)
        name = self._track_name(ch)
        level = self._level(ch)
        self.collector.set_volume(key, name, sample, level, program=80)
        self.collector.set_pan(key, name, sample, self.pan[ch], program=80)
        period = self.period[ch] or 0x400
        if level <= 0 or period <= 0:
            self.collector.note_off(key, sample)
            return
        freq = self.clock / (32.0 * period)
        self.collector.note_on_pitch(key, name, sample, frequency_to_midi_pitch(freq), max(12, level), program=80)

    def _refresh_noise(self, sample: int) -> None:
        ch = 3
        key = self._voice_key(ch)
        name = self._track_name(ch)
        level = self._level(ch)
        self.collector.set_volume(key, name, sample, level, is_drum=True)
        self.collector.set_pan(key, name, sample, self.pan[ch], is_drum=True)
        if level <= 0:
            self.collector.note_off(key, sample)
            return
        rate = self.noise_control & 0x03
        drum_note = [42, 46, 38, 36][rate]
        self.collector.note_on(key, name, sample, drum_note, max(16, level), is_drum=True)

    def set_stereo(self, value: int, sample: int) -> None:
        """Apply Game Gear stereo routing (bits 7-4 left, 3-0 right)."""
        value &= 0xFF
        for ch in range(4):
            left = bool(value & (1 << (ch + 4)))
            right = bool(value & (1 << ch))
            pan = 64 if left == right else (0 if left else 127)
            enabled = left or right
            if self.pan[ch] != pan or self.stereo_enabled[ch] != enabled:
                self.pan[ch] = pan
                self.stereo_enabled[ch] = enabled
                if ch < 3:
                    self._refresh_tone(ch, sample)
                else:
                    self._refresh_noise(sample)

    def write(self, value: int, sample: int) -> None:
        value &= 0xFF
        if value & 0x80:
            self.latched_channel = (value >> 5) & 0x03
            self.latched_is_volume = bool(value & 0x10)
            ch = self.latched_channel
            if self.latched_is_volume:
                self.volume[ch] = value & 0x0F
                if ch < 3:
                    self._refresh_tone(ch, sample)
                else:
                    self._refresh_noise(sample)
            elif ch < 3:
                self.period[ch] = (self.period[ch] & 0x3F0) | (value & 0x0F)
                self._refresh_tone(ch, sample)
            else:
                self.noise_control = value & 0x07
                self._refresh_noise(sample)
        else:
            ch = self.latched_channel
            if not self.latched_is_volume and ch < 3:
                self.period[ch] = (self.period[ch] & 0x00F) | ((value & 0x3F) << 4)
                self._refresh_tone(ch, sample)


class AY8910:
    def __init__(self, collector: MidiCollector, name: str, clock: int, instance: int = 1):
        self.collector = collector
        self.name = name
        self.clock = clock
        self.instance = instance
        self.reg = [0] * 16

    def _tone_key(self, ch: int) -> str:
        return f"{self.name}#{self.instance}:tone{ch}"

    def _noise_key(self, ch: int) -> str:
        return f"{self.name}#{self.instance}:noise{ch}"

    def _refresh(self, ch: int, sample: int) -> None:
        period = self.reg[ch * 2] | ((self.reg[ch * 2 + 1] & 0x0F) << 8)
        mixer = self.reg[7]
        vol_reg = self.reg[8 + ch]
        volume = 15 if (vol_reg & 0x10) else (vol_reg & 0x0F)
        level = max(0, min(127, int(round(volume * 127 / 15))))
        velocity = max(12, level)
        tone_enabled = not bool(mixer & (1 << ch))
        noise_enabled = not bool(mixer & (1 << (ch + 3)))

        tone_key = self._tone_key(ch)
        tone_name = f"{self.name} #{self.instance} Tone {ch + 1}"
        self.collector.set_volume(tone_key, tone_name, sample, level, program=80)
        if tone_enabled and level > 0 and period > 0:
            freq = self.clock / (16.0 * period)
            self.collector.note_on_pitch(tone_key, tone_name, sample, frequency_to_midi_pitch(freq), velocity, program=80)
        else:
            self.collector.note_off(tone_key, sample)

        noise_key = self._noise_key(ch)
        noise_name = f"{self.name} #{self.instance} Noise {ch + 1}"
        self.collector.set_volume(noise_key, noise_name, sample, level, is_drum=True)
        if noise_enabled and level > 0:
            self.collector.note_on(noise_key, noise_name, sample, [42, 38, 46][ch], velocity, is_drum=True)
        else:
            self.collector.note_off(noise_key, sample)

    def write(self, register: int, value: int, sample: int) -> None:
        register &= 0x7F
        if register >= len(self.reg):
            return
        self.reg[register] = value & 0xFF
        if register <= 6:
            ch = register // 2
            if ch < 3:
                self._refresh(ch, sample)
        elif register == 7:
            for ch in range(3):
                self._refresh(ch, sample)
        elif 8 <= register <= 10:
            self._refresh(register - 8, sample)
        elif 11 <= register <= 13:
            for ch in range(3):
                if self.reg[8 + ch] & 0x10:
                    self._refresh(ch, sample)


class OPLChip:
    MOD_OFFSETS = [0, 1, 2, 8, 9, 10, 16, 17, 18]
    CAR_OFFSETS = [3, 4, 5, 11, 12, 13, 19, 20, 21]

    def __init__(self, collector: MidiCollector, name: str, clock: int, channels: int, instance: int = 1, opl3: bool = False):
        self.collector = collector
        self.name = name
        self.clock = clock
        self.channels = channels
        self.instance = instance
        self.opl3 = opl3
        self.a0 = [0] * channels
        self.b0 = [0] * channels
        self.c0 = [0] * channels
        self.tl_mod = [0] * channels
        self.tl_car = [0] * channels
        self.vib_mod = [False] * channels
        self.vib_car = [False] * channels
        self.rhythm = 0

    def _key(self, ch: int) -> str:
        return f"{self.name}#{self.instance}:ch{ch}"

    def _track_name(self, ch: int) -> str:
        return f"{self.name} #{self.instance} Channel {ch + 1}"

    def _operator_channel(self, port: int, register: int, base_register: int) -> Tuple[Optional[int], Optional[bool]]:
        offset = register - base_register
        if offset in self.MOD_OFFSETS:
            return port * 9 + self.MOD_OFFSETS.index(offset), False
        if offset in self.CAR_OFFSETS:
            return port * 9 + self.CAR_OFFSETS.index(offset), True
        return None, None

    def _expression(self, ch: int) -> int:
        additive = bool(self.c0[ch] & 0x01)
        tl = min(self.tl_mod[ch], self.tl_car[ch]) if additive else self.tl_car[ch]
        return max(0, min(127, int(round((63 - tl) * 127 / 63))))

    def _pan(self, ch: int) -> int:
        if not self.opl3:
            return 64
        left = bool(self.c0[ch] & 0x10)
        right = bool(self.c0[ch] & 0x20)
        if left and not right:
            return 0
        if right and not left:
            return 127
        return 64

    def _modulation(self, ch: int) -> int:
        additive = bool(self.c0[ch] & 0x01)
        return 96 if (self.vib_car[ch] or (additive and self.vib_mod[ch])) else 0

    def _refresh_controls(self, ch: int, sample: int) -> None:
        if ch >= self.channels:
            return
        key = self._key(ch)
        name = self._track_name(ch)
        self.collector.set_expression(key, name, sample, self._expression(ch), program=16)
        self.collector.set_pan(key, name, sample, self._pan(ch), program=16)
        self.collector.set_modulation(key, name, sample, self._modulation(ch), program=16)

    def _refresh(self, ch: int, sample: int, retrigger: bool = False) -> None:
        if ch >= self.channels:
            return
        key = self._key(ch)
        name = self._track_name(ch)
        self._refresh_controls(ch, sample)
        if bool(self.rhythm & 0x20) and 6 <= ch <= 8:
            self.collector.note_off(key, sample)
            return
        fnum = self.a0[ch] | ((self.b0[ch] & 0x03) << 8)
        block = (self.b0[ch] >> 2) & 0x07
        if not (self.b0[ch] & 0x20) or fnum == 0:
            self.collector.note_off(key, sample)
            return
        divisor = 288.0 if self.opl3 else 72.0
        freq = fnum * (2.0 ** (block - 1)) * self.clock / ((2.0 ** 19) * divisor)
        self.collector.note_on_pitch(key, name, sample, frequency_to_midi_pitch(freq), max(24, self._expression(ch)), program=16, retrigger=retrigger)

    def _refresh_rhythm(self, old: int, new: int, sample: int) -> None:
        rhythm_on = bool(new & 0x20)
        if rhythm_on != bool(old & 0x20):
            for ch in range(6, min(9, self.channels)):
                self._refresh(ch, sample)
        for bit, note, label in [(4, 36, "Bass Drum"), (3, 38, "Snare"), (2, 45, "Tom"), (1, 49, "Cymbal"), (0, 42, "Hi-Hat")]:
            key = f"{self.name}#{self.instance}:rhythm{bit}"
            name = f"{self.name} #{self.instance} {label}"
            is_on = rhythm_on and bool(new & (1 << bit))
            was_on = bool(old & 0x20) and bool(old & (1 << bit))
            if is_on and not was_on:
                self.collector.note_on(key, name, sample, note, 110, is_drum=True)
            elif was_on and not is_on:
                self.collector.note_off(key, sample)

    def write(self, port: int, register: int, value: int, sample: int) -> None:
        port &= 1
        register &= 0xFF
        value &= 0xFF
        base = 9 if (self.opl3 and port == 1) else 0
        if 0x20 <= register <= 0x35:
            ch, carrier = self._operator_channel(port if self.opl3 else 0, register, 0x20)
            if ch is not None and ch < self.channels:
                if carrier:
                    self.vib_car[ch] = bool(value & 0x40)
                else:
                    self.vib_mod[ch] = bool(value & 0x40)
                self._refresh_controls(ch, sample)
        elif 0x40 <= register <= 0x55:
            ch, carrier = self._operator_channel(port if self.opl3 else 0, register, 0x40)
            if ch is not None and ch < self.channels:
                if carrier:
                    self.tl_car[ch] = value & 0x3F
                else:
                    self.tl_mod[ch] = value & 0x3F
                self._refresh_controls(ch, sample)
        elif 0xA0 <= register <= 0xA8:
            ch = base + register - 0xA0
            if ch < self.channels:
                self.a0[ch] = value
                self._refresh(ch, sample)
        elif 0xB0 <= register <= 0xB8:
            ch = base + register - 0xB0
            if ch < self.channels:
                old = self.b0[ch]
                self.b0[ch] = value
                retrigger = not bool(old & 0x20) and bool(value & 0x20)
                self._refresh(ch, sample, retrigger=retrigger)
        elif 0xC0 <= register <= 0xC8:
            ch = base + register - 0xC0
            if ch < self.channels:
                self.c0[ch] = value
                self._refresh_controls(ch, sample)
        elif register == 0xBD and port == 0:
            old = self.rhythm
            self.rhythm = value
            self._refresh_rhythm(old, value, sample)


class OPLLChip:
    def __init__(self, collector: MidiCollector, name: str, clock: int, instance: int = 1):
        self.collector = collector
        self.name = name
        self.clock = clock
        self.instance = instance
        self.low = [0] * 9
        self.high = [0] * 9
        self.instvol = [0] * 9
        self.custom_carrier_vibrato = False
        self.rhythm = 0

    def _key(self, ch: int) -> str:
        return f"{self.name}#{self.instance}:ch{ch}"

    def _track_name(self, ch: int) -> str:
        return f"{self.name} #{self.instance} Channel {ch + 1}"

    def _level(self, ch: int) -> int:
        return max(0, min(127, int(round((15 - (self.instvol[ch] & 0x0F)) * 127 / 15))))

    def _refresh(self, ch: int, sample: int, retrigger: bool = False) -> None:
        key = self._key(ch)
        name = self._track_name(ch)
        level = self._level(ch)
        self.collector.set_volume(key, name, sample, level, program=16)
        instrument = (self.instvol[ch] >> 4) & 0x0F
        modulation = 96 if instrument == 0 and self.custom_carrier_vibrato else 0
        self.collector.set_modulation(key, name, sample, modulation, program=16)
        if (self.rhythm & 0x20) and ch >= 6:
            self.collector.note_off(key, sample)
            return
        fnum = self.low[ch] | ((self.high[ch] & 1) << 8)
        block = (self.high[ch] >> 1) & 7
        if not (self.high[ch] & 0x10) or fnum == 0 or level <= 0:
            self.collector.note_off(key, sample)
            return
        freq = fnum * (2.0 ** block) * self.clock / (72.0 * (2.0 ** 19))
        self.collector.note_on_pitch(key, name, sample, frequency_to_midi_pitch(freq), max(18, level), program=16, retrigger=retrigger)

    def _rhythm_update(self, old: int, new: int, sample: int) -> None:
        if bool(old & 0x20) != bool(new & 0x20):
            for ch in range(6, 9):
                self._refresh(ch, sample)
        for bit, note, label in [(4, 36, "Bass Drum"), (3, 38, "Snare"), (2, 45, "Tom"), (1, 49, "Cymbal"), (0, 42, "Hi-Hat")]:
            key = f"{self.name}#{self.instance}:rhythm{bit}"
            name = f"{self.name} #{self.instance} {label}"
            is_on = bool(new & 0x20) and bool(new & (1 << bit))
            was_on = bool(old & 0x20) and bool(old & (1 << bit))
            if is_on and not was_on:
                self.collector.note_on(key, name, sample, note, 110, is_drum=True)
            elif was_on and not is_on:
                self.collector.note_off(key, sample)

    def write(self, register: int, value: int, sample: int) -> None:
        register &= 0xFF
        value &= 0xFF
        if register == 0x01:
            self.custom_carrier_vibrato = bool(value & 0x40)
            for ch in range(9):
                if self._key(ch) in self.collector.tracks and ((self.instvol[ch] >> 4) & 0x0F) == 0:
                    self._refresh(ch, sample)
        elif 0x10 <= register <= 0x18:
            ch = register - 0x10
            self.low[ch] = value
            self._refresh(ch, sample)
        elif 0x20 <= register <= 0x28:
            ch = register - 0x20
            old = self.high[ch]
            self.high[ch] = value
            retrigger = not bool(old & 0x10) and bool(value & 0x10)
            self._refresh(ch, sample, retrigger=retrigger)
        elif 0x30 <= register <= 0x38:
            ch = register - 0x30
            self.instvol[ch] = value
            self._refresh(ch, sample)
        elif register == 0x0E:
            old = self.rhythm
            self.rhythm = value
            self._rhythm_update(old, value, sample)


class OPNChip:
    # OPN register groups are ordered OP1, OP3, OP2, OP4.
    CARRIER_GROUPS = {
        0: (3,), 1: (3,), 2: (3,), 3: (3,),
        4: (2, 3), 5: (2, 1, 3), 6: (2, 1, 3), 7: (0, 2, 1, 3),
    }

    def __init__(self, collector: MidiCollector, name: str, clock: int, channels: int,
                 instance: int = 1, has_ssg: bool = False, has_stereo: bool = True):
        self.collector = collector
        self.name = name
        self.clock = clock
        self.channels = channels
        self.instance = instance
        self.has_stereo = has_stereo
        self.low = [0] * channels
        self.high = [0] * channels
        self.key_on = [False] * channels
        self.algorithm = [0] * channels
        self.tl = [[127] * 4 for _ in range(channels)]
        self.pan_fms = [0xC0 if has_stereo else 0] * channels
        self.lfo_enabled = False
        self.dac_enabled = False
        self.ssg = AY8910(collector, f"{name} SSG", max(1, clock // 4), instance) if has_ssg else None

    def _key(self, ch: int) -> str:
        return f"{self.name}#{self.instance}:ch{ch}"

    def _track_name(self, ch: int) -> str:
        return f"{self.name} #{self.instance} Channel {ch + 1}"

    def _expression(self, ch: int) -> int:
        groups = self.CARRIER_GROUPS.get(self.algorithm[ch] & 7, (3,))
        level = max(0, min(127, 127 - min(self.tl[ch][group] for group in groups)))
        if self.has_stereo and not (self.pan_fms[ch] & 0xC0):
            return 0
        return level

    def _pan(self, ch: int) -> int:
        if not self.has_stereo:
            return 64
        left = bool(self.pan_fms[ch] & 0x80)
        right = bool(self.pan_fms[ch] & 0x40)
        if left and not right:
            return 0
        if right and not left:
            return 127
        return 64

    def _modulation(self, ch: int) -> int:
        if not self.lfo_enabled:
            return 0
        fms = self.pan_fms[ch] & 0x07
        ams = (self.pan_fms[ch] >> 4) & 0x03
        return max(0, min(127, max(fms * 18, ams * 32)))

    def _refresh_controls(self, ch: int, sample: int) -> None:
        if ch >= self.channels:
            return
        key = self._key(ch)
        name = self._track_name(ch)
        self.collector.set_expression(key, name, sample, self._expression(ch), program=81)
        self.collector.set_pan(key, name, sample, self._pan(ch), program=81)
        self.collector.set_modulation(key, name, sample, self._modulation(ch), program=81)

    def _refresh(self, ch: int, sample: int, retrigger: bool = False) -> None:
        if ch >= self.channels:
            return
        key = self._key(ch)
        name = self._track_name(ch)
        self._refresh_controls(ch, sample)
        if self.dac_enabled and ch == 5 and self.channels >= 6:
            self.collector.note_off(key, sample)
            return
        fnum = self.low[ch] | ((self.high[ch] & 0x07) << 8)
        block = (self.high[ch] >> 3) & 0x07
        if not self.key_on[ch] or fnum == 0 or self._expression(ch) <= 0:
            self.collector.note_off(key, sample)
            return
        freq = fnum * (2.0 ** (block - 1)) * self.clock / (144.0 * (2.0 ** 20))
        self.collector.note_on_pitch(key, name, sample, frequency_to_midi_pitch(freq),
                                     max(24, self._expression(ch)), program=81, retrigger=retrigger)

    def write(self, port: int, register: int, value: int, sample: int) -> None:
        port &= 1
        register &= 0xFF
        value &= 0xFF
        if self.ssg is not None and port == 0 and register <= 0x0D:
            self.ssg.write(register, value, sample)
        if register == 0x22 and port == 0:
            self.lfo_enabled = bool(value & 0x08)
            for ch in range(self.channels):
                self._refresh_controls(ch, sample)
        elif 0x40 <= register <= 0x4F and (register & 0x03) != 0x03:
            ch = port * 3 + (register & 0x03)
            group = (register >> 2) & 0x03
            if ch < self.channels:
                self.tl[ch][group] = value & 0x7F
                self._refresh_controls(ch, sample)
        elif 0xA0 <= register <= 0xA2:
            ch = port * 3 + register - 0xA0
            if ch < self.channels:
                self.low[ch] = value
                self._refresh(ch, sample)
        elif 0xA4 <= register <= 0xA6:
            ch = port * 3 + register - 0xA4
            if ch < self.channels:
                self.high[ch] = value
                self._refresh(ch, sample)
        elif 0xB0 <= register <= 0xB2:
            ch = port * 3 + register - 0xB0
            if ch < self.channels:
                self.algorithm[ch] = value & 0x07
                self._refresh_controls(ch, sample)
        elif 0xB4 <= register <= 0xB6:
            ch = port * 3 + register - 0xB4
            if ch < self.channels:
                self.pan_fms[ch] = value
                self._refresh_controls(ch, sample)
        elif register == 0x28 and port == 0:
            code = value & 0x07
            if code not in (3, 7):
                ch = (code & 0x03) + (3 if code & 0x04 else 0)
                if ch < self.channels:
                    new_on = bool(value & 0xF0)
                    self.key_on[ch] = new_on
                    self._refresh(ch, sample, retrigger=new_on)
        elif register == 0x2B and port == 0 and self.channels >= 6:
            old = self.dac_enabled
            self.dac_enabled = bool(value & 0x80)
            if old != self.dac_enabled:
                self._refresh(5, sample)


class YM2151:
    NOTE_CODE_TO_SEMITONE = {0x0: 0, 0x1: 1, 0x2: 2, 0x4: 3, 0x5: 4, 0x6: 5,
                             0x8: 6, 0x9: 7, 0xA: 8, 0xC: 9, 0xD: 10, 0xE: 11}
    CARRIER_GROUPS = {
        0: (3,), 1: (3,), 2: (3,), 3: (3,),
        4: (1, 3), 5: (1, 2, 3), 6: (1, 2, 3), 7: (0, 1, 2, 3),
    }

    def __init__(self, collector: MidiCollector, name: str, instance: int = 1):
        self.collector = collector
        self.name = name
        self.instance = instance
        self.kc = [0] * 8
        self.kf = [0] * 8
        self.key_on = [False] * 8
        self.algorithm_pan = [0xC0] * 8
        self.tl = [[127] * 4 for _ in range(8)]
        self.pms_ams = [0] * 8
        self.pmd = 0
        self.lfo_frequency = 0

    def _key(self, ch: int) -> str:
        return f"{self.name}#{self.instance}:ch{ch}"

    def _track_name(self, ch: int) -> str:
        return f"{self.name} #{self.instance} Channel {ch + 1}"

    def _expression(self, ch: int) -> int:
        carriers = self.CARRIER_GROUPS.get(self.algorithm_pan[ch] & 7, (3,))
        level = max(0, min(127, 127 - min(self.tl[ch][group] for group in carriers)))
        return 0 if not (self.algorithm_pan[ch] & 0xC0) else level

    def _pan(self, ch: int) -> int:
        left = bool(self.algorithm_pan[ch] & 0x40)
        right = bool(self.algorithm_pan[ch] & 0x80)
        if left and not right:
            return 0
        if right and not left:
            return 127
        return 64

    def _modulation(self, ch: int) -> int:
        pms = (self.pms_ams[ch] >> 4) & 0x07
        if pms == 0 or self.pmd == 0:
            return 0
        return max(0, min(127, int(round((pms / 7.0) * (self.pmd / 127.0) * 127))))

    def _refresh_controls(self, ch: int, sample: int) -> None:
        key = self._key(ch)
        name = self._track_name(ch)
        self.collector.set_expression(key, name, sample, self._expression(ch), program=81)
        self.collector.set_pan(key, name, sample, self._pan(ch), program=81)
        self.collector.set_modulation(key, name, sample, self._modulation(ch), program=81)

    def _refresh(self, ch: int, sample: int, retrigger: bool = False) -> None:
        key = self._key(ch)
        name = self._track_name(ch)
        self._refresh_controls(ch, sample)
        if not self.key_on[ch] or self._expression(ch) <= 0:
            self.collector.note_off(key, sample)
            return
        code = self.kc[ch]
        octave = (code >> 4) & 0x07
        semitone = self.NOTE_CODE_TO_SEMITONE.get(code & 0x0F)
        if semitone is None:
            self.collector.note_off(key, sample)
            return
        pitch = 12.0 * (octave + 1) + semitone + (((self.kf[ch] >> 2) & 0x3F) / 64.0)
        self.collector.note_on_pitch(key, name, sample, pitch, max(24, self._expression(ch)), program=81, retrigger=retrigger)

    def write(self, register: int, value: int, sample: int) -> None:
        register &= 0xFF
        value &= 0xFF
        if register == 0x08:
            ch = value & 0x07
            new_on = bool(value & 0x78)
            self.key_on[ch] = new_on
            self._refresh(ch, sample, retrigger=new_on)
        elif register == 0x18:
            self.lfo_frequency = value
        elif register == 0x19:
            if value & 0x80:
                self.pmd = value & 0x7F
                for ch in range(8):
                    self._refresh_controls(ch, sample)
        elif 0x20 <= register <= 0x27:
            ch = register - 0x20
            self.algorithm_pan[ch] = value
            self._refresh_controls(ch, sample)
        elif 0x28 <= register <= 0x2F:
            ch = register - 0x28
            self.kc[ch] = value
            self._refresh(ch, sample)
        elif 0x30 <= register <= 0x37:
            ch = register - 0x30
            self.kf[ch] = value
            self._refresh(ch, sample)
        elif 0x38 <= register <= 0x3F:
            ch = register - 0x38
            self.pms_ams[ch] = value
            self._refresh_controls(ch, sample)
        elif 0x60 <= register <= 0x7F:
            ch = register & 0x07
            group = (register - 0x60) // 8
            if group < 4:
                self.tl[ch][group] = value & 0x7F
                self._refresh_controls(ch, sample)


@dataclass
class ConversionResult:
    source: Path
    output: Optional[Path]
    used_chips: List[str]
    unsupported_commands: int
    note_tracks: int
    duration_seconds: float
    bpm: float
    bpm_confidence: float
    bpm_method: str
    loop_start_seconds: Optional[float] = None
    loop_end_seconds: Optional[float] = None
    warning: str = ""


class VGMConverter:
    def __init__(self, source: Path, bpm: Optional[float] = None, pitch_bend_range: int = DEFAULT_PITCH_BEND_RANGE, midi_volume: int = DEFAULT_MIDI_VOLUME):
        self.source = Path(source)
        self.requested_bpm = bpm
        self.data = self._read_file(self.source)
        if len(self.data) < 0x40 or self.data[:4] != b"Vgm ":
            raise ValueError("Invalid VGM/VGZ file: VGM header not found.")
        self.version = u32le(self.data, 0x08)
        data_rel = u32le(self.data, 0x34) if self.version >= 0x00000150 else 0
        self.data_offset = 0x34 + data_rel if data_rel else 0x40
        if self.data_offset < 0x40 or self.data_offset >= len(self.data):
            raise ValueError(f"Invalid VGM data offset: 0x{self.data_offset:X}")
        self.collector = MidiCollector(self.source.name, bpm if bpm is not None else DEFAULT_BPM, pitch_bend_range=pitch_bend_range, midi_volume=midi_volume)
        self.used_chips: Set[str] = set()
        self.unsupported_commands = 0
        self.sample_pos = 0
        self.loop_samples = u32le(self.data, 0x20)
        loop_rel = u32le(self.data, 0x1C)
        self.loop_offset = 0x1C + loop_rel if loop_rel else None
        if self.loop_offset is not None and not (self.data_offset <= self.loop_offset < len(self.data)):
            self.loop_offset = None
        self.loop_start_sample: Optional[int] = None
        self.loop_end_sample: Optional[int] = None
        self.bpm_detection = BPMDetection(
            bpm if bpm is not None else DEFAULT_BPM,
            1.0 if bpm is not None else 0.0,
            "manual override" if bpm is not None else "not analyzed",
        )
        self.title = self._read_gd3_title() or self.source.stem
        self._build_chips()

    @staticmethod
    def _read_file(path: Path) -> bytes:
        raw = path.read_bytes()
        # The VGM specification recommends detecting compression independently
        # of the filename extension, because either extension may be compressed.
        if raw[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(raw)
            except OSError as exc:
                raise ValueError(f"Unable to decompress VGZ data: {exc}") from exc
        return raw

    def _read_gd3_title(self) -> str:
        rel = u32le(self.data, 0x14)
        if not rel:
            return ""
        offset = 0x14 + rel
        if offset + 12 > len(self.data) or self.data[offset : offset + 4] != b"Gd3 ":
            return ""
        length = u32le(self.data, offset + 8)
        payload = self.data[offset + 12 : offset + 12 + length]
        try:
            text = payload.decode("utf-16le", errors="replace")
            fields = text.split("\x00")
            return (fields[0] or (fields[1] if len(fields) > 1 else "")).strip()
        except Exception:
            return ""

    def _clock(self, offset: int, default: int) -> int:
        return clean_clock(u32le(self.data, offset), default)

    def _build_chips(self) -> None:
        c = self.collector
        self.sn = [
            SN76489(c, "SN76489", self._clock(0x0C, 3_579_545), 1),
            SN76489(c, "SN76489", self._clock(0x0C, 3_579_545), 2),
        ]
        self.opll = [
            OPLLChip(c, "YM2413", self._clock(0x10, 3_579_545), 1),
            OPLLChip(c, "YM2413", self._clock(0x10, 3_579_545), 2),
        ]
        self.ym2612 = [
            OPNChip(c, "YM2612", self._clock(0x2C, 7_670_454), 6, 1, False),
            OPNChip(c, "YM2612", self._clock(0x2C, 7_670_454), 6, 2, False),
        ]
        self.ym2151 = [YM2151(c, "YM2151", 1), YM2151(c, "YM2151", 2)]
        self.ym2203 = [
            OPNChip(c, "YM2203", self._clock(0x44, 3_000_000), 3, 1, True, False),
            OPNChip(c, "YM2203", self._clock(0x44, 3_000_000), 3, 2, True, False),
        ]
        self.ym2608 = [
            OPNChip(c, "YM2608", self._clock(0x48, 8_000_000), 6, 1, True),
            OPNChip(c, "YM2608", self._clock(0x48, 8_000_000), 6, 2, True),
        ]
        self.ym2610 = [
            OPNChip(c, "YM2610", self._clock(0x4C, 8_000_000), 6, 1, True),
            OPNChip(c, "YM2610", self._clock(0x4C, 8_000_000), 6, 2, True),
        ]
        self.ym3812 = [
            OPLChip(c, "YM3812", self._clock(0x50, 3_579_545), 9, 1),
            OPLChip(c, "YM3812", self._clock(0x50, 3_579_545), 9, 2),
        ]
        self.ym3526 = [
            OPLChip(c, "YM3526", self._clock(0x54, 3_579_545), 9, 1),
            OPLChip(c, "YM3526", self._clock(0x54, 3_579_545), 9, 2),
        ]
        self.y8950 = [
            OPLChip(c, "Y8950", self._clock(0x58, 3_579_545), 9, 1),
            OPLChip(c, "Y8950", self._clock(0x58, 3_579_545), 9, 2),
        ]
        self.ymf262 = [
            OPLChip(c, "YMF262", self._clock(0x5C, 14_318_180), 18, 1, True),
            OPLChip(c, "YMF262", self._clock(0x5C, 14_318_180), 18, 2, True),
        ]
        self.ay = [
            AY8910(c, "AY8910", self._clock(0x74, 1_789_773), 1),
            AY8910(c, "AY8910", self._clock(0x74, 1_789_773), 2),
        ]

    def _require(self, size: int, pos: int) -> None:
        if pos + size > len(self.data):
            raise ValueError(f"VGM command stream is truncated at 0x{pos:X}.")

    def _chip_write(self, cmd: int, reg: int, value: int, instance: int = 0) -> None:
        s = self.sample_pos
        if cmd == 0x51:
            self.used_chips.add("YM2413")
            self.opll[instance].write(reg, value, s)
        elif cmd in (0x52, 0x53):
            self.used_chips.add("YM2612")
            self.ym2612[instance].write(cmd - 0x52, reg, value, s)
        elif cmd == 0x54:
            self.used_chips.add("YM2151")
            self.ym2151[instance].write(reg, value, s)
        elif cmd == 0x55:
            self.used_chips.add("YM2203")
            self.ym2203[instance].write(0, reg, value, s)
        elif cmd in (0x56, 0x57):
            self.used_chips.add("YM2608")
            self.ym2608[instance].write(cmd - 0x56, reg, value, s)
        elif cmd in (0x58, 0x59):
            self.used_chips.add("YM2610")
            self.ym2610[instance].write(cmd - 0x58, reg, value, s)
        elif cmd == 0x5A:
            self.used_chips.add("YM3812")
            self.ym3812[instance].write(0, reg, value, s)
        elif cmd == 0x5B:
            self.used_chips.add("YM3526")
            self.ym3526[instance].write(0, reg, value, s)
        elif cmd == 0x5C:
            self.used_chips.add("Y8950")
            self.y8950[instance].write(0, reg, value, s)
        elif cmd in (0x5E, 0x5F):
            self.used_chips.add("YMF262")
            self.ymf262[instance].write(cmd - 0x5E, reg, value, s)

    def parse(self) -> None:
        p = self.data_offset
        data = self.data
        while p < len(data):
            if self.loop_offset is not None and p == self.loop_offset and self.loop_start_sample is None:
                self.loop_start_sample = self.sample_pos
            cmd = data[p]
            if cmd == 0x00:
                p += 1
            elif cmd == 0x30:
                self._require(2, p)
                self.used_chips.add("SN76489")
                self.sn[1].write(data[p + 1], self.sample_pos)
                p += 2
            elif cmd == 0x31:
                p += 2
            elif cmd == 0x40:
                p += 3
            elif cmd == 0x4F:
                self._require(2, p)
                self.used_chips.add("SN76489")
                self.sn[0].set_stereo(data[p + 1], self.sample_pos)
                p += 2
            elif cmd == 0x50:
                self._require(2, p)
                self.used_chips.add("SN76489")
                self.sn[0].write(data[p + 1], self.sample_pos)
                p += 2
            elif 0x51 <= cmd <= 0x5F:
                self._require(3, p)
                self._chip_write(cmd, data[p + 1], data[p + 2], 0)
                p += 3
            elif cmd == 0x61:
                self._require(3, p)
                self.sample_pos += u16le(data, p + 1)
                p += 3
            elif cmd == 0x62:
                self.sample_pos += 735
                p += 1
            elif cmd == 0x63:
                self.sample_pos += 882
                p += 1
            elif cmd == 0x66:
                p += 1
                break
            elif cmd == 0x67:
                self._require(7, p)
                if data[p + 1] != 0x66:
                    raise ValueError(f"Invalid VGM data block at 0x{p:X}.")
                size = u32le(data, p + 3)
                self._require(7 + size, p)
                p += 7 + size
            elif cmd == 0x68:
                self._require(12, p)
                p += 12
            elif 0x70 <= cmd <= 0x7F:
                self.sample_pos += (cmd & 0x0F) + 1
                p += 1
            elif 0x80 <= cmd <= 0x8F:
                # YM2612 DAC sample write. The PCM sample itself has no direct MIDI equivalent.
                self.sample_pos += cmd & 0x0F
                p += 1
            elif cmd == 0x90:
                p += 5
            elif cmd == 0x91:
                p += 5
            elif cmd == 0x92:
                p += 6
            elif cmd == 0x93:
                p += 11
            elif cmd == 0x94:
                p += 2
            elif cmd == 0x95:
                p += 5
            elif cmd == 0xA0:
                self._require(3, p)
                register = data[p + 1]
                instance = 1 if register & 0x80 else 0
                self.used_chips.add("AY8910")
                self.ay[instance].write(register & 0x7F, data[p + 2], self.sample_pos)
                p += 3
            elif 0xA1 <= cmd <= 0xAF:
                self._require(3, p)
                base_cmd = 0x50 + (cmd & 0x0F)
                self._chip_write(base_cmd, data[p + 1], data[p + 2], 1)
                p += 3
            elif 0xB0 <= cmd <= 0xBF:
                # Register writes for currently unsupported chips are skipped safely.
                self.unsupported_commands += 1
                p += 3
            elif 0xC0 <= cmd <= 0xDF:
                self.unsupported_commands += 1
                p += 4
            elif 0xE0 <= cmd <= 0xFF:
                self.unsupported_commands += 1
                p += 5
            elif 0x32 <= cmd <= 0x3F:
                self.unsupported_commands += 1
                p += 2
            elif 0x41 <= cmd <= 0x4E:
                self.unsupported_commands += 1
                p += 3
            else:
                raise ValueError(f"Unsupported or undefined VGM command 0x{cmd:02X} at file offset 0x{p:X}.")
            if p > len(data):
                raise ValueError("A VGM command extends beyond the end of the file.")

        if self.loop_samples > 0:
            if self.loop_start_sample is None:
                self.loop_start_sample = max(0, self.sample_pos - self.loop_samples)
            computed_end = self.loop_start_sample + self.loop_samples
            self.loop_end_sample = min(self.sample_pos, computed_end) if self.sample_pos else computed_end
            self.collector.add_marker(self.loop_start_sample, "LOOP START")
            self.collector.add_marker(self.loop_end_sample, "LOOP END")
        self.collector.stop_all(self.sample_pos)

    def convert(self, output_path: Path) -> ConversionResult:
        self.parse()
        if self.requested_bpm is None:
            self.bpm_detection = detect_bpm_from_events(self.collector.tracks.values(), self.loop_samples)
        else:
            manual_bpm = max(1.0, min(float(self.requested_bpm), 999.0))
            self.bpm_detection = BPMDetection(manual_bpm, 1.0, "manual override")
        self.collector.bpm = self.bpm_detection.bpm
        self.collector.tempo_description = (
            f"{self.bpm_detection.method}, confidence {self.bpm_detection.confidence:.0%}"
            if self.requested_bpm is None
            else self.bpm_detection.method
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self.collector.write(output_path, self.title)
        note_tracks = sum(
            1 for track in self.collector.tracks.values()
            if any(len(event.data) >= 3 and (event.data[0] & 0xF0) == 0x90 and event.data[2] > 0
                   for event in track.events)
        )
        warning = ""
        if not note_tracks:
            warning = "No supported note events were found; the file may mainly use PCM or unsupported chips."
        return ConversionResult(
            source=self.source,
            output=output_path,
            used_chips=sorted(self.used_chips),
            unsupported_commands=self.unsupported_commands,
            note_tracks=note_tracks,
            duration_seconds=self.sample_pos / SAMPLE_RATE,
            bpm=self.bpm_detection.bpm,
            bpm_confidence=self.bpm_detection.confidence,
            bpm_method=self.bpm_detection.method,
            loop_start_seconds=(self.loop_start_sample / SAMPLE_RATE) if self.loop_start_sample is not None else None,
            loop_end_seconds=(self.loop_end_sample / SAMPLE_RATE) if self.loop_end_sample is not None else None,
            warning=warning,
        )


def output_path_for(source: Path, output_dir: Optional[Path]) -> Path:
    directory = output_dir if output_dir is not None else source.parent
    return directory / f"{source.stem}.mid"


def convert_file(source: Path, output_dir: Optional[Path] = None, bpm: Optional[float] = None, pitch_bend_range: int = DEFAULT_PITCH_BEND_RANGE, midi_volume: int = DEFAULT_MIDI_VOLUME) -> ConversionResult:
    source = Path(source)
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file extension: {source.suffix}")
    converter = VGMConverter(source, bpm, pitch_bend_range, midi_volume)
    return converter.convert(output_path_for(source, output_dir))


def gather_input_files(paths: Iterable[Path]) -> List[Path]:
    result: List[Path] = []
    seen: Set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            candidates = sorted(
                p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        else:
            candidates = [path] if path.suffix.lower() in SUPPORTED_EXTENSIONS else []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate.absolute()
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
    return result


def format_result(result: ConversionResult) -> str:
    chips = ", ".join(result.used_chips) if result.used_chips else "none"
    text = (
        f"Done: {result.source.name} -> {result.output.name if result.output else '-'} | "
        f"{result.duration_seconds:.2f}s | BPM {result.bpm:.1f}"
        f" ({result.bpm_method}, confidence {result.bpm_confidence:.0%}) | "
        f"note tracks {result.note_tracks} | chips: {chips}"
    )
    if result.loop_start_seconds is not None and result.loop_end_seconds is not None:
        text += f" | loop {result.loop_start_seconds:.3f}s-{result.loop_end_seconds:.3f}s"
    if result.unsupported_commands:
        text += f" | skipped unsupported commands: {result.unsupported_commands}"
    if result.warning:
        text += f" | warning: {result.warning}"
    return text



def run_conversion(paths: Sequence[str], bpm: Optional[float] = None, output_dir: Optional[Path] = None, pitch_bend_range: int = DEFAULT_PITCH_BEND_RANGE, midi_volume: int = DEFAULT_MIDI_VOLUME) -> int:
    """Convert paths supplied by Windows drag-and-drop or the command line."""
    files = gather_input_files(Path(p) for p in paths)
    if not files:
        print("No .vgz or .vgm files were found.", file=sys.stderr)
        return 2

    failures: List[str] = []
    print(f"vgz2midi: starting conversion of {len(files)} file(s)")
    for path in files:
        try:
            result = convert_file(path, output_dir, bpm, pitch_bend_range, midi_volume)
            print(format_result(result))
        except Exception as exc:
            message = f"Failed: {path}: {exc}"
            failures.append(message)
            print(message, file=sys.stderr)

    if failures:
        # A persistent error log is useful because a drag-and-drop console window
        # may close immediately after the program exits.
        base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
        log_path = base_dir / "vgz2midi_errors.txt"
        log_path.write_text("\n".join(failures) + "\n", encoding="utf-8")
        print(f"{len(failures)} error(s). Details: {log_path}", file=sys.stderr)
        return 1

    print(f"Conversion complete: {len(files)} file(s)")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert VGZ/VGM files to Standard MIDI Files. Multiple files or folders may be supplied."
        )
    )
    parser.add_argument("paths", nargs="*", help="input .vgz/.vgm files or folders")
    parser.add_argument(
        "--bpm",
        type=float,
        default=None,
        help="override MIDI BPM; omit to estimate tempo from event timing",
    )
    parser.add_argument("-o", "--output", help="output folder; defaults to each source file folder")
    parser.add_argument(
        "--pitch-bend-range",
        type=int,
        default=DEFAULT_PITCH_BEND_RANGE,
        choices=range(1, MAX_PITCH_BEND_RANGE + 1),
        metavar="SEMITONES",
        help=f"Pitch Bend sensitivity in semitones (default: {DEFAULT_PITCH_BEND_RANGE}; range: 1-{MAX_PITCH_BEND_RANGE})",
    )
    parser.add_argument(
        "--midi-volume",
        type=int,
        default=DEFAULT_MIDI_VOLUME,
        choices=range(0, 128),
        metavar="0-127",
        help=f"Maximum MIDI channel volume (default: {DEFAULT_MIDI_VOLUME}; range: 0-127)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.paths:
        parser.print_help()
        print("\nUsage: drag VGZ/VGM files or folders onto vgz2midi.py, or pass them on the command line.")
        return 2
    output_dir = Path(args.output).expanduser().resolve() if args.output else None
    return run_conversion(args.paths, args.bpm, output_dir, args.pitch_bend_range, args.midi_volume)


if __name__ == "__main__":
    raise SystemExit(main())
