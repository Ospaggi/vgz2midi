# vgz2midi drag-and-drop converter

`vgz2midi.py` converts `.vgz` and `.vgm` files to Standard MIDI Files for editing, transcription, and arrangement.

## Quick start

1. Install Python 3 on Windows.
2. Select one or more `.vgz` or `.vgm` files.
3. Drag them onto `vgz2midi.py`.
4. A `.mid` file with the same base name is created beside each source file.

Example:

- Input: `music.vgz`
- Output: `music.mid`

A folder can also be dropped onto `vgz2midi.py`. The converter searches that folder and its subfolders for `.vgz` and `.vgm` files.

## Automatic BPM detection

VGM files normally do not contain an explicit BPM value. `vgz2midi.py` estimates BPM from:

- extracted note and sample-trigger timing
- rhythmic repetition between event onsets
- simultaneous-event accents
- VGM loop length, when available

The detected BPM is written as the MIDI tempo meta event. VGM sample positions are converted to MIDI ticks using that tempo while preserving the original playback duration.

The console output shows the estimated BPM and confidence. Half-time and double-time ambiguity is still possible with sparse, ambient, free-time, or sample-heavy music.

To override automatic detection:

```text
python vgz2midi.py music.vgz --bpm 128
```

When `--bpm` is omitted, automatic detection is used. If there are too few usable events, the converter falls back to 120 BPM.

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

### Yamaha FM, PSG, PCM, and ADPCM

- YM2413 / OPLL
- YM2612 / OPN2, including DAC PCM
- YM2151 / OPM
- YM2203 / OPN
- YM2608 / OPNA, including rhythm PCM and ADPCM-B
- YM2610 / OPNB, including ADPCM-A and ADPCM-B
- YM3812 / OPL2
- YM3526 / OPL
- Y8950, including Delta-T ADPCM
- YMF262 / OPL3
- YMF278B / OPL4, including the 18-channel FM section and 24 PCM voices
- AY-3-8910 compatible PSG

### Other programmable sound generators

- SN76489
- Game Boy DMG APU
- NES APU, including DPCM
- HuC6280 PSG, including DDA PCM
- Konami K051649 SCC
- Atari POKEY
- Philips SAA1099

Dual-chip VGM commands are handled for the supported chips.

## PCM and ADPCM conversion

Standard MIDI cannot contain the original PCM or ADPCM waveform. The converter therefore writes symbolic MIDI sample-trigger events that retain as much sequencing information as the VGM stream exposes.

The following information is preserved or estimated:

- sample start time
- sample stop time or estimated duration
- sample identity from a data-bank block, ROM address range, wave number, or DPCM address
- playback-rate changes as relative MIDI pitch when the chip provides a rate or frequency register
- level or attenuation as MIDI velocity when available
- fixed rhythm voices as General MIDI drum notes

### Implemented sample paths

- **YM2612 DAC PCM**
  - direct writes to DAC register `0x2A`
  - VGM PCM bank commands `0x80` through `0x8F`
  - PCM seek command `0xE0`
- **VGM DAC stream system**
  - data blocks and data banks
  - stream setup/start/stop commands `0x90` through `0x95`
  - uncompressed banks and supported n-bit/DPCM-compressed banks
- **YM2608 / OPNA**
  - six fixed rhythm-ROM voices
  - Delta-T ADPCM-B playback, address range, level, rate, repeat, and stop tracking
- **YM2610 / OPNB**
  - six ADPCM-A voices
  - Delta-T ADPCM-B playback
- **Y8950**
  - Delta-T ADPCM playback
- **NES APU**
  - DPCM address, length, rate, loop state, and direct output level
- **HuC6280**
  - DDA sample-write runs and write-rate estimation
- **YMF278B / OPL4**
  - 24 PCM voice key-on/key-off events
  - wave number, F-number, octave, and total-level tracking

Sample-based melodic tracks use a neutral sample-oriented MIDI program. Because most VGM sample formats do not define a musical root key, the first observed playback rate for a sample is treated as C4 and later rate changes are mapped relative to it. Drum-like and fixed-rhythm samples are placed on the MIDI percussion channel.

## Per-chip note coverage

### Game Boy DMG APU

- two square channels
- one wave channel
- one noise channel mapped to MIDI drums
- trigger timing and register-derived volume

Wave RAM changes affect timbre but do not contain independent note events. Hardware length-counter expiration, sweep, and envelope evolution are not simulated exactly.

### NES APU

- two pulse channels
- triangle channel
- noise channel mapped to MIDI drums
- DPCM sample triggers

Expansion audio is not currently decoded.

### HuC6280 PSG

- six wavetable tone channels
- noise on channels 5 and 6 mapped to MIDI drums
- DDA PCM runs
- channel selection, frequency, volume, and enable state

LFO behavior is not reproduced.

### Konami K051649 SCC

- five wavetable channels
- frequency, volume, and key-mask tracking

Waveform RAM affects timbre and is not embedded in MIDI.

### Atari POKEY

- four channels
- normal, high-clock, and joined-channel pitch estimation
- polynomial/noise modes mapped to MIDI drums

Distortion modes and counter edge cases are approximated rather than cycle-emulated.

### Philips SAA1099

- six tone channels
- two routed noise generators mapped to MIDI drums
- amplitude, octave, frequency, and enable registers

Hardware envelopes are not reproduced.

## Conversion limitations

VGM/VGZ stores sound-chip commands rather than MIDI notes. The generated MIDI is an analytical approximation, not an audio conversion.

- Original PCM, ADPCM, waveform-ROM, and waveform-RAM audio is not embedded in the MIDI file.
- A sample's original instrument name and musical root note are usually absent from VGM data, so sample track naming and pitch mapping are inferred.
- Sample end times can be estimated from byte length and playback rate, but chip looping, status flags, or external ROM behavior may make the real duration different.
- FM instruments, detailed envelopes, modulation, pitch slides, hardware effects, and special channel modes cannot always be represented accurately.
- FM Channel 3 special-slot frequencies and CSM behavior are not fully decoded.

## Error log

If a conversion fails, `vgz2midi_errors.txt` is written beside `vgz2midi.py`.

## Requirements

- Python 3.9 or newer recommended
- No external Python packages required
