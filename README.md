# vgz2midi drag-and-drop converter

`vgz2midi.py` converts `.vgz` and `.vgm` files to Standard MIDI Files for editing, transcription, and arrangement.

## Quick start

1. Install Python 3 on Windows.
2. Select one or more `.vgz` or `.vgm` files.
3. Drag them onto `vgz2midi.py`.
4. A `.mid` file with the same base name is created beside each source file.

A folder can also be dropped onto `vgz2midi.py`. The converter searches that folder and its subfolders for supported files.

## Automatic BPM detection

VGM files normally do not contain an explicit BPM value. The converter estimates BPM from extracted note onsets, repeated timing patterns, simultaneous-note accents, and the VGM loop length when available.

The detected BPM is written as the MIDI Tempo meta event. If there are too few usable events, the converter falls back to 120 BPM.

To override automatic detection:

```text
python vgz2midi.py music.vgz --bpm 128
```

## Pitch Bend, vibrato, and portamento

Frequency changes made while a source channel remains keyed on are written as MIDI Pitch Bend instead of repeated Note On events.

- Pitch Bend range defaults to `+/-2` semitones and is initialized with MIDI RPN 0,0 for broad General MIDI compatibility.
- Fine or rapidly repeated pitch changes are retained as Pitch Bend.
- Ordinary note jumps and explicit chip key-on commands retrigger a MIDI note instead of becoming an incorrect wide bend.
- Multiple frequency-register writes, Key Off, and Key On operations at the same VGM timestamp are treated as one pitch transaction before the final bend value is emitted.
- A Pitch Bend reset can no longer be sorted after a same-timestamp Note On; the new note keeps its required fractional-pitch correction.
- Continuous frequency sweeps become portamento-style Pitch Bend curves.
- Supported hardware LFO or vibrato flags are approximated with Modulation Wheel `CC1`.

Direct frequency-register modulation is more accurate than hardware-LFO approximation because VGM does not store the continuously evaluated internal LFO waveform.

## Volume, Expression, and Pan automation

Register changes are converted to MIDI controller events where the chip exposes suitable information.

- `CC7 Volume`: defaults to 100 instead of 127; chip-derived channel levels are scaled under this ceiling
- `CC11 Expression`: FM carrier Total Level changes
- `CC10 Pan`: Game Gear stereo, YM2612/OPNA/OPNB, YM2151, and OPL3 routing
- `CC1 Modulation`: supported FM/OPLL vibrato and LFO depth settings

Duplicate controller values are suppressed. FM output level is estimated from the operators that act as carriers for the selected algorithm; this is not a complete FM envelope simulation.

## VGM loop markers

When the VGM header contains a valid loop offset and loop sample count, the MIDI conductor track receives:

- `LOOP START`
- `LOOP END`

The loop section is not duplicated. The markers identify the original loop boundaries for a DAW, sequencer, or game-music tool.

## Command-line usage

Convert one file:

```text
python vgz2midi.py music.vgz
```

Convert several files:

```text
python vgz2midi.py song1.vgz song2.vgm
```

Convert every supported file under a folder:

```text
python vgz2midi.py "D:\vgm music"
```

Choose a wider Pitch Bend range for large portamento sweeps:

```text
python vgz2midi.py music.vgz --pitch-bend-range 12
```

Set a different maximum MIDI channel volume:

```text
python vgz2midi.py music.vgz --midi-volume 90
```

Choose an output folder:

```text
python vgz2midi.py music.vgz -o "D:\midi"
```

## Supported sound chips

The converter currently decodes approximate pitch, key-on/key-off timing, and available controller information from:

- SN76489, including Game Gear stereo routing
- AY-3-8910 compatible PSG
- YM2413 / OPLL
- YM2612 / OPN2
- YM2151 / OPM
- YM2203 / OPN with SSG
- YM2608 / OPNA with SSG
- YM2610 / OPNB with SSG
- YM3812 / OPL2
- YM3526 / OPL
- Y8950 FM section
- YMF262 / OPL3

Dual-chip VGM commands are handled for these register-write paths.

## Conversion limitations

VGM/VGZ stores sound-chip commands rather than MIDI notes, so the result is an analytical approximation.

- Original FM instruments and chip waveforms are not embedded in MIDI.
- PCM, ADPCM, DAC sample audio, and external sample ROM contents are not embedded or reconstructed.
- Hardware envelopes and internal LFO waveforms are not cycle-emulated.
- FM Channel 3 special-slot frequencies, CSM, four-operator coupling details, and other special modes may be incomplete.
- Pitch Bend and MIDI CC events preserve register-visible changes but cannot reproduce every chip-specific effect exactly.
- `--pitch-bend-range` accepts 1-24 semitones. The default is 2; wider values are mainly useful for unusually large portamento sweeps and require a receiver that honors Pitch Bend Sensitivity RPN messages.
- `--midi-volume` accepts 0-127. The default is 100 and acts as the maximum CC7 channel level.

## Error log

If a conversion fails, `vgz2midi_errors.txt` is written beside `vgz2midi.py`.

## Requirements

- Python 3.9 or newer recommended
- No external Python packages required
