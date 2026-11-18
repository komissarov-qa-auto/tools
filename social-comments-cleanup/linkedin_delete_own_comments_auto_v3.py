"""
LinkedIn — автоматическое удаление СВОИХ комментариев (v3).

Главные исправления относительно v2:
1. Обрабатываем ТОЛЬКО ОДИН комментарий за итерацию.
   После удаления (или неудачи) заново ищем кнопки «⋯».
   Старые локаторы после перерисовки ленты больше не используются.
2. Более гибкий поиск пункта «Удалить» в выпадающем меню
   (не только fullmatch, но и contains + data-view-name).
3. Подтверждение в диалоге предпочитаем по
   data-view-name="comment-submit-delete".
4. Лучшие паузы и закрытие «залипших» меню.
"""

import hashlib
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Укажите адрес своего профиля LinkedIn и имя владельца аккаунта.
# Перед публикацией оставьте только шаблонные значения — не добавляйте личные данные.
COMMENTS_URL = (
    "https://www.linkedin.com/in/YOUR-LINKEDIN-PROFILE/"
    "recent-activity/comments/"
)
OWNER_NAME = ""  # Например: "Имя Фамилия"; можно оставить пустым.
PROFILE_DIR = Path("./linkedin_chrome_profile")
LOG_FILE = Path("./linkedin_comments_deleted.jsonl")
DEBUG_DIR = Path("./linkedin_delete_debug")
MAX_SCROLL_ROUNDS = 120
SCROLL_PAUSE_MS = 2000
NO_CHANGE_LIMIT = 8
# Сколько раз подряд «не нашли ни одной кнопки» / «ничего не удалили» — выход
EMPTY_ROUNDS_LIMIT = 5

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


def fp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_done() -> set:
    done = set()
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                if obj.get("fingerprint") and obj.get("status") == "deleted_confirmed":
                    done.add(obj["fingerprint"])
            except Exception:
                pass
    return done


def write_log(key: str, label: str, text: str, status: str):
    item = {
        "fingerprint": key,
        "menu_label": label,
        "comment_text": text[:3000],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")


def save_debug(page, label: str):
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", label, flags=re.UNICODE)[:50]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(
            path=str(DEBUG_DIR / f"menu_{stamp}_{safe}.png"),
            full_page=False,
        )
        (DEBUG_DIR / f"menu_{stamp}_{safe}.html").write_text(
            page.locator("body").inner_html(),
            encoding="utf-8",
        )
    except Exception:
        pass


def dismiss_overlays(page):
    """Закрыть открытые меню/диалоги, если они мешают."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    except Exception:
        pass


def get_own_menu_buttons(page):
    """
    Найти видимые кнопки «⋯» только у своих комментариев.
    Возвращает список (locator, aria-label).
    Каждый раз вызывается заново — не храним старые локаторы.
    """
    found = []
    seen = set()

    # 1) по aria-label
    for selector in MENU_SELECTORS:
        loc = page.locator(selector)
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            btn = loc.nth(i)
            try:
                if not btn.is_visible():
                    continue
                label = (btn.get_attribute("aria-label") or "").strip()
                # Для data-view-name="comment-control-menu" aria-label может быть пустым
                # или на другом языке — проверяем наличие имени владельца, если label есть.
                if OWNER_NAME and label and OWNER_NAME.casefold() not in label.casefold():
                    # Если имя задано — отсекаем кнопки, не принадлежащие владельцу.
                    continue
                rect = btn.evaluate(
                    "el => { const r = el.getBoundingClientRect(); "
                    "return [Math.round(r.top), Math.round(r.left)].join(':'); }"
                )
                key = (label or selector, rect)
                if key in seen:
                    continue
                seen.add(key)
                found.append((btn, label or selector))
            except Exception:
                continue

    return found


def get_card_text(button) -> str:
    """Текст карточки комментария (для fingerprint и лога)."""
    try:
        ancestors = button.locator("xpath=ancestor::div")
        count = min(ancestors.count(), 25)
        candidates = []
        for i in range(count):
            node = ancestors.nth(i)
            try:
                if not node.is_visible():
                    continue
                txt = node.inner_text(timeout=800).strip()
                compact = re.sub(r"\s+", " ", txt)
                low = compact.casefold()
                if (
                    ("оставил(а) комментарий" in low
                     or "left a comment" in low
                     or "commented on" in low)
                    and 30 <= len(compact) <= 20000
                ):
                    candidates.append((len(compact), compact))
            except Exception:
                continue
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return candidates[0][1]
    except Exception:
        pass
    return ""


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def find_delete_action(container, soft: bool = False):
    """
    Найти кликабельный элемент «Удалить» / «Delete» внутри container.
    soft=True — достаточно, чтобы текст СОДЕРЖАЛ слово.
    """
    selectors = [
        '[role="menuitem"]',
        'button',
        '[role="button"]',
        '[tabindex="0"]',
        'div[role="button"]',
        'li',
    ]
    pattern = DELETE_SOFT_RE if soft else DELETE_EXACT_RE
    for sel in selectors:
        loc = container.locator(sel)
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if not el.is_visible() or not el.is_enabled():
                    continue
                txt = _normalize(el.inner_text(timeout=400) or "")
                aria = _normalize(el.get_attribute("aria-label") or "")
                if soft:
                    if pattern.search(txt) or pattern.search(aria):
                        # Не путать с «Отмена» и т.п.
                        if re.search(r"отмена|cancel|dismiss", txt + " " + aria, re.I):
                            continue
                        return el
                else:
                    if pattern.fullmatch(txt) or pattern.fullmatch(aria):
                        return el
            except Exception:
                continue
    return None


def click_delete_in_opened_menu(page) -> bool:
    """После клика по ⋯ найти и нажать «Удалить» в выпадающем меню."""
    # Ждём появления меню
    try:
        page.wait_for_function(
            """() => {
                const els = [...document.querySelectorAll(
                    '[role=menu], [role=menuitem], [role=listbox], .artdeco-dropdown__content'
                )];
                return els.some(e => e.offsetParent !== null);
            }""",
            timeout=4000,
        )
    except Exception:
        pass

    page.wait_for_timeout(300)

    # 1) role=menu
    menus = page.locator('[role="menu"]')
    try:
        for i in range(menus.count()):
            m = menus.nth(i)
            if not m.is_visible():
                continue
            item = find_delete_action(m, soft=False) or find_delete_action(m, soft=True)
            if item:
                item.click()
                return True
    except Exception:
        pass

    # 2) типичные контейнеры LinkedIn dropdown
    for sel in [
        ".artdeco-dropdown__content-inner",
        ".artdeco-dropdown__content",
        '[role="listbox"]',
        ".artdeco-dropdown",
    ]:
        containers = page.locator(sel)
        try:
            for i in range(containers.count()):
                c = containers.nth(i)
                if not c.is_visible():
                    continue
                item = find_delete_action(c, soft=False) or find_delete_action(c, soft=True)
                if item:
                    item.click()
                    return True
        except Exception:
            continue

    # 3) крайний fallback: любой видимый элемент с текстом «Удалить»/Delete,
    #    который появился недавно (высокий z-index / в портале)
    try:
        candidates = page.locator("button, [role='menuitem'], [role='button'], div[tabindex='0']")
        n = min(candidates.count(), 80)
        for i in range(n):
            el = candidates.nth(i)
            try:
                if not el.is_visible():
                    continue
                txt = _normalize(el.inner_text(timeout=300) or "")
                aria = _normalize(el.get_attribute("aria-label") or "")
                combined = txt + " " + aria
                if DELETE_EXACT_RE.fullmatch(txt) or DELETE_EXACT_RE.fullmatch(aria):
                    if not re.search(r"отмена|cancel", combined, re.I):
                        el.click()
                        return True
            except Exception:
                continue
        # soft
        for i in range(n):
            el = candidates.nth(i)
            try:
                if not el.is_visible():
                    continue
                txt = _normalize(el.inner_text(timeout=300) or "")
                if DELETE_SOFT_RE.search(txt) and not re.search(r"отмена|cancel", txt, re.I):
                    # только короткие тексты — чтобы не кликнуть «Удалить из ленты» и т.п. случайно
                    if len(txt) < 40:
                        el.click()
                        return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def confirm_delete_dialog(page) -> bool:
    """Нажать «Удалить» в модальном окне подтверждения."""
    try:
        page.wait_for_function(
            """() => [...document.querySelectorAll('[role=dialog], [data-testid=dialog]')]
                .some(e => e.offsetParent !== null)""",
            timeout=5000,
        )
    except Exception:
        pass

    page.wait_for_timeout(350)

    # Самый надёжный способ — data-view-name
    try:
        btn = page.locator('[data-view-name="comment-submit-delete"]')
        if btn.count() > 0 and btn.first.is_visible():
            btn.first.click()
            return True
    except Exception:
        pass

    # По role=dialog + текст
    dialogs = page.locator('[role="dialog"], [data-testid="dialog"]')
    try:
        for i in range(dialogs.count()):
            dialog = dialogs.nth(i)
            if not dialog.is_visible():
                continue
            # сначала точное
            item = find_delete_action(dialog, soft=False)
            if item:
                item.click()
                return True
            # потом мягкое
            item = find_delete_action(dialog, soft=True)
            if item:
                item.click()
                return True
    except Exception:
        pass

    return False


def try_load_more(page):
    try:
        more = page.get_by_role("button", name=LOAD_MORE_RE)
        for j in range(more.count()):
            m = more.nth(j)
            if m.is_visible() and m.is_enabled():
                m.click()
                page.wait_for_timeout(SCROLL_PAUSE_MS)
                return True
    except Exception:
        pass
    return False


def main():
    print("=" * 68)
    print("LINKEDIN — АВТОУДАЛЕНИЕ СОБСТВЕННЫХ КОММЕНТАРИЕВ (v3)")
    print("Исправлено: один комментарий за раз + повторный поиск кнопок")
    print("=" * 68)
    print("Страница:", COMMENTS_URL)
    print("Профиль Chrome:", PROFILE_DIR.resolve())
    print("Журнал:", LOG_FILE.resolve())
    print("\nВНИМАНИЕ: удаление необратимо.")

    done = read_done()
    deleted = failed = skipped = 0
    previous_height = -1
    empty_rounds = 0
    unchanged = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            executable_path=str(chrome_path()),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(COMMENTS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        print("Текущий URL:", page.url)
        input(
            "Если нужно — войди в LinkedIn. "
            "Когда откроется страница своих комментариев — нажми Enter здесь..."
        )
        if "/recent-activity/comments" not in page.url:
            page.goto(COMMENTS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
        if "/recent-activity/comments" not in page.url:
            print("Не удалось подтвердить страницу комментариев. Остановка.")
            context.close()
            return

        for round_no in range(1, MAX_SCROLL_ROUNDS + 1):
            page.wait_for_timeout(600)
            dismiss_overlays(page)

            buttons = get_own_menu_buttons(page)
            if not buttons:
                empty_rounds += 1
                print(f"[{round_no}] Кнопок «⋯» своих комментариев не видно ({empty_rounds})")
                try_load_more(page)
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(SCROLL_PAUSE_MS)
                if empty_rounds >= EMPTY_ROUNDS_LIMIT:
                    print("Долго нет кнопок — выходим.")
                    break
                continue

            empty_rounds = 0

            # --- берём ТОЛЬКО первую подходящую кнопку, которой ещё нет в done ---
            target_btn = None
            target_label = ""
            target_key = ""
            target_card = ""

            for btn, label in buttons:
                try:
                    if not btn.is_visible():
                        continue
                    current_label = (btn.get_attribute("aria-label") or label or "").strip()
                    if OWNER_NAME and OWNER_NAME.casefold() not in current_label.casefold() and "comment-control-menu" not in current_label:
                        # data-view-name кнопки без имени — проверяем карточку
                        pass
                    card_text = get_card_text(btn)
                    key = fp(current_label + "|" + re.sub(r"\s+", " ", card_text).strip())
                    if key in done:
                        skipped += 1
                        continue
                    target_btn = btn
                    target_label = current_label
                    target_key = key
                    target_card = card_text
                    break
                except Exception:
                    continue

            if target_btn is None:
                # Все видимые уже в done → скроллим дальше
                print(f"[{round_no}] Все видимые комментарии уже обработаны, скролл…")
                try_load_more(page)
                h1 = page.evaluate("() => document.body.scrollHeight")
                page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(SCROLL_PAUSE_MS)
                h2 = page.evaluate("() => document.body.scrollHeight")
                if h2 <= previous_height:
                    unchanged += 1
                else:
                    unchanged = 0
                previous_height = max(h1, h2)
                if unchanged >= NO_CHANGE_LIMIT:
                    print("Новые элементы больше не появляются.")
                    break
                continue

            # --- удаляем один комментарий ---
            print("\n" + "-" * 68)
            print(f"[{round_no}] СВОЙ КОММЕНТАРИЙ — удаляю")
            print("Меню:", target_label)
            print("Фрагмент:", (target_card or "")[:500])

            try:
                # 1) открыть меню ⋯
                target_btn.scroll_into_view_if_needed()
                page.wait_for_timeout(200)
                target_btn.click()
                page.wait_for_timeout(500)

                # 2) пункт «Удалить»
                if not click_delete_in_opened_menu(page):
                    print("[ОШИБКА] После ⋯ не найден пункт «Удалить».")
                    save_debug(page, target_label or "no_delete_item")
                    dismiss_overlays(page)
                    failed += 1
                    # небольшой скролл, чтобы не зациклиться на том же элементе
                    page.evaluate("() => window.scrollBy(0, 200)")
                    page.wait_for_timeout(800)
                    continue

                page.wait_for_timeout(500)

                # 3) подтверждение в диалоге
                if not confirm_delete_dialog(page):
                    print("[ОШИБКА] Диалог подтверждения не найден / не нажат.")
                    save_debug(page, target_label or "no_confirm")
                    dismiss_overlays(page)
                    failed += 1
                    page.evaluate("() => window.scrollBy(0, 200)")
                    page.wait_for_timeout(800)
                    continue

                # успех
                page.wait_for_timeout(1500)
                write_log(target_key, target_label, target_card, "deleted_confirmed")
                done.add(target_key)
                deleted += 1
                print("[УДАЛЕНО] Подтверждение нажато.")
                # даём LinkedIn убрать комментарий из DOM
                page.wait_for_timeout(1200)
                dismiss_overlays(page)

            except Exception as exc:
                print("[ОШИБКА ОБРАБОТКИ]", type(exc).__name__, str(exc)[:300])
                save_debug(page, "exception")
                dismiss_overlays(page)
                failed += 1
                page.wait_for_timeout(800)

            # после каждого удаления — лёгкий скролл / load more
            try_load_more(page)
            h1 = page.evaluate("() => document.body.scrollHeight")
            page.evaluate("() => window.scrollBy(0, 400)")
            page.wait_for_timeout(600)
            previous_height = max(previous_height, h1)

            print(
                f"Итого: удалено {deleted}, ошибок {failed}, "
                f"пропущено (уже было) {skipped}"
            )

        print("\n" + "=" * 68)
        print("ЗАВЕРШЕНО")
        print("Удалений с подтверждением:", deleted)
        print("Ошибок:", failed)
        print("Журнал:", LOG_FILE.resolve())
        if failed:
            print("Отладка (скрин + html):", DEBUG_DIR.resolve())
        input("Нажми Enter для закрытия браузера...")
        context.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("КРИТИЧЕСКАЯ ОШИБКА:", type(e).__name__, str(e))
        input("Нажми Enter для выхода...")
