# vgz2midi 드래그 앤 드롭 변환기

`vgz2midi.py`는 `.vgz`와 `.vgm` 파일을 편집·채보·편곡용 표준 MIDI 파일로 변환합니다.

## 가장 간단한 사용법

1. Windows에 Python 3를 설치합니다.
2. 변환할 `.vgz` 또는 `.vgm` 파일을 하나 이상 선택합니다.
3. 선택한 파일을 `vgz2midi.py` 위에 끌어다 놓습니다.
4. 원본 파일과 같은 폴더에 같은 이름의 `.mid` 파일이 생성됩니다.

폴더를 끌어다 놓으면 하위 폴더까지 검색해 지원 파일을 변환합니다.

## BPM 자동 감지

VGM 파일에는 일반적으로 BPM 값이 직접 저장되지 않습니다. 변환기는 추출된 음표 시작 시점, 반복되는 타이밍 패턴, 동시 발음 강세, VGM 루프 길이를 조합해 BPM을 추정합니다.

감지한 BPM은 MIDI Tempo 메타 이벤트에 기록합니다. 분석 가능한 이벤트가 너무 적으면 120 BPM을 사용합니다.

BPM을 직접 지정하려면 다음과 같이 실행합니다.

```text
python vgz2midi.py music.vgz --bpm 128
```

## Pitch Bend·비브라토·포르타멘토

원본 칩의 채널이 키온 상태인 동안 주파수가 변하면 MIDI 음표와 Pitch Bend 이벤트로 변환합니다.

- MIDI RPN 0,0으로 Pitch Bend 범위를 기본 `±2`반음으로 초기화해 General MIDI 호환성을 높였습니다.
- 미세하거나 짧은 간격으로 연속되는 음정 변화만 Pitch Bend로 유지합니다.
- 일반적인 음표 이동과 명시적인 칩 키온 명령은 넓은 벤드로 연결하지 않고 새 MIDI 음표로 재발음합니다.
- 같은 VGM 시점의 주파수 레지스터 쓰기, Key Off, Key On을 하나의 음정 트랜잭션으로 합친 뒤 최종 Pitch Bend를 생성합니다.
- 같은 시점의 새 Note On 뒤에 Pitch Bend 중앙값 복원이 배치되던 문제를 수정해 새 음표의 미세 음정 보정이 유지됩니다.
- 연속 주파수 이동은 포르타멘토 형태의 Pitch Bend 곡선이 됩니다.
- 확인 가능한 하드웨어 LFO·비브라토 설정은 Modulation Wheel `CC1`로 근사합니다.

VGM에는 칩 내부 LFO가 매 순간 계산한 실제 파형이 저장되지 않으므로, 주파수 레지스터를 직접 움직이는 비브라토가 하드웨어 LFO 근사보다 정확합니다.

## Volume·Expression·Pan 자동화

칩에서 확인할 수 있는 레벨과 스테레오 레지스터 변화를 MIDI 컨트롤러 이벤트로 변환합니다.

- `CC7 Volume`: 기본 상한을 127이 아닌 100으로 설정하며, 칩 채널 레벨도 이 상한 안에서 비례 조정
- `CC11 Expression`: FM 캐리어 연산자의 Total Level 변화
- `CC10 Pan`: Game Gear 스테레오, YM2612·OPNA·OPNB, YM2151, OPL3 라우팅
- `CC1 Modulation`: 지원 가능한 FM·OPLL 비브라토와 LFO 깊이

같은 값이 반복되면 중복 CC 이벤트를 제거합니다. FM 출력 레벨은 현재 알고리즘에서 캐리어로 동작하는 연산자의 TL을 분석해 추정하며, 실제 FM 엔벌로프 전체를 에뮬레이션한 값은 아닙니다.

## VGM 루프 마커

VGM 헤더에 유효한 루프 오프셋과 루프 샘플 수가 있으면 MIDI 컨덕터 트랙에 다음 Marker 메타 이벤트를 기록합니다.

- `LOOP START`
- `LOOP END`

루프 구간을 여러 번 복제하지 않고, DAW나 시퀀서에서 원본 루프 경계를 찾을 수 있도록 위치만 표시합니다.

## 명령행 사용법

파일 하나 변환:

```text
python vgz2midi.py music.vgz
```

여러 파일 변환:

```text
python vgz2midi.py song1.vgz song2.vgm
```

폴더와 하위 폴더의 지원 파일 변환:

```text
python vgz2midi.py "D:\vgm music"
```

Pitch Bend 범위를 직접 지정:

```text
python vgz2midi.py music.vgz --pitch-bend-range 12
```

MIDI 채널 최대 음량 지정:

```text
python vgz2midi.py music.vgz --midi-volume 90
```

출력 폴더 지정:

```text
python vgz2midi.py music.vgz -o "D:\midi"
```

## 지원 사운드 칩

현재 다음 칩의 음정, 키온·키오프 타이밍과 확인 가능한 컨트롤 정보를 근사적으로 해석합니다.

- SN76489 및 Game Gear 스테레오 라우팅
- AY-3-8910 호환 PSG
- YM2413 / OPLL
- YM2612 / OPN2
- YM2151 / OPM
- YM2203 / OPN 및 SSG
- YM2608 / OPNA 및 SSG
- YM2610 / OPNB 및 SSG
- YM3812 / OPL2
- YM3526 / OPL
- Y8950 FM 부분
- YMF262 / OPL3

위 레지스터 쓰기 경로의 듀얼 칩 VGM 명령도 처리합니다.

## 변환 한계

VGM/VGZ는 MIDI 음표가 아니라 사운드 칩 명령을 저장하므로 결과는 분석용 근사 변환입니다.

- 원본 FM 악기와 칩 파형은 MIDI에 포함되지 않습니다.
- PCM·ADPCM·DAC 샘플 오디오와 외부 샘플 ROM 내용은 MIDI에 삽입하거나 복원하지 않습니다.
- 하드웨어 엔벌로프와 칩 내부 LFO 파형을 사이클 단위로 에뮬레이션하지 않습니다.
- FM 채널 3 특수 슬롯 음정, CSM, 4OP 결합 세부 동작과 특수 모드는 불완전할 수 있습니다.
- Pitch Bend와 MIDI CC는 레지스터에서 확인 가능한 변화를 보존하지만 모든 칩 전용 효과를 정확히 재현하지는 못합니다.
- `--pitch-bend-range`에는 1~24반음을 지정할 수 있습니다. 기본값은 2이며, 더 넓은 값은 큰 포르타멘토가 필요하고 MIDI 재생기가 Pitch Bend Sensitivity RPN을 정상 처리할 때 사용하는 것이 좋습니다.
- `--midi-volume`에는 0~127을 지정할 수 있습니다. 기본값은 100이며 CC7 채널 음량의 최대값으로 사용됩니다.

## 오류 로그

변환 중 오류가 발생하면 `vgz2midi.py` 옆에 `vgz2midi_errors.txt`가 생성됩니다.

## 요구 사항

- Python 3.9 이상 권장
- 외부 Python 패키지 불필요
