"""Проверка распознавания без микрофона.

Генерирует WAV с фразой через Windows Speech, распознаёт его Vosk
и прогоняет текст через движок команд (без выполнения действий).
"""
import json
import os
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "libs"))

import vosk  # noqa: E402

from assistant import CommandEngine, MODEL_DIR, has_wake_word, resolve_model_path  # noqa: E402

MODEL = MODEL_DIR
PHRASE = "Джарвис, который час"
WAV = os.path.join(tempfile.gettempdir(), "jarvis-speech-test.wav")

MAKE_WAV = rf"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{WAV}', $fmt)
$s.Speak('{PHRASE}')
$s.Dispose()
"""


def make_wav() -> None:
    os.makedirs(os.path.dirname(WAV), exist_ok=True)
    if os.path.exists(WAV):
        os.remove(WAV)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", MAKE_WAV],
        check=True,
        capture_output=True,
        timeout=60,
    )


def recognize() -> str:
    model = vosk.Model(resolve_model_path(MODEL))
    rec = vosk.KaldiRecognizer(model, 16000)
    with wave.open(WAV, "rb") as w:
        print(f"wav: {w.getnchannels()}ch {w.getframerate()}Hz {w.getsampwidth() * 8}bit")
        while True:
            data = w.readframes(4000)
            if not data:
                break
            rec.AcceptWaveform(data)
    return json.loads(rec.FinalResult()).get("text", "")


def main() -> int:
    if not os.path.isdir(MODEL):
        print("нет модели — запустите tools/getmodel.py")
        return 1
    make_wav()
    text = recognize()
    print(f"фраза:        {PHRASE}")
    print(f"распознано:   {text!r}")

    engine = CommandEngine(os.path.join(ROOT, "commands.json"))
    tail = has_wake_word(text)
    print(f"после wake:   {tail!r}")
    cmd, rest = engine.match(tail or text)
    print(f"команда:      {cmd['action'] if cmd else 'НЕ НАЙДЕНА'}")
    print(f"остаток:      {rest!r}")
    return 0 if cmd else 2


if __name__ == "__main__":
    sys.exit(main())
