"""Скачивает колёса с PyPI и распаковывает их в libs/ (обход pip в песочнице)."""
import json
import os
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIBS = os.path.join(ROOT, "libs")
WHEELDIR = os.path.join(ROOT, "wheels")

# порядок внутри списка: сначала платформенные колёса, потом универсальные
PACKAGES = {
    "vosk": ["py3-none-win_amd64.whl"],
    "sounddevice": ["py3-none-win_amd64.whl", "py3-none-any.whl"],
    "cffi": ["cp314-win_amd64.whl"],
    "pycparser": ["py3-none-any.whl"],
    "srt": ["py3-none-any.whl"],
    "tqdm": ["py3-none-any.whl"],
    "websockets": ["cp314-win_amd64.whl", "py3-none-any.whl"],
    "requests": ["py3-none-any.whl"],
    "urllib3": ["py3-none-any.whl"],
    "idna": ["py3-none-any.whl"],
    "certifi": ["py3-none-any.whl"],
    "charset-normalizer": ["cp314-win_amd64.whl", "py3-none-any.whl"],
    "pyttsx3": ["py3-none-any.whl"],
    "comtypes": ["py3-none-any.whl"],
}


def _retry(fn, attempts=6, delay=1.5):
    """Повторяет сетевую операцию: соединение с PyPI периодически рвётся."""
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  сбой сети ({i + 1}/{attempts}): {type(e).__name__}")
            time.sleep(delay * (i + 1))
    raise last


def pypi_json(pkg):
    def go():
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30) as r:
            return json.load(r)

    return _retry(go)


def pick(pkg, suffixes):
    data = pypi_json(pkg)
    urls = data["urls"]
    for suffix in suffixes:
        for u in urls:
            if u["filename"].endswith(suffix):
                return u
    return None


def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  уже есть: {os.path.basename(dest)}")
        return dest

    def go():
        with urllib.request.urlopen(url, timeout=180) as r:
            return r.read()

    blob = _retry(go)
    with open(dest, "wb") as f:
        f.write(blob)
    print(f"  скачано: {os.path.basename(dest)} ({len(blob) // 1024} KiB)")
    return dest


def extract(wheel, libs):
    with zipfile.ZipFile(wheel) as z:
        for n in z.namelist():
            z.extract(n, libs)


def main():
    os.makedirs(LIBS, exist_ok=True)
    os.makedirs(WHEELDIR, exist_ok=True)
    failed = []
    for pkg, suffixes in PACKAGES.items():
        try:
            u = pick(pkg, suffixes)
        except Exception as e:  # noqa: BLE001
            print(f"[!] {pkg}: метаданные недоступны ({type(e).__name__})")
            failed.append(pkg)
            continue
        if not u:
            print(f"[!] {pkg}: подходящее колесо не найдено")
            failed.append(pkg)
            continue
        print(f"{pkg}: {u['filename']}")
        try:
            path = download(u["url"], os.path.join(WHEELDIR, u["filename"]))
            extract(path, LIBS)
        except Exception as e:  # noqa: BLE001
            print(f"[!] {pkg}: ошибка загрузки ({type(e).__name__}: {e})")
            failed.append(pkg)
    print("не установлено: " + (", ".join(failed) if failed else "нет"))


if __name__ == "__main__":
    main()
    sys.exit(0)
