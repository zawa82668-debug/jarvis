"""Скачивает русскую модель Vosk и распаковывает её в models/.

    python tools/getmodel.py            # маленькая модель (45 МБ, быстро, меньше точность)
    python tools/getmodel.py --big      # большая модель (1.8 ГБ, заметно лучше понимает речь)
    python tools/getmodel.py --model <имя>
"""
import os
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")

URL_BASE = "https://alphacephei.com/vosk/models/"
NAME = "vosk-model-small-ru-0.22"  # значение по умолчанию
BIG_NAME = "vosk-model-ru-0.42"


def _retry(fn, attempts=6, delay=2.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  сбой сети ({i + 1}/{attempts}): {type(e).__name__}: {e}")
            time.sleep(delay * (i + 1))
    raise last


def main():
    name = NAME
    args = sys.argv[1:]
    if "--big" in args:
        name = BIG_NAME
    if "--model" in args:
        idx = args.index("--model")
        if idx + 1 < len(args):
            name = args[idx + 1]
    url = URL_BASE + name + ".zip"

    os.makedirs(MODELS, exist_ok=True)
    target = os.path.join(MODELS, name)
    if os.path.isdir(target):
        print(f"модель уже распакована: {target}")
        return 0

    zip_path = os.path.join(MODELS, name + ".zip")
    if not (os.path.exists(zip_path) and os.path.getsize(zip_path) > 1_000_000):
        print(f"скачиваю {url}")

        def go():
            with urllib.request.urlopen(url, timeout=300) as r, open(zip_path, "wb") as f:
                total = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total % (50 << 20) < (1 << 20):
                        print(f"  {total // (1 << 20)} MiB")

        _retry(go)
        print(f"скачано {os.path.getsize(zip_path) // (1 << 20)} MiB")

    print("распаковываю...")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(MODELS)
    print(f"готово: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
