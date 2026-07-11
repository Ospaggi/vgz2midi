# vgz2midi drag-and-drop converter

`vgz2midi.py` converts `.vgz` and `.vgm` files to Standard MIDI Files.

## Quick start

1. Install Python 3 on Windows.
2. Select one or more `.vgz` or `.vgm` files.
3. Drag them onto `vgz2midi.py`.
4. A `.mid` file with the same base name is created beside each source file.

Example:

- Input: `music.vgz`
- Output: `music.mid`

You can also drag a folder onto `vgz2midi.py`. The converter searches that folder and its subfolders for all `.vgz` and `.vgm` files.

## Automatic BPM detection

VGM files normally do not contain an explicit BPM value. `vgz2midi.py` estimates BPM from:

- extracted note-on timing
- rhythmic repetition between note onsets
- simultaneous-note accents
- VGM loop length, when available

The detected BPM is written as the MIDI tempo meta event. The original playback duration is preserved by converting VGM sample positions to MIDI ticks using the detected tempo.

The console output shows the estimated BPM and a confidence value. Half-time and double-time ambiguity is still possible with sparse, ambient, free-time, PCM-heavy, or unsupported-chip music.

To override automatic detection:

```text
python vgz2midi.py music.vgz --bpm 128
```

When `--bpm` is omitted, automatic detection is used. If there are too few supported note events, the converter falls back to 120 BPM.

## Command-line usage

Convert one file:

```text
python vgz2midi.py music.vgz
```

Convert several files:

```text
python vgz2midi.py song1.vgz song2.vgm
```

Convert every `.vgz` and `.vgm` file under a folder:

```text
python vgz2midi.py "D:\vgm music"
```

Choose an output folder:

```text
python vgz2midi.py music.vgz -o "D:\midi"
```

## Supported sound chips

The converter extracts approximate note pitch and key-on/key-off timing from:

- SN76489
- AY-3-8910 compatible PSG
- YM2413 / OPLL
- YM2612 / OPN2
- YM2151 / OPM
- YM2203 / OPN
- YM2608 / OPNA
- YM2610 / OPNB
- YM3812 / OPL2
- YM3526 / OPL
- Y8950
- YMF262 / OPL3

Dual-chip VGM commands are also handled for the supported chips.

## OPNA support

YM2608/OPNA support currently includes:

- six FM channels
- three SSG tone/noise channels
- pitch extraction
- key-on and key-off timing
- dual YM2608 instances

The following OPNA features are not converted directly to MIDI:

- six-channel OPNA rhythm samples
- ADPCM-B sample playback
- independent slot frequencies in FM Channel 3 special mode
- CSM mode
- original FM instruments, envelopes, modulation, and LFO behavior

## Conversion limitations

VGM/VGZ stores sound-chip register writes rather than MIDI notes. The generated MIDI is therefore an approximation intended for editing, transcription, and arrangement.

Original chip timbres, PCM/ADPCM samples, detailed envelopes, modulation, pitch slides, and chip-specific effects cannot always be represented accurately in Standard MIDI Files.

## Error log

If a conversion fails, `vgz2midi_errors.txt` is written beside `vgz2midi.py`.

## Requirements

- Python 3.9 or newer recommended
- No external Python packages required
