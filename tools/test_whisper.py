#!/usr/bin/env python3
"""Сравнение STT: Groq Whisper (облако) против локального Vosk.

    python tools/test_whisper.py

Генерирует русскую фразу через Windows Speech, распознаёт её Whisper'ом
(если аккаунт Groq его даёт) и локальной Vosk-моделью.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "libs"))

import requests  # noqa: E402

from brain import API, load_key  # noqa: E402

PHRASE = "Джарвис, какая погода в Москве и который час"
WAV = os.path.join(tempfile.gettempdir(), "jarvis-test-phrase.wav")

MAKE_WAV = """
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{wav}', $fmt)
$s.Speak('{phrase}')
$s.Dispose()
""".format(wav=WAV, phrase=PHRASE)


def make_wav() -> None:
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", MAKE_WAV],
        check=True,
        capture_output=True,
        timeout=90,
    )


def main() -> int:
    key = load_key()
    if not key:
        print("Нет ключа Groq.")
        return 1
    headers = {"Authorization": f"Bearer {key}"}

    r = requests.get(f"{API}/models", headers=headers, timeout=20)
    ids = [m["id"] for m in r.json().get("data", [])]
    print("модели аккаунта:", ", ".join(ids))
    whisper = [i for i in ids if "whisper" in i or "transcribe" in i]
    if not whisper:
        print("Whisper в аккаунте недоступен.")
        return 2

    make_wav()
    data = open(WAV, "rb").read()
    print(f"фраза: {PHRASE!r}")
    print(f"wav: {os.path.getsize(WAV)} байт")

    for model in whisper:
        try:
            rr = requests.post(
                f"{API}/audio/transcriptions",
                headers=headers,
                files={"file": ("audio.wav", data, "audio/wav")},
                data={"model": model, "language": "ru", "response_format": "json"},
                timeout=60,
            )
            if rr.status_code == 200:
                print(f"whisper [{model}] -> {rr.json().get('text', '').strip()!r}")
            else:
                print(f"whisper [{model}] -> HTTP {rr.status_code}: {rr.text[:200]}")
        except Exception as e:  # noqa: BLE001
            print(f"whisper [{model}] -> ERR {type(e).__name__}: {e}")

    # Локальный Vosk для сравнения
    try:
        import vosk

        from assistant import MODEL_DIR, SAMPLE_RATE, resolve_model_path

        model = vosk.Model(resolve_model_path(MODEL_DIR))
        rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        with wave.open(WAV, "rb") as w:
            rec.AcceptWaveform(w.readframes(w.getnframes()))
        print(f"vosk [{os.path.basename(MODEL_DIR)}] -> {json.loads(rec.FinalResult()).get('text')!r}")
    except Exception as e:  # noqa: BLE001
        print(f"vosk -> ERR {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
