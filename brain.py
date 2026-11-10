#!/usr/bin/env python3
"""Мозг Джарвиса — LLM через Groq (бесплатный тир) с потоковым ответом и инструментами.

Ключ: переменная окружения GROQ_API_KEY или файл jarvis/.groq-key.txt.
Модель: JARVIS_MODEL (по умолчанию самая быстрая разумная — openai/gpt-oss-20b).

Проверка:
    python brain.py --test
    python brain.py --ask "какая сегодня погода?"
"""
from __future__ import annotations

import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "libs"))

try:
    import requests
except Exception as e:  # noqa: BLE001
    print(f"requests недоступен: {e}")
    requests = None

API = "https://api.groq.com/openai/v1"
KEY_FILE = os.path.join(BASE, ".groq-key.txt")

# Порядок предпочтения: сначала быстрые модели — голосовой ассистент не должен ждать.
PREFERRED_MODELS = (
    "openai/gpt-oss-20b",          # ~1.2 c — выбор по умолчанию
    "openai/gpt-oss-120b",         # умнее, но ~4–5 c
    "qwen/qwen3.8-27b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
)
NON_CHAT_HINTS = ("whisper", "tts", "orpheus", "guard", "allam")

PERSONA = (
    "Ты — Джарвис, голосовой ассистент на компьютере пользователя с Windows. "
    "Говори по-русски, дружелюбно и по делу. "
    "Ответ будет прочитан вслух: максимум 25 слов, максимум 2 предложения, "
    "без markdown, списков и смайликов. "
    "Ты умеешь выполнять действия через инструменты: открывать программы и сайты, "
    "искать в интернете, узнавать погоду, ставить напоминания, читать буфер обмена, "
    "управлять звуком и медиа, делать скриншот, блокировать компьютер, узнавать время. "
    "Если пользователь просит действие — вызови инструмент, а не рассказывай о нём. "
    "После результата коротко скажи, что сделал, без служебных слов вроде «Сделано». "
    "Не выдумывай факты; если не знаешь — скажи честно."
)

HISTORY_LIMIT = 12
REQUEST_TIMEOUT = (10, 60)  # (connect, read)
MAX_ROUNDS = 4

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "current_datetime",
            "description": "Текущие дата и время на компьютере пользователя.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Открыть программу на компьютере: браузер, блокнот, калькулятор, "
            "проводник, дискорд, телеграм, стим и т.п.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Название программы"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_site",
            "description": "Открыть сайт в браузере по названию или адресу.",
            "parameters": {
                "type": "object",
                "properties": {"site": {"type": "string", "description": "Название или адрес сайта"}},
                "required": ["site"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Найти что-либо в интернете (откроет поиск в браузере).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Узнать текущую погоду в городе.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "Город, например Москва"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Поставить напоминание, которое прозвучит голосом через N минут.",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "Через сколько минут напомнить"},
                    "text": {"type": "string", "description": "Что напомнить"},
                },
                "required": ["minutes", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "Показать активные напоминания.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_clipboard",
            "description": "Прочитать вслух текст, скопированный в буфер обмена.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media",
            "description": "Управление медиа и звуком.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "play_pause",
                            "next",
                            "prev",
                            "volume_up",
                            "volume_down",
                            "volume_mute",
                        ],
                    }
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Сделать скриншот экрана.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lock_pc",
            "description": "Заблокировать компьютер.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _fix_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def load_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if key:
        return key
    try:
        if os.path.exists(KEY_FILE):
            with open(KEY_FILE, encoding="utf-8-sig") as f:
                return f.read().strip()
    except OSError:
        pass
    return ""


def clean_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_#>]+", " ", text)
    text = re.sub(r"^\s*[-•\d]+[.)]\s+", "", text, flags=re.M)
    text = re.sub(r"\((?:[^()]{0,60})\)", " ", text)
    # То, что SAPI читает вслух коряво.
    text = text.replace("°C", " градусов").replace("°", " градусов")
    text = re.sub(r"(\d+)\s*km/h", r"\1 километров в час", text)
    text = text.replace("→", " ").replace("←", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


_SENTENCE = re.compile(r"[.!?…]+(?=\s|$)")


class Brain:
    """Диалог с Groq: память, быстрый потоковый ответ, вызов инструментов."""

    def __init__(self, model: str | None = None):
        self.key = load_key()
        self._requested_model = model or os.environ.get("JARVIS_MODEL", "").strip()
        self.model: str | None = None
        self.history: list[dict] = []
        self.last_error: str | None = None
        self._models_cache: list[str] | None = None

    @property
    def available(self) -> bool:
        return bool(self.key) and requests is not None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def _list_chat_models(self) -> list[str]:
        if self._models_cache is not None:
            return self._models_cache
        if not self.available:
            return []
        try:
            r = requests.get(f"{API}/models", headers=self._headers(), timeout=20)
            r.raise_for_status()
            ids = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception as e:  # noqa: BLE001
            self.last_error = f"список моделей: {e}"
            return []
        ids = [i for i in ids if i and not any(h in i for h in NON_CHAT_HINTS)]
        self._models_cache = ids
        return ids

    def resolve_model(self) -> str | None:
        if self.model:
            return self.model
        available = self._list_chat_models()
        if not available:
            return None
        if self._requested_model and self._requested_model in available:
            self.model = self._requested_model
        else:
            for candidate in PREFERRED_MODELS:
                if candidate in available:
                    self.model = candidate
                    break
            else:
                self.model = available[0]
        return self.model

    def reset(self) -> None:
        self.history.clear()

    # ------------------------------------------------------------ потоковый вызов
    def _stream(self, messages: list[dict], on_sentence=None, tools: bool = False) -> tuple[str, list]:
        model = self.resolve_model()
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0.5,
            "max_tokens": 250,
            "stream": True,
        }
        if "gpt-oss" in (model or ""):
            # У gpt-oss «размышления» по умолчанию долгие: low даёт 0.5–2.5 c вместо 3–6 c.
            payload["reasoning_effort"] = "low"
        if tools:
            payload["tools"] = TOOLS
            payload["tool_choice"] = "auto"

        r = requests.post(
            f"{API}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        r.encoding = "utf-8"  # Groq отдаёт UTF-8 без charset — иначе кириллица ломается
        if r.status_code != 200:
            return "", [{"error": f"HTTP {r.status_code}: {r.text[:200]}"}]

        parts: list[str] = []
        buffer = ""
        calls: dict[int, dict] = {}

        def flush_sentences(final: bool = False) -> None:
            nonlocal buffer
            if not on_sentence:
                return
            while True:
                m = _SENTENCE.search(buffer)
                if not m:
                    break
                piece = clean_for_speech(buffer[: m.end()])
                buffer = buffer[m.end():]
                if piece:
                    on_sentence(piece)
            if final:
                piece = clean_for_speech(buffer)
                buffer = ""
                if piece:
                    on_sentence(piece)

        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    parts.append(content)
                    buffer += content
                    flush_sentences()
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
        except Exception as e:  # noqa: BLE001
            self.last_error = f"поток: {type(e).__name__}: {e}"
            if not parts:
                return "", [{"error": str(e)}]
        finally:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass

        flush_sentences(final=True)
        text = "".join(parts).strip()
        call_list: list[dict] = []
        for idx in sorted(calls):
            slot = calls[idx]
            if not slot.get("name"):
                continue
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            call_list.append({"id": slot["id"] or f"call_{idx}", "name": slot["name"], "arguments": args})
        return text, call_list

    # ------------------------------------------------------------ основной вход
    def ask(self, text: str, on_sentence=None, executor=None) -> str:
        """Ответ модели. executor(имя, аргументы) -> строка; включает вызов инструментов."""
        text = (text or "").strip()
        if not text:
            return "Спроси что-нибудь."
        if not self.available:
            return "Мозг не подключён: нет ключа Groq."
        if not self.resolve_model():
            return f"Не смог получить список моделей Groq. {self.last_error or ''}".strip()

        messages: list[dict] = [
            {"role": "system", "content": PERSONA},
            *self.history,
            {"role": "user", "content": text},
        ]
        final_text = ""
        try:
            for _round in range(MAX_ROUNDS):
                final_text, calls = self._stream(
                    messages, on_sentence=on_sentence, tools=executor is not None
                )
                if not calls or executor is None:
                    break
                if calls and "error" in calls[0]:
                    return "Не получилось связаться с мозгом."
                # Модель просит инструменты: выполняем и возвращаем результаты.
                messages.append(
                    {
                        "role": "assistant",
                        "content": final_text or None,
                        "tool_calls": [
                            {
                                "id": c["id"],
                                "type": "function",
                                "function": {
                                    "name": c["name"],
                                    "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                                },
                            }
                            for c in calls
                        ],
                    }
                )
                for call in calls:
                    try:
                        result = executor(call["name"], call["arguments"])
                    except Exception as e:  # noqa: BLE001
                        result = f"ошибка выполнения: {e}"
                    print(f"[tool] {call['name']} -> {str(result)[:140]}")
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": str(result)[:2000]}
                    )
                on_sentence = on_sentence
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"
            return "Не получилось связаться с мозгом. Проверь интернет."

        answer = clean_for_speech(final_text) or "Готово."
        self.history.append({"role": "user", "content": text})
        self.history.append({"role": "assistant", "content": answer})
        del self.history[:-HISTORY_LIMIT]
        return answer


def main() -> int:
    _fix_stdio()
    args = sys.argv[1:]
    brain = Brain()
    if "--test" in args:
        print("== проверка мозга (Groq) ==")
        print(f"ключ: {'есть' if brain.available else 'НЕТ'}")
        print(f"модели: {', '.join(brain._list_chat_models()) or '—'}")
        print(f"выбрана: {brain.resolve_model() or '—'}")
        print(f"ответ: {brain.ask('Ответь одной короткой фразой: ты на связи?')}")
        return 0 if brain.model else 1
    if "--ask" in args:
        idx = args.index("--ask")
        question = " ".join(args[idx + 1:]).strip() or "привет"
        print(brain.ask(question))
        return 0
    print('Используй --test или --ask "вопрос".')
    return 0


if __name__ == "__main__":
    sys.exit(main())
