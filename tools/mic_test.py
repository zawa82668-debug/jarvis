#!/usr/bin/env python3
"""Проверка микрофона: пишет несколько секунд, показывает уровень и распознанный текст.

Запуск:
    python tools/mic_test.py                 # авто-выбор микрофона
    python tools/mic_test.py --device 3      # конкретное устройство
    python tools/mic_test.py --seconds 10
"""
from __future__ import annotations

import array
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "libs"))

import sounddevice as sd  # noqa: E402

from assistant import (  # noqa: E402
    MODEL_DIR,
    SAMPLE_RATE,
    pick_input_device,
    print_input_devices,
    resolve_model_path,
)


def arg_value(args: list[str], name: str) -> str | None:
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            return args[idx + 1]
    return None


def main() -> int:
    args = sys.argv[1:]
    try:
        seconds = float(arg_value(args, "--seconds") or 6)
    except ValueError:
        seconds = 6.0

    device_arg = arg_value(args, "--device")
    if device_arg is not None:
        try:
            device = int(device_arg)
        except ValueError:
            print("--device должен быть числом; список: python assistant.py --devices")
            return 2
    else:
        device = pick_input_device(sd)

    if device is None:
        print("Рабочий микрофон не найден.")
        print_input_devices(sd)
        return 1

    name = sd.query_devices(device)["name"]
    print(f"микрофон: #{device} — {name}")
    print(f"говори что-нибудь {seconds:.0f} секунд…")

    frames: list[bytes] = []

    def callback(indata, _n, _t, status):  # noqa: ANN001, ARG001
        if status:
            print(f"[mic] {status}")
        frames.append(bytes(indata))

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=4000,
        dtype="int16",
        channels=1,
        device=device,
        callback=callback,
    ):
        time.sleep(seconds)

    data = b"".join(frames)
    samples = array.array("h")
    samples.frombytes(data[: len(data) // 2 * 2])
    rms = math.sqrt(sum(x * x for x in samples) / max(1, len(samples)))
    level = "тишина" if rms < 200 else ("тихо" if rms < 800 else "сигнал есть")
    print(f"семплов: {len(samples)}, RMS: {rms:.0f} ({level})")

    try:
        import vosk

        model = vosk.Model(resolve_model_path(MODEL_DIR))
        rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
        rec.AcceptWaveform(data)
        text = json.loads(rec.FinalResult()).get("text", "").strip()
        print(f"распознано: {text or '—'}")
    except Exception as e:  # noqa: BLE001
        print(f"Vosk не смог проверить запись: {e}")

    if rms < 200:
        print()
        print("Микрофон молчит. Что проверить:")
        print("  1. микрофон не в Mute в Windows и в его софте;")
        print("  2. выбран правильный вход (python assistant.py --devices);")
        print("  3. уровень в Параметры → Система → Звук → Ввод;")
        print("  4. если выбран не тот аппарат — перезапусти с --device N.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
