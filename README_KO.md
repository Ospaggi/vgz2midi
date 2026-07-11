# vgz2midi 드래그 앤 드롭 변환기

`vgz2midi.py`는 `.vgz`와 `.vgm` 파일을 표준 MIDI 파일로 변환합니다.

## 가장 간단한 사용법

1. Windows에 Python 3를 설치합니다.
2. 변환할 `.vgz` 또는 `.vgm` 파일을 하나 이상 선택합니다.
3. 선택한 파일을 `vgz2midi.py` 위에 끌어다 놓습니다.
4. 원본 파일과 같은 폴더에 같은 이름의 `.mid` 파일이 생성됩니다.

예:

- 입력: `music.vgz`
- 출력: `music.mid`

폴더를 `vgz2midi.py` 위에 끌어다 놓으면 하위 폴더까지 검색하여 모든 `.vgz`와 `.vgm` 파일을 변환합니다.

## BPM 자동 감지

VGM 파일에는 일반적으로 BPM 값이 직접 저장되어 있지 않습니다. `vgz2midi.py`는 다음 정보를 조합하여 BPM을 추정합니다.

- 추출된 음표의 시작 시점
- 음표 시작 간격의 반복 패턴
- 여러 채널이 동시에 발음되는 강세
- VGM 루프 길이 정보

감지한 BPM은 MIDI의 Tempo 메타 이벤트에 기록됩니다. VGM 샘플 위치를 감지된 템포 기준의 MIDI 틱으로 환산하여 원곡의 전체 재생 시간을 유지합니다.

콘솔에는 감지된 BPM과 신뢰도가 표시됩니다. 음표가 적은 곡, 자유 박자 곡, 앰비언트 곡, PCM 중심 곡 또는 미지원 칩 중심 곡은 반 BPM이나 두 배 BPM으로 판정될 수 있습니다.

BPM을 직접 지정하려면 다음과 같이 실행합니다.

```text
python vgz2midi.py music.vgz --bpm 128
```

`--bpm`을 생략하면 자동 감지를 사용합니다. 지원되는 음표 이벤트가 너무 적으면 120 BPM을 기본값으로 사용합니다.

## 명령행 사용법

파일 하나 변환:

```text
python vgz2midi.py music.vgz
```

여러 파일 변환:

```text
python vgz2midi.py song1.vgz song2.vgm
```

폴더와 하위 폴더의 모든 파일 변환:

```text
python vgz2midi.py "D:\vgm music"
```

출력 폴더 지정:

```text
python vgz2midi.py music.vgz -o "D:\midi"
```

## 지원 사운드 칩

다음 칩의 음정과 키온·키오프 타이밍을 근사적으로 추출합니다.

- SN76489
- AY-3-8910 호환 PSG
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

지원 칩의 듀얼 칩 VGM 명령도 처리합니다.

## OPNA 지원 범위

YM2608/OPNA에서 현재 지원하는 항목:

- FM 6채널
- SSG 톤·노이즈 3채널
- 음정 추출
- 키온·키오프 타이밍
- 듀얼 YM2608 인스턴스

현재 MIDI로 직접 변환하지 않는 항목:

- OPNA 리듬 샘플 6종
- ADPCM-B 샘플 재생
- FM 채널 3 특수 모드의 슬롯별 독립 음정
- CSM 모드
- 원래 FM 음색, 엔벌로프, 변조 및 LFO 동작

## 변환 한계

VGM/VGZ는 MIDI 음표가 아니라 사운드 칩 레지스터 기록을 저장합니다. 따라서 생성된 MIDI는 편집, 채보 및 편곡을 위한 근사 변환 결과입니다.

원래 칩 음색, PCM/ADPCM 샘플, 세부 엔벌로프, 변조, 피치 슬라이드와 칩 고유 효과는 표준 MIDI로 정확히 표현되지 않을 수 있습니다.

## 오류 로그

변환 중 오류가 발생하면 `vgz2midi.py` 옆에 `vgz2midi_errors.txt`가 생성됩니다.

## 요구 사항

- Python 3.9 이상 권장
- 외부 Python 패키지 불필요
