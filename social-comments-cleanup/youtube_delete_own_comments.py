"""
Удаление своих комментариев из Google «Мои действия»
(YouTube → Комментарии).

Быстрая + надёжная версия:
- ищет aria-label «Удалить действие …»
- почти без проверки visibility (виртуализация часто даёт 0x0)
- кликает через Playwright locator (надёжнее evaluate)
- без ввода DELETE
"""

import hashlib
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ACTIVITY_URL = (
    "https://myactivity.google.com/page"
    "?utm_source=my-activity"
    "&hl=ru"
    "&page=youtube_comments"
)

PROFILE_DIR = Path("./youtube_chrome_profile")
LOG_FILE = Path("./youtube_comments_deleted.jsonl")
DEBUG_DIR = Path("./youtube_delete_debug")

MAX_ROUNDS = 1000
AFTER_DELETE_MS = 400
SCROLL_PAUSE_MS = 500
EMPTY_LIMIT = 25
NO_PROGRESS_LIMIT = 30
BATCH_CLICK = 4

def chrome_path():
    """Найти установленный Google Chrome в Windows или macOS.

    При нестандартной установке можно задать переменную окружения CHROME_PATH.
    """
    import os
    import shutil
    import sys

    override = os.environ.get("CHROME_PATH")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        raise FileNotFoundError(
            f"CHROME_PATH задан, но файл не найден: {candidate}"
        )

    candidates = []
    if sys.platform == "win32":
        candidates.extend([
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
            / "Google/Chrome/Application/chrome.exe",
        ])
        executable_names = ["chrome.exe", "chrome"]
    elif sys.platform == "darwin":
        candidates.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ])
        executable_names = ["google-chrome", "chrome"]
    else:
        candidates.extend([
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
        ])
        executable_names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    for name in executable_names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()

    if sys.platform == "darwin":
        hint = "Установите Google Chrome в /Applications или задайте CHROME_PATH."
    elif sys.platform == "win32":
        hint = "Установите Google Chrome или задайте переменную CHROME_PATH."
    else:
        hint = "Установите Google Chrome/Chromium или задайте переменную CHROME_PATH."
    raise FileNotFoundError(f"Не удалось найти исполняемый файл Chrome. {hint}")


def fp(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def read_done() -> set:
    s = set()
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:
                o = json.loads(line)
                if o.get("status") == "deleted_confirmed" and o.get("fingerprint"):
                    s.add(o["fingerprint"])
            except Exception:
                pass
    return s


def write_log(key: str, snippet: str, status: str):
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "fingerprint": key,
                    "comment_snippet": snippet[:2500],
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": status,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def save_debug(page, label: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", label)[:40]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"yt_{stamp}_{safe}.png"))
        (DEBUG_DIR / f"yt_{stamp}_{safe}.html").write_text(
            page.locator("body").inner_html(), encoding="utf-8"
        )
    except Exception:
        pass


def dismiss(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(60)
    except Exception:
        pass


def is_junk(snippet: str) -> bool:
    s = re.sub(r"\s+", " ", (snippet or "")).strip()
    if len(s) < 1:
        return True
    if JUNK_SNIPPET_RE.match(s):
        return True
    return False


def diagnose(page):
    try:
        info = page.evaluate(JS_DIAG) or {}
    except Exception as e:
        print("diagnose error:", e)
        return
    print("\n=== ДИАГНОСТИКА ===")
    print(f"  кнопок с aria-label: {info.get('totalWithAria')}")
    print(f"  из них с «удал/delete/действие»: {info.get('withUdalit')}")
    samples = info.get("samples") or []
    for it in samples:
        mark = ""
        if "удалить действие" in (it.get("aria") or "").lower():
            mark = "  << КРЕСТИК"
        print(f"  {it.get('w')}x{it.get('h')} vis={it.get('visibility')} aria={it.get('aria')!r}{mark}")
    print("===================\n")


def scroll(page, px=450):
    try:
        page.evaluate(f"() => window.scrollBy(0, {px})")
        page.wait_for_timeout(SCROLL_PAUSE_MS)
    except Exception:
        pass


def click_by_id(page, id_: str) -> bool:
    """Клик через evaluate (быстро)."""
    try:
        return bool(page.evaluate("""
            (id) => {
              const el = document.querySelector('[data-ytdel-x="' + id + '"]');
              if (!el) return false;
              el.scrollIntoView({ block: 'center', behavior: 'instant' });
              el.click();
              return true;
            }
        """, id_))
    except Exception:
        return False


def click_by_aria(page, aria: str) -> bool:
    """Запасной клик через Playwright locator по точному aria-label."""
    try:
        loc = page.locator(f'button[aria-label="{aria}"]').first
        if loc.count() == 0:
            # пробуем частичное
            loc = page.locator('button[aria-label*="Удалить действие"]').first
        if loc.count() == 0:
            return False
        loc.scroll_into_view_if_needed(timeout=2000)
        loc.click(timeout=2500, force=True)
        return True
    except Exception:
        return False


def main():
    print("=" * 64)
    print("МОИ ДЕЙСТВИЯ — удаление комментариев YouTube (v4)")
    print(ACTIVITY_URL)
    print("=" * 64)
    print("Профиль:", PROFILE_DIR.resolve())
    print("Журнал :", LOG_FILE.resolve())
    print()

    done = read_done()
    deleted = failed = 0
    empty = no_progress = 0

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            executable_path=str(chrome_path()),
            headless=False,
            viewport={"width": 1400, "height": 1000},
            locale="ru-RU",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(ACTIVITY_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        print("URL:", page.url)
        print(
            "Войди при необходимости.\n"
            "Открой: Мои действия → YouTube → Комментарии\n"
            "чтобы были видны строки с крестиками X."
        )
        input("Enter, когда страница готова... ")

        if "myactivity" not in page.url:
            page.goto(ACTIVITY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

        diagnose(page)
        save_debug(page, "start")
        print("Старт...\n")

        for rnd in range(1, MAX_ROUNDS + 1):
            dismiss(page)

            try:
                items = page.evaluate(JS_FIND_X) or []
            except Exception as e:
                print("JS error:", e)
                items = []

            targets = []
            for d in items:
                if is_junk(d.get("snippet", "")):
                    continue
                key = fp(d["snippet"][:500])
                if key in done:
                    continue
                targets.append((d, key))
                if len(targets) >= BATCH_CLICK:
                    break

            if not targets:
                empty += 1
                if empty <= 4 or empty % 5 == 0:
                    print(f"[{rnd}] Нет крестиков ({empty})  [JS нашёл всего: {len(items)}]")
                if empty in (1, 2, 5, 12):
                    diagnose(page)
                    save_debug(page, f"empty_{empty}")
                scroll(page)
                if empty >= EMPTY_LIMIT:
                    print(
                        "\nСтоп: крестики не находятся.\n"
                        "Пришли вывод === ДИАГНОСТИКА === "
                        "(особенно строки с << КРЕСТИК)."
                    )
                    break
                continue

            empty = 0
            print("-" * 56)
            print(f"[{rnd}] к удалению: {len(targets)}  (JS всего нашёл: {len(items)})")

            round_ok = 0
            for data, key in targets:
                print(" ·", data["snippet"][:110].replace("\n", " "))

                ok = click_by_id(page, data["id"])
                if not ok:
                    ok = click_by_aria(page, data.get("aria") or "")

                page.wait_for_timeout(AFTER_DELETE_MS)
                dismiss(page)

                if ok:
                    write_log(key, data["snippet"], "deleted_confirmed")
                    done.add(key)
                    deleted += 1
                    round_ok += 1
                    print(f"  [УДАЛЕНО]  всего={deleted}")
                else:
                    failed += 1
                    print("  [ОШИБКА клика]")
                    save_debug(page, "fail")

            if round_ok == 0:
                no_progress += 1
                if no_progress >= NO_PROGRESS_LIMIT:
                    print("Долго нет прогресса — стоп.")
                    break
            else:
                no_progress = 0

            try:
                page.evaluate("() => window.scrollBy(0, 120)")
            except Exception:
                pass
            page.wait_for_timeout(150)

        print("\n" + "=" * 64)
        print("ГОТОВО  удалено =", deleted, "  ошибок =", failed)
        print("Журнал:", LOG_FILE.resolve())
        input("Enter — закрыть...")
        ctx.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ОШИБКА:", type(e).__name__, e)
        input("Enter...")
