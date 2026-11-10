#!/usr/bin/env python3
"""Jarvis-like голосовой ассистент для Windows.

Режимы:
    python assistant.py            — голосовой режим (микрофон)
    python assistant.py --console  — текстовый режим (команды с клавиатуры)
    python assistant.py --test     — самопроверка окружения
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "libs"))

import requests  # noqa: E402  (лежит в vendored libs)

CONFIG_PATH = os.path.join(BASE, "commands.json")

# Порядок важен: большая модель понимает речь заметно лучше маленькой.
MODEL_CANDIDATES = (
    os.path.join(BASE, "models", "vosk-model-ru-0.42"),
    os.path.join(BASE, "models", "vosk-model-ru-0.22"),
    os.path.join(BASE, "models", "vosk-model-small-ru-0.22"),
)


def pick_model_dir() -> str:
    """Выбирает лучшую установленную модель распознавания."""
    for path in MODEL_CANDIDATES:
        if os.path.exists(os.path.join(path, "am", "final.mdl")):
            return path
    return MODEL_CANDIDATES[-1]


MODEL_DIR = pick_model_dir()
SAMPLE_RATE = 16000
WAKE_WORDS = (
    "джарвис", "джарвис,", "jarvis", "дарвис", "чарвис", "жарвис",
    "джервис", "джарвиз", "джорвис", "jarvis,",
)

VK = {
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "volume_mute": 0xAD,
    "media_next": 0xB0,
    "media_prev": 0xB1,
    "media_play_pause": 0xB3,
}
KEYEVENTF_KEYUP = 0x0002


# ---------------------------------------------------------------- синтез речи
PS_TTS_SCRIPT = r"""
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $ru = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'ru-*' } | Select-Object -First 1
    if ($ru) { $s.SelectVoice($ru.VoiceInfo.Name) }
} catch { }
while ($null -ne ($line = [Console]::In.ReadLine())) {
    if ($line.Trim().Length -gt 0) {
        try { $s.Speak($line) } catch { }
    }
}
"""


class Speaker:
    """Озвучка через Windows Speech (SAPI) в отдельном процессе-мосту.

    pyttsx3 не подходит: требует pywin32. Поэтому держим один фоновый
    powershell.exe и пишем ему строки в stdin — без задержек на запуск.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.proc = None
        self.lock = threading.Lock()
        if enabled:
            try:
                self.proc = subprocess.Popen(  # noqa: S603
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", PS_TTS_SCRIPT],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._alive = True
            except Exception as e:  # noqa: BLE001
                print(f"[tts] недоступен ({e}), ответы будут только в консоли")
                self.proc = None

    @property
    def engine(self):
        """Совместимость с самопроверкой: движок есть, если поднялся мост."""
        return self.proc

    def say(self, text: str, wait: bool = False) -> None:
        print(f"[Джарвис] {text}")
        if self.proc is not None and self.proc.poll() is None:
            try:
                with self.lock:
                    self.proc.stdin.write((text.replace("\n", " ") + "\n").encode("utf-8"))
                    self.proc.stdin.flush()
            except Exception as e:  # noqa: BLE001
                print(f"[tts] ошибка: {e}")
        if wait:
            time.sleep(min(8.0, 0.075 * len(text) + 0.5))

    def stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------- действия
def press_vk(code: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


def open_path(path: str) -> bool:
    try:
        if path.startswith(("http://", "https://")):
            webbrowser.open(path)
        else:
            os.startfile(path)  # noqa: S606
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[action] не удалось открыть {path}: {e}")
        return False


def shutdown_pc() -> str:
    try:
        subprocess.Popen(["shutdown.exe", "/s", "/t", "5"])
        return "Выключаю компьютер. До связи."
    except Exception as e:  # noqa: BLE001
        return f"Не получилось выключить: {e}"


def lock_pc() -> str:
    ctypes.windll.user32.LockWorkStation()
    return "Блокирую компьютер."


def screenshot() -> str:
    script = os.path.join(BASE, "tools", "screenshot.ps1")
    out = os.path.join(os.path.expanduser("~"), "Pictures", "jarvis-screenshot.png")
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Out", out],
            check=True,
            capture_output=True,
            timeout=25,
        )
        return "Скриншот сохранён в Изображения."
    except Exception as e:  # noqa: BLE001
        return f"Скриншот не удался: {e}"


# ---------------------------------------------------------------- команды
class CommandEngine:
    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.commands = data.get("commands", [])
        self.apps = data.get("apps", {})
        self.sites = data.get("sites", {})

    @staticmethod
    def normalize(text: str) -> str:
        text = text.lower().replace("ё", "е")
        text = re.sub(r"[^0-9a-zа-я\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def match(self, text: str):
        """Возвращает (команда, слова после фразы)."""
        norm = self.normalize(text)
        best = None
        for cmd in self.commands:
            for phrase in cmd.get("phrases", []):
                p = self.normalize(phrase)
                if not p:
                    continue
                if p in norm:
                    tail = norm.split(p, 1)[1].strip()
                    if best is None or len(p) > best[1]:
                        best = (cmd, len(p), tail)
        if best is None:
            return None, ""
        return best[0], best[2]

    def run(self, cmd: dict, tail: str, speaker: Speaker) -> None:
        action = cmd.get("action", "")
        reply = cmd.get("reply", "")
        try:
            if action == "open_app":
                name = cmd.get("target") or tail
                path = self.apps.get(name)
                if path and open_path(path):
                    speaker.say(reply or "Открываю.")
                else:
                    speaker.say(f"Не знаю программу {name}.")
            elif action == "open_site":
                name = cmd.get("target") or tail
                url = self.sites.get(name, name if "." in name else None)
                if url and open_path(url):
                    speaker.say(reply or "Открываю.")
                else:
                    speaker.say("Не знаю такой сайт.")
            elif action == "search_web":
                query = tail or cmd.get("target", "")
                if query:
                    open_path("https://yandex.ru/search/?text=" + query.replace(" ", "+"))
                    speaker.say(reply or "Ищу.")
                else:
                    speaker.say("Что искать?")
            elif action == "url":
                open_path(cmd["target"])
                speaker.say(reply or "Готово.")
            elif action == "volume_up":
                press_vk(VK["volume_up"], cmd.get("times", 3))
                speaker.say(reply or "Громче.")
            elif action == "volume_down":
                press_vk(VK["volume_down"], cmd.get("times", 3))
                speaker.say(reply or "Тише.")
            elif action == "volume_mute":
                press_vk(VK["volume_mute"])
                speaker.say(reply or "Звук выключен.")
            elif action == "media_play_pause":
                press_vk(VK["media_play_pause"])
                speaker.say(reply or "Готово.")
            elif action == "media_next":
                press_vk(VK["media_next"])
                speaker.say(reply or "Следующий.")
            elif action == "media_prev":
                press_vk(VK["media_prev"])
                speaker.say(reply or "Предыдущий.")
            elif action == "screenshot":
                speaker.say(screenshot())
            elif action == "lock":
                speaker.say(lock_pc())
            elif action == "shutdown":
                speaker.say(reply or "Выключаю через пять секунд.")
                time.sleep(1.0)
                speaker.say(shutdown_pc())
            elif action == "time":
                now = datetime.datetime.now()
                speaker.say(f"Сейчас {now.strftime('%H:%M')}.")
            elif action == "date":
                months = ("января февраля марта апреля мая июня июля августа "
                          "сентября октября ноября декабря").split()
                now = datetime.datetime.now()
                speaker.say(f"Сегодня {now.day} {months[now.month - 1]} {now.year} года.")
            elif action == "repeat":
                speaker.say(tail or "Нечего повторять.")
            elif action == "say":
                speaker.say(cmd.get("target", reply or "Готово."))
            elif action == "stop":
                speaker.say(reply or "Останавливаюсь.")
                sys.exit(0)
            else:
                speaker.say(f"Команда {action} пока не реализована.")
        except Exception as e:  # noqa: BLE001
            speaker.say(f"Ошибка при выполнении: {e}")


# ---------------------------------------------------------------- напоминания
class Reminders:
    """Голосовые напоминания: «через N минут сказать …». Переживают перезапуск."""

    PATH = os.path.join(BASE, "reminders.json")

    def __init__(self, speaker: Speaker):
        self.speaker = speaker
        self.items: list[dict] = []
        self.lock = threading.Lock()
        self._load()
        threading.Thread(target=self._run, daemon=True).start()

    def _load(self) -> None:
        try:
            with open(self.PATH, encoding="utf-8") as f:
                data = json.load(f)
            self.items = [i for i in data if isinstance(i, dict) and "due" in i and "text" in i]
        except Exception:  # noqa: BLE001
            self.items = []

    def _save(self) -> None:
        try:
            with self.lock:
                data = list(self.items)
            with open(self.PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass

    def add(self, minutes: float, text: str) -> str:
        try:
            minutes = max(0.05, float(minutes))
        except (TypeError, ValueError):
            minutes = 10.0
        with self.lock:
            self.items.append({"due": time.time() + minutes * 60, "text": str(text)})
        self._save()
        mins = f"{minutes:g}".replace(".", ",")
        return f"напомню через {mins} мин: {text}"

    def list_text(self) -> str:
        with self.lock:
            items = sorted(self.items, key=lambda i: i["due"])
        if not items:
            return "активных напоминаний нет"
        parts = []
        for it in items[:6]:
            left = max(0, int(it["due"] - time.time()))
            parts.append(f"{it['text']} — через {left // 60} мин")
        return "; ".join(parts)

    def _run(self) -> None:
        while True:
            time.sleep(1)
            now = time.time()
            due: list[dict] = []
            with self.lock:
                for it in list(self.items):
                    if it["due"] <= now:
                        due.append(it)
                        self.items.remove(it)
            if due:
                self._save()
                for it in due:
                    self.speaker.say(f"Напоминаю: {it['text']}")


def _boost_quiet(data: bytes) -> bytes:
    """Поднимает громкость тихой речи, чтобы Vosk не терял слова."""
    import array
    import math

    samples = array.array("h")
    samples.frombytes(data)
    if not samples:
        return data
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    if rms >= 350 or rms <= 1:
        return data
    gain = min(6.0, 900.0 / rms)
    for i, value in enumerate(samples):
        samples[i] = max(-32768, min(32767, int(value * gain)))
    return samples.tobytes()


def _split_speech(text: str, limit: int = 260, max_chunks: int = 12) -> list[str]:
    words = text.split()
    chunks: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > limit:
            if current:
                chunks.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        chunks.append(current)
    return chunks[:max_chunks]


# ---------------------------------------------------------------- инструменты модели
class Actions:
    """Что модель может СДЕЛАТЬ: исполнение её tool-call'ов."""

    def __init__(self, engine: CommandEngine, speaker: Speaker, reminders: Reminders):
        self.engine = engine
        self.speaker = speaker
        self.reminders = reminders
        self.voice = True

    def __call__(self, name: str, args: dict) -> str:
        try:
            return self._run(name, args or {})
        except Exception as e:  # noqa: BLE001
            return f"не получилось: {e}"

    def _run(self, name: str, args: dict) -> str:
        if name == "current_datetime":
            now = datetime.datetime.now()
            months = ("января февраля марта апреля мая июня июля августа "
                      "сентября октября ноября декабря").split()
            weekdays = ("понедельник", "вторник", "среда", "четверг",
                        "пятница", "суббота", "воскресенье")
            return (f"{weekdays[now.weekday()]}, {now.day} {months[now.month - 1]} "
                    f"{now.year} года, {now.strftime('%H:%M')}")

        if name == "open_app":
            app_name = str(args.get("name", "")).strip()
            path = self._find_app(app_name)
            if path and open_path(path):
                return f"открыл {app_name}"
            return f"не знаю программу {app_name}"

        if name == "open_site":
            site = str(args.get("site", "")).strip()
            url = self._find_site(site)
            if url and open_path(url):
                return f"открыл {site}"
            return f"не знаю сайт {site}"

        if name == "web_search":
            from urllib.parse import quote_plus

            query = str(args.get("query", "")).strip()
            if not query:
                return "пустой поисковый запрос"
            open_path("https://ya.ru/search/?text=" + quote_plus(query))
            return f"открыл поиск по запросу «{query}»"

        if name == "weather":
            return self._weather(str(args.get("city", "")).strip())

        if name == "set_reminder":
            return self.reminders.add(args.get("minutes", 10), str(args.get("text", "напоминание")))

        if name == "list_reminders":
            return self.reminders.list_text()

        if name == "read_clipboard":
            text = self._clipboard()
            if not text:
                return "буфер обмена пуст"
            if self.voice:
                for chunk in _split_speech(text):
                    self.speaker.say(chunk)
                return "прочитал текст из буфера обмена вслух"
            return f"в буфере обмена: {text[:600]}"

        if name == "media":
            mapping = {
                "play_pause": "media_play_pause",
                "next": "media_next",
                "prev": "media_prev",
                "volume_up": "volume_up",
                "volume_down": "volume_down",
                "volume_mute": "volume_mute",
            }
            key = mapping.get(str(args.get("action", "")))
            if not key:
                return "неизвестное действие с медиа"
            press_vk(VK[key], 3 if key in ("volume_up", "volume_down") else 1)
            return "готово"

        if name == "screenshot":
            return screenshot()

        if name == "lock_pc":
            return lock_pc()

        return f"инструмент {name} не поддерживается"

    # ---------------------------------------------------------- вспомогательное
    def _find_app(self, app_name: str) -> str | None:
        norm = CommandEngine.normalize(app_name)
        if not norm:
            return None
        for key, path in self.engine.apps.items():
            key_norm = CommandEngine.normalize(key)
            if key_norm and (key_norm == norm or key_norm in norm or norm in key_norm):
                return path
        return None

    def _find_site(self, site: str) -> str | None:
        from urllib.parse import quote_plus

        norm = CommandEngine.normalize(site)
        for key, url in self.engine.sites.items():
            key_norm = CommandEngine.normalize(key)
            if key_norm and norm and (key_norm == norm or key_norm in norm or norm in key_norm):
                return url
        if "." in site and " " not in site:
            return site if site.startswith(("http://", "https://")) else "https://" + site
        if norm:
            return "https://ya.ru/search/?text=" + quote_plus(site)
        return None

    def _weather(self, city: str) -> str:
        from urllib.parse import quote

        city = city.strip() or "Москва"
        try:
            r = requests.get(
                f"https://wttr.in/{quote(city)}",
                params={"format": "%l: %C, %t, ветер %w", "lang": "ru"},
                headers={"User-Agent": "curl/8.0"},
                timeout=15,
            )
            r.encoding = "utf-8"
            r.raise_for_status()
            line = " ".join(r.text.split())
        except Exception as e:  # noqa: BLE001
            return f"не смог узнать погоду: {e}"
        return line or "погода недоступна"

    @staticmethod
    def _clipboard() -> str:
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "Get-Clipboard -Raw"
        )
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                capture_output=True,
                timeout=10,
            )
            return out.stdout.decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------- распознавание
def _ascii_destinations() -> list[str]:
    """Куда можно положить модель, чтобы путь был без кириллицы.

    Порядок: сначала пользовательские папки (всегда доступны на запись),
    затем стандартные общие пути и только потом временная папка.
    """
    env = os.environ
    candidates: list[str] = []
    for var in ("LOCALAPPDATA", "APPDATA", "PUBLIC", "PROGRAMDATA"):
        base = env.get(var, "").strip()
        if base:
            candidates.append(os.path.join(base, "JarvisModel"))
    # Литеральные запасные пути на случай, если переменные окружения пустые.
    candidates += [
        r"C:\Users\Public\JarvisModel",
        r"C:\ProgramData\JarvisModel",
    ]
    try:
        candidates.append(os.path.join(os.path.expanduser("~"), "AppData", "Local", "JarvisModel"))
    except Exception:  # noqa: BLE001
        pass
    try:
        import tempfile

        candidates.append(os.path.join(tempfile.gettempdir(), "JarvisModel"))
    except Exception:  # noqa: BLE001
        pass
    candidates.append(os.path.join(env.get("SystemDrive", "C:") + os.sep, "JarvisModel"))

    # Отбрасываем кириллицу и дубликаты: Vosk её не умеет читать.
    seen: set[str] = set()
    result: list[str] = []
    for path in candidates:
        key = path.lower()
        if path.isascii() and key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _short_ascii_path(path: str) -> str:
    """Короткий 8.3-путь Windows: `C:\\88A7~1\\E473~1\\...` вместо кириллицы.

    Vosk не читает юникод-пути, но короткий путь всегда латинский и указывает
    на те же файлы — копировать 90 МБ модели не нужно.
    """
    if os.name != "nt":
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 32768)
        if not n:
            return ""
        short = buf.value
        return short if short.isascii() else ""
    except Exception:  # noqa: BLE001
        return ""


def resolve_model_path(path: str) -> str:
    """Возвращает путь к модели, по которому Vosk сможет её открыть.

    Порядок: обычный путь (если латинский) → короткий 8.3-путь (без копирования)
    → копия в латинскую папку (если 8.3 на диске отключён).
    """
    if path.isascii():
        return path

    short = _short_ascii_path(path)
    if short and os.path.exists(os.path.join(short, "am", "final.mdl")):
        return short

    return _copy_model_to_ascii(path)


def _copy_model_to_ascii(path: str) -> str:
    """Копия модели в латинскую папку — когда короткие имена на диске выключены."""

    import hashlib
    import shutil

    name = "jarvis-model-" + hashlib.md5(path.encode("utf-8")).hexdigest()[:8]
    required = (
        os.path.join("am", "final.mdl"),
        os.path.join("conf", "model.conf"),
        os.path.join("graph", "HCLr.fst"),
    )

    def valid(folder: str) -> bool:
        return os.path.exists(os.path.join(folder, ".ready")) and all(
            os.path.exists(os.path.join(folder, rel)) for rel in required
        )

    for root in _ascii_destinations():
        dst = os.path.join(root, name)
        try:
            os.makedirs(root, exist_ok=True)
            if valid(dst):
                return dst
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            print(f"[stt] копирую модель в ASCII-путь: {dst} (один раз, ~90 МБ)")
            shutil.copytree(path, dst)
            with open(os.path.join(dst, ".ready"), "w", encoding="utf-8") as f:
                f.write(path)
            if valid(dst):
                return dst
        except Exception as e:  # noqa: BLE001
            print(f"[stt] не удалось использовать путь {dst}: {e}")
    print("[stt] не нашёл ASCII-путь для модели — Vosk может её не открыть.")
    return path


# ---------------------------------------------------------------- микрофон
def list_input_devices(sd) -> list[tuple[int, str, str, int]]:
    """(индекс, hostapi, имя, каналов) для всех устройств ввода."""
    apis = sd.query_hostapis()
    result: list[tuple[int, str, str, int]] = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            api = apis[dev["hostapi"]]["name"]
            result.append((idx, api, dev["name"], dev["max_input_channels"]))
    return result


def print_input_devices(sd) -> None:
    try:
        default_in = sd.default.device[0]
    except Exception:  # noqa: BLE001
        default_in = None
    print("Микрофоны (индекс | API | имя | каналы):")
    for idx, api, name, ch in list_input_devices(sd):
        mark = "   <- системный по умолчанию" if idx == default_in else ""
        print(f"  {idx:>3} | {api:<20} | {name} | {ch}{mark}")
    print('Выбрать: python assistant.py --mic <индекс>  (или запиши индекс в mic.txt)')


def _mic_setting() -> str | None:
    """Явный выбор микрофона: env JARVIS_MIC или файл mic.txt (индекс или часть имени)."""
    value = os.environ.get("JARVIS_MIC", "").strip()
    if not value:
        try:
            with open(os.path.join(BASE, "mic.txt"), encoding="utf-8-sig") as f:
                value = " ".join(f.read().split())
        except OSError:
            return None
    return value or None


def _device_opens(sd, idx: int) -> bool:
    try:
        stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=8000, dtype="int16", channels=1, device=idx
        )
        stream.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def pick_input_device(sd, prefer: str | None = None) -> int | None:
    """Рабочий микрофон: явный выбор → внешний (fifine и т.п.) → системный по умолчанию.

    «По умолчанию» в Windows часто оказывается гарнитура или мышь без сигнала,
    поэтому внешний USB-микрофон пробуем раньше системного выбора.
    """
    devices = sd.query_devices()
    inputs = [i for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    if not inputs:
        return None

    def name(i: int) -> str:
        return devices[i].get("name", "").lower()

    setting = (prefer or _mic_setting() or "").strip()
    if setting:
        try:
            idx = int(setting)
            if idx in inputs and _device_opens(sd, idx):
                return idx
        except ValueError:
            low = setting.lower()
            for i in inputs:
                if low in name(i) and _device_opens(sd, i):
                    return i

    for i in inputs:
        if "fifine" in name(i) and _device_opens(sd, i):
            return i

    try:
        default_in = sd.default.device[0]
    except Exception:  # noqa: BLE001
        default_in = None
    if default_in in inputs and _device_opens(sd, default_in):
        return default_in

    for i in inputs:
        if _device_opens(sd, i):
            return i
    return None


# ---------------------------------------------------------------- облачный STT
CLOUD_STT_MODELS = ("whisper-large-v3-turbo", "whisper-large-v3")
_STT_OVERRIDE: str | None = None


def _stt_mode() -> str:
    """cloud | vosk | auto (auto = облако, если есть ключ Groq)."""
    if _STT_OVERRIDE:
        return _STT_OVERRIDE
    value = os.environ.get("JARVIS_STT", "").strip().lower()
    if value in ("cloud", "vosk"):
        return value
    return "auto"


def cloud_stt_available() -> bool:
    try:
        from brain import load_key

        return bool(load_key())
    except Exception:  # noqa: BLE001
        return False


def _wav_bytes(pcm: bytes) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# Whisper на тишине/шуме любит выдумывать титры — такие «фразы» отбрасываем.
_HALLUCINATIONS = (
    "продолжение следует",
    "субтитры",
    "редактор субтитров",
    "спасибо за просмотр",
    "подпишитесь на канал",
    "продолжение в следующей",
    "amara.org",
    "dimatorzok",
)


def _is_hallucination(text: str) -> bool:
    low = text.lower()
    return len(text.strip()) < 2 or any(h in low for h in _HALLUCINATIONS)


def transcribe_cloud(pcm: bytes) -> str | None:
    """Точное распознавание через Groq Whisper. None — сеть или ключ подвели."""
    if len(pcm) < SAMPLE_RATE:  # меньше ~0.5 c звука
        return None
    try:
        from brain import API, load_key

        key = load_key()
        if not key:
            return None
        wav = _wav_bytes(pcm)
        for model in CLOUD_STT_MODELS:
            try:
                r = requests.post(
                    f"{API}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": ("audio.wav", wav, "audio/wav")},
                    data={
                        "model": model,
                        "language": "ru",
                        "response_format": "json",
                        "temperature": 0,
                    },
                    timeout=40,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[stt] сеть: {type(e).__name__}: {e}")
                return None
            if r.status_code == 200:
                text = (r.json().get("text") or "").strip()
                return "" if _is_hallucination(text) else text
            if r.status_code in (400, 404):
                continue  # этой модели нет — пробуем следующую
            print(f"[stt] {model}: HTTP {r.status_code}: {r.text[:120]}")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[stt] облако недоступно: {e}")
        return None


class UtteranceRecorder:
    """Режет поток на фразы по тишине — для облачного распознавания.

    Порог подстраивается под уровень шума в комнате: не доверяем абсолютной
    громкости, сравниваем с фоном.
    """

    def __init__(
        self,
        floor: int = 160,
        multiplier: float = 2.2,
        silence_blocks: int = 4,
        min_voiced: int = 3,
        max_blocks: int = 96,
    ):
        self.floor = floor
        self.multiplier = multiplier
        self.silence_blocks = silence_blocks
        self.min_voiced = min_voiced
        self.max_blocks = max_blocks
        self.noise = 60.0
        self.parts: list[bytes] = []
        self.voiced = 0
        self.silent = 0
        self.active = False

    def reset(self) -> None:
        self.parts = []
        self.voiced = 0
        self.silent = 0
        self.active = False

    def feed(self, block: bytes) -> bytes | None:
        import array
        import math

        samples = array.array("h")
        samples.frombytes(block)
        rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
        threshold = max(self.floor, self.noise * self.multiplier)

        if rms >= threshold:
            self.voiced += 1
            self.silent = 0
            self.active = True
            self.parts.append(block)
        elif self.active:
            self.parts.append(block)
            self.silent += 1
            if self.silent >= self.silence_blocks:
                return self._finish()
        else:
            # Пока не говорим — запоминаем уровень шума комнаты.
            self.noise = 0.92 * self.noise + 0.08 * rms

        if len(self.parts) >= self.max_blocks:
            return self._finish()
        return None

    def _finish(self) -> bytes | None:
        pcm = b"".join(self.parts)
        enough = self.voiced >= self.min_voiced
        self.reset()
        return pcm if enough else None


def _dispatch_transcript(
    text: str, engine: CommandEngine, speaker: Speaker, listening: bool
) -> bool:
    """Общая обработка распознанной фразы. Возвращает новое состояние «слушаю»."""
    text = (text or "").strip()
    if not text:
        return False
    print(f"[вы] {text}")
    tail = has_wake_word(text)
    if tail or text.lower().startswith(("джарвис", "jarvis")):
        if tail:
            handle_text(tail, engine, speaker, voice=True)
            return False
        speaker.say("Слушаю.")
        return True
    if listening:
        handle_text(text, engine, speaker, voice=True)
    return False


def has_wake_word(text: str) -> str:
    norm = text.lower()
    for w in WAKE_WORDS:
        if w in norm:
            idx = norm.find(w)
            return norm[idx + len(w):].strip(" ,")
    return ""


def listen_loop(engine: CommandEngine, speaker: Speaker, mic_preference: str | None = None) -> None:
    import json as _json

    import sounddevice as sd
    import vosk

    if not os.path.isdir(MODEL_DIR):
        speaker.say("Модель распознавания не найдена. Запустите tools/getmodel.py.")
        return

    mode = _stt_mode()
    use_cloud = mode == "cloud" or (mode == "auto" and cloud_stt_available())
    if mode == "vosk":
        use_cloud = False

    print("[stt] загружаю модель...")
    resolved = resolve_model_path(MODEL_DIR)
    try:
        model = vosk.Model(resolved)
    except Exception as e:  # noqa: BLE001
        print(f"[stt] модель не загрузилась: {e}")
        speaker.say(
            "Не получилось загрузить модель распознавания речи. "
            "Подробности в консоли, попробую позже."
        )
        return
    rec = vosk.KaldiRecognizer(model, SAMPLE_RATE)
    recorder = UtteranceRecorder()
    audio_q: queue.Queue = queue.Queue()

    def callback(indata, frames, t, status):  # noqa: ARG001
        if status:
            print(f"[stt] {status}")
        audio_q.put(bytes(indata))

    device = pick_input_device(sd, prefer=mic_preference)
    if device is None:
        print("[stt] рабочий микрофон не найден.")
        print_input_devices(sd)
        speaker.say("Не нашёл рабочий микрофон. Проверь, что он подключён.")
        return
    try:
        device_name = sd.query_devices(device)["name"]
    except Exception:  # noqa: BLE001
        device_name = str(device)
    print(f"[stt] микрофон #{device}: {device_name}")
    print(
        "[stt] распознавание: "
        + ("Groq Whisper (облако, точнее)" if use_cloud else "Vosk (офлайн)")
    )
    print("[stt] слушаю — скажи «Джарвис, ...»")

    speaker.say("Джарвис на связи.")
    listening = False

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE,
        blocksize=4000,
        dtype="int16",
        channels=1,
        device=device,
        callback=callback,
    ):
        while True:
            data = audio_q.get()

            if use_cloud:
                utterance = recorder.feed(data)
                if utterance is None:
                    continue
                text = transcribe_cloud(utterance)
                if text is None:  # облако не ответило — распознаём локально
                    rec.AcceptWaveform(utterance)
                    text = _json.loads(rec.FinalResult()).get("text", "").strip()
                listening = _dispatch_transcript(text, engine, speaker, listening)
                continue

            data = _boost_quiet(data)
            if rec.AcceptWaveform(data):
                text = _json.loads(rec.Result()).get("text", "").strip()
                listening = _dispatch_transcript(text, engine, speaker, listening)
            else:
                partial = _json.loads(rec.PartialResult()).get("partial", "")
                if partial and has_wake_word(partial):
                    pass  # ждём конца фразы


_BRAIN = None  # экземпляр brain.Brain, создаётся в main()
_ACTIONS = None  # экземпляр Actions — что модель может делать
_REMINDERS = None  # экземпляр Reminders


def handle_text(text: str, engine: CommandEngine, speaker: Speaker, voice: bool = False) -> None:
    cmd, tail = engine.match(text)
    if cmd is not None:
        engine.run(cmd, tail, speaker)
        return

    low = CommandEngine.normalize(text)
    if low in ("сбрось контекст", "забудь разговор", "новый разговор", "сбросить контекст"):
        if _BRAIN is not None:
            _BRAIN.reset()
        speaker.say("Хорошо, начинаем разговор с чистого листа.")
        return

    if _BRAIN is not None and _BRAIN.available:
        if _ACTIONS is not None:
            _ACTIONS.voice = voice
        spoken: list[str] = []

        def on_sentence(piece: str) -> None:
            # Озвучиваем по предложениям, не дожидаясь всего ответа.
            spoken.append(piece)
            speaker.say(piece)

        answer = _BRAIN.ask(text, on_sentence=on_sentence if voice else None, executor=_ACTIONS)
        if not spoken:
            speaker.say(answer)
    else:
        speaker.say("Не понял команду, а мозг не подключён.")


# ---------------------------------------------------------------- утилиты
def selftest() -> int:
    print("== самопроверка ==")
    print(f"Python: {sys.version.split()[0]}")
    ok = True
    try:
        import vosk  # noqa: F401

        print("vosk: OK")
    except Exception as e:  # noqa: BLE001
        print(f"vosk: НЕТ ({e})")
        ok = False
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        print(f"микрофоны: {len(inputs)}")
        for d in inputs[:5]:
            print(f"  - {d['name']}")
    except Exception as e:  # noqa: BLE001
        print(f"sounddevice: НЕТ ({e})")
        ok = False
    print(f"модель: {'OK' if os.path.isdir(MODEL_DIR) else 'НЕТ ' + MODEL_DIR}")
    try:
        eng = CommandEngine(CONFIG_PATH)
        print(f"команд: {len(eng.commands)}, программ: {len(eng.apps)}, сайтов: {len(eng.sites)}")
    except Exception as e:  # noqa: BLE001
        print(f"commands.json: ошибка ({e})")
        ok = False
    try:
        import pyttsx3

        sp = Speaker()
        print(f"синтез речи: {'OK' if sp.engine else 'нет движка'}")
    except Exception as e:  # noqa: BLE001
        print(f"синтез речи: НЕТ ({e})")
    try:
        from brain import Brain

        b = Brain()
        print(f"мозг Groq: {'ключ есть' if b.available else 'НЕТ ключа (jarvis/.groq-key.txt)'}")
    except Exception as e:  # noqa: BLE001
        print(f"мозг Groq: НЕТ ({e})")
        ok = False
    print(f"STT-режим: {'Groq Whisper (облако)' if cloud_stt_available() else 'Vosk (офлайн)'}")
    return 0 if ok else 1


def console_loop(engine: CommandEngine, speaker: Speaker) -> None:
    speaker.say("Текстовый режим. Введите команду или 'выход'.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        if line.lower() in ("выход", "exit", "quit", "q"):
            return
        handle_text(line, engine, speaker)


def stdin_loop(engine: CommandEngine, speaker: Speaker) -> None:
    """Печать с клавиатуры в том же окне, где работает голос."""
    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        if line.lower() in ("выход", "exit", "quit", "q"):
            speaker.say("Отключаюсь.")
            time.sleep(0.4)
            os._exit(0)
        print(f"[вы, текст] {line}")
        handle_text(line, engine, speaker, voice=True)


def hybrid_loop(engine: CommandEngine, speaker: Speaker, mic_preference: str | None = None) -> None:
    """Голос и клавиатура одновременно: микрофон слушает, печать тоже работает."""
    print("[text] можно печатать вопросы прямо в это окно ('выход' — закрыть)")
    reader = threading.Thread(target=stdin_loop, args=(engine, speaker), daemon=True)
    reader.start()
    try:
        listen_loop(engine, speaker, mic_preference)
    except KeyboardInterrupt:
        speaker.say("Отключаюсь.")
        return
    if reader.is_alive():
        print("[text] микрофон недоступен — можно продолжать писать текстом.")
        reader.join()


def main() -> int:
    global _BRAIN, _ACTIONS, _REMINDERS, _STT_OVERRIDE
    args = sys.argv[1:]
    if "--test" in args:
        return selftest()
    if "--devices" in args:
        import sounddevice as sd

        print_input_devices(sd)
        return 0
    if "--stt" in args:
        idx = args.index("--stt")
        value = args[idx + 1].strip().lower() if idx + 1 < len(args) else ""
        if value not in ("cloud", "vosk"):
            print("Использование: python assistant.py --stt cloud|vosk")
            return 2
        _STT_OVERRIDE = value
    try:
        from brain import Brain

        _BRAIN = Brain()
        model = _BRAIN.resolve_model()
        print(f"[brain] Groq: {model or 'модель недоступна'}")
    except Exception as e:  # noqa: BLE001
        print(f"[brain] не подключён: {e}")
        _BRAIN = None
    speaker = Speaker()
    engine = CommandEngine(CONFIG_PATH)
    _REMINDERS = Reminders(speaker)
    _ACTIONS = Actions(engine, speaker, _REMINDERS)
    if "--ask" in args:
        idx = args.index("--ask")
        question = " ".join(args[idx + 1:]).strip()
        if not question:
            print('Использование: python assistant.py --ask "вопрос"')
            return 2
        if _BRAIN is None or not _BRAIN.available:
            print("Мозг не подключён: нет ключа Groq (jarvis/.groq-key.txt).")
            return 1
        answer = _BRAIN.ask(question)
        print(answer)
        return 0
    if "--console" in args:
        console_loop(engine, speaker)
        return 0
    mic = None
    if "--mic" in args:
        idx = args.index("--mic")
        mic = args[idx + 1] if idx + 1 < len(args) else None
        if not mic:
            print('Использование: python assistant.py --mic <индекс>  (список: --devices)')
            return 2
    try:
        hybrid_loop(engine, speaker, mic)
    except KeyboardInterrupt:
        speaker.say("Отключаюсь.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
