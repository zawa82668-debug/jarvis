#!/usr/bin/env python3
"""Проверка доступа к Groq API (бесплатный тир).

Запуск:
    python tools/test_groq.py          # только список моделей
    python tools/test_groq.py --chat   # плюс пробный запрос к модели

Ключ берётся из переменной GROQ_API_KEY или из файла jarvis/.groq-key.txt
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "libs"))

try:
    import requests
except Exception as e:  # noqa: BLE001
    print(f"requests недоступен: {e}")
    sys.exit(2)

API = "https://api.groq.com/openai/v1"
KEY_FILE = os.path.join(BASE, ".groq-key.txt")


def load_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, encoding="utf-8-sig") as f:
            return f.read().strip()
    return ""


def main() -> int:
    key = load_key()
    if not key:
        print("Ключ не найден: положи его в jarvis/.groq-key.txt или задай GROQ_API_KEY.")
        return 2
    print(f"Ключ: {key[:4]}…{key[-4:]} ({len(key)} символов)")
    headers = {"Authorization": f"Bearer {key}"}

    try:
        r = requests.get(f"{API}/models", headers=headers, timeout=20)
    except Exception as e:  # noqa: BLE001
        print(f"СЕТЬ: запрос не ушёл ({type(e).__name__}: {e})")
        return 1
    print(f"GET /models -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        return 1

    data = r.json().get("data", [])
    ignore = ("whisper", "tts", "guard", "distil-whisper", "playai", "orpheus", "allam")
    chat = [m for m in data if not any(x in m.get("id", "") for x in ignore)]
    print(f"моделей всего: {len(data)}, чат-моделей: {len(chat)}")
    for m in sorted(chat, key=lambda x: x.get("id", "")):
        ctx = m.get("context_window") or "?"
        print(f"  - {m.get('id')}  ctx={ctx}")

    if "--chat" in sys.argv:
        if not chat:
            print("Нет чат-моделей для теста.")
            return 1
        model = chat[0]["id"]
        print(f"\nПробный запрос к {model}…")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Ответь одной фразой по-русски: ты на связи?"}],
            "max_tokens": 60,
            "temperature": 0.3,
        }
        try:
            r = requests.post(f"{API}/chat/completions", headers=headers, json=payload, timeout=40)
        except Exception as e:  # noqa: BLE001
            print(f"СЕТЬ: запрос не ушёл ({type(e).__name__}: {e})")
            return 1
        print(f"POST /chat/completions -> HTTP {r.status_code}")
        if r.status_code != 200:
            print(r.text[:500])
            return 1
        try:
            msg = r.json()["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            print(json.dumps(r.json(), ensure_ascii=False)[:800])
            return 1
        print(f"ОТВЕТ: {msg.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
