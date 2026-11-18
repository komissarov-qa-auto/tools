"""
Instagram — удаление своих комментариев (v4).

Страница: https://www.instagram.com/your_activity/interactions/comments/

По DOM из инспектора:
  кликабельный div (pointer-events: auto; cursor: pointer)
    └── div.wbloks_1 (pointer-events: none)
          └── span «Выбрать»

Все клики — через page.evaluate (Bloks UI плохо ловится Playwright-локаторами).
"""

import hashlib
import json
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

COMMENTS_URL = "https://www.instagram.com/your_activity/interactions/comments/"
PROFILE_DIR = Path("./instagram_chrome_profile")
LOG_FILE = Path("./instagram_comments_deleted.jsonl")
DEBUG_DIR = Path("./instagram_delete_debug")

MAX_ROUNDS = 500
BATCH_SIZE = 3
AFTER_DELETE_MS = 2600
SCROLL_PAUSE_MS = 1400
EMPTY_LIMIT = 12
FAIL_LIMIT = 15

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
                    "comment_snippet": snippet[:2000],
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
        page.screenshot(path=str(DEBUG_DIR / f"ig_{stamp}_{safe}.png"))
        (DEBUG_DIR / f"ig_{stamp}_{safe}.html").write_text(
            page.locator("body").inner_html(), encoding="utf-8"
        )
    except Exception:
        pass


def dismiss(page):
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass


# ---------- JS helpers (работают с Bloks) ----------

JS_CLICK_BY_TEXT = """
(texts) => {
  const wanted = texts.map(t => t.toLowerCase().trim());
  const forbid = /сортировк|фильтр|sort\\b|filter\\b|применить|apply|отмена|cancel/i;

  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    return true;
  }

  function clickableAncestor(el) {
    let cur = el;
    for (let i = 0; i < 8 && cur; i++) {
      const st = getComputedStyle(cur);
      if (st.pointerEvents !== 'none' && (st.cursor === 'pointer' || cur.getAttribute('role') === 'button' || cur.tagName === 'BUTTON')) {
        return cur;
      }
      cur = cur.parentElement;
    }
    return el;
  }

  const all = document.querySelectorAll('span, div, button, a, [role="button"]');
  for (const el of all) {
    if (!visible(el)) continue;
    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length > 40) continue;
    if (forbid.test(t)) continue;
    const low = t.toLowerCase();
    if (!wanted.some(w => low === w)) continue;
    const target = clickableAncestor(el);
    if (forbid.test((target.innerText || '').slice(0, 60))) continue;
    target.click();
    return { ok: true, text: t };
  }
  return { ok: false };
}
"""

JS_FIND_CHECKBOXES = """
() => {
  const results = [];
  const seen = new Set();

  // 1) aria-label checkbox / toggle
  const labels = [
    '[aria-label="Toggle checkbox"]',
    '[aria-label*="Toggle checkbox" i]',
    '[aria-label*="checkbox" i]',
    '[role="checkbox"]',
    '[data-testid="bulk_action_checkbox"]',
  ];
  for (const sel of labels) {
    document.querySelectorAll(sel).forEach((el, idx) => {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) return;
      // кликаем родителя-строку, если есть
      let target = el;
      let p = el.parentElement;
      for (let i = 0; i < 6 && p; i++) {
        const st = getComputedStyle(p);
        if (st.cursor === 'pointer' || p.getAttribute('role') === 'button') {
          target = p;
          break;
        }
        p = p.parentElement;
      }
      const text = (target.innerText || el.getAttribute('aria-label') || ('cb-' + idx)).replace(/\\s+/g, ' ').trim().slice(0, 400);
      if (/сортировк|фильтр|выбрать|select|удалить|delete/i.test(text) && text.length < 50) return;
      const key = text.slice(0, 120);
      if (seen.has(key)) return;
      seen.add(key);
      // помечаем элемент data-атрибутом для последующего клика
      const id = 'igdel_' + results.length;
      target.setAttribute('data-igdel-id', id);
      results.push({ id, text });
    });
  }

  // 2) Image with button rows
  document.querySelectorAll('[aria-label="Image with button"], [aria-label*="Image with button" i]').forEach((el, idx) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return;
    const text = (el.innerText || el.getAttribute('aria-label') || ('img-' + idx)).replace(/\\s+/g, ' ').trim().slice(0, 400);
    if (/сортировк|фильтр|выбрать|select/i.test(text) && text.length < 50) return;
    const key = text.slice(0, 120);
    if (seen.has(key)) return;
    seen.add(key);
    const id = 'igdel_' + results.length;
    el.setAttribute('data-igdel-id', id);
    results.push({ id, text });
  });

  return results;
}
"""

JS_CLICK_BY_ID = """
(id) => {
  const el = document.querySelector('[data-igdel-id="' + id + '"]');
  if (!el) return false;
  el.scrollIntoView({ block: 'center', behavior: 'instant' });
  el.click();
  return true;
}
"""

JS_CLICK_DELETE = """
() => {
  const forbid = /сортировк|фильтр|отмена|cancel|применить/i;
  // aria-label
  for (const sel of ['[aria-label="Delete"]', '[aria-label="Удалить"]', '[aria-label*="Delete" i]', '[aria-label*="Удалить" i]']) {
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const st = getComputedStyle(el);
      if (st.pointerEvents === 'none') continue;
      el.click();
      return { ok: true, via: 'aria' };
    }
  }
  // точный текст
  const wanted = ['удалить', 'delete'];
  for (const el of document.querySelectorAll('span, div, button, [role="button"]')) {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
    if (!t || t.length > 30) continue;
    if (forbid.test(t)) continue;
    if (!wanted.includes(t.toLowerCase())) continue;
    let target = el;
    let p = el.parentElement;
    for (let i = 0; i < 6 && p; i++) {
      const st = getComputedStyle(p);
      if (st.pointerEvents !== 'none' && (st.cursor === 'pointer' || p.getAttribute('role') === 'button' || p.tagName === 'BUTTON')) {
        target = p;
        break;
      }
      p = p.parentElement;
    }
    target.click();
    return { ok: true, via: 'text', text: t };
  }
  return { ok: false };
}
"""

JS_CONFIRM_DELETE = """
() => {
  const dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"]');
  for (const d of dialogs) {
    const r = d.getBoundingClientRect();
    if (r.width < 10) continue;
    for (const el of d.querySelectorAll('button, [role="button"], span, div')) {
      const t = (el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      if (!t || t.length > 30) continue;
      if (t.toLowerCase() === 'удалить' || t.toLowerCase() === 'delete') {
        let target = el;
        let p = el.parentElement;
        for (let i = 0; i < 5 && p && p !== d; i++) {
          if (p.getAttribute('role') === 'button' || p.tagName === 'BUTTON') {
            target = p;
            break;
          }
          p = p.parentElement;
        }
        target.click();
        return true;
      }
    }
  }
  return false;
}
"""

JS_IS_FILTER_OPEN = """
() => {
  for (const d of document.querySelectorAll('[role="dialog"]')) {
    const t = (d.innerText || '').toLowerCase();
    if (t.includes('сортировк') || t.includes('фильтр') || t.includes('sort') || t.includes('filter')) {
      return true;
    }
  }
  return false;
}
"""

JS_CLOSE_FILTER = """
() => {
  for (const sel of ['[aria-label="Закрыть"]', '[aria-label="Close"]', '[aria-label*="Close" i]', '[aria-label*="Закрыть" i]']) {
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width > 2 && r.height > 2) {
        el.click();
        return true;
      }
    }
  }
  // X внутри диалога фильтра
  for (const d of document.querySelectorAll('[role="dialog"]')) {
    const t = (d.innerText || '').toLowerCase();
    if (!(t.includes('сортировк') || t.includes('фильтр') || t.includes('sort'))) continue;
    for (const el of d.querySelectorAll('[aria-label*="Close" i], [aria-label*="Закрыть" i], svg')) {
      const r = el.getBoundingClientRect();
      if (r.width > 2) {
        el.click();
        return true;
      }
    }
  }
  return false;
}
"""

JS_HAS_DELETE_BTN = """
() => {
  for (const sel of ['[aria-label="Delete"]', '[aria-label="Удалить"]', '[aria-label*="Delete" i]', '[aria-label*="Удалить" i]']) {
    for (const el of document.querySelectorAll(sel)) {
      const r = el.getBoundingClientRect();
      if (r.width > 2 && r.height > 2) return true;
    }
  }
  for (const el of document.querySelectorAll('span, div, button, [role="button"]')) {
    const t = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (t === 'удалить' || t === 'delete') {
      const r = el.getBoundingClientRect();
      if (r.width > 2 && r.height > 2) return true;
    }
  }
  return false;
}
"""


def js_click_text(page, *texts) -> bool:
    try:
        res = page.evaluate(JS_CLICK_BY_TEXT, list(texts))
        return bool(res and res.get("ok"))
    except Exception:
        return False


def js_select_mode(page) -> bool:
    return js_click_text(page, "Выбрать", "Select", "Выделить")


def js_has_delete(page) -> bool:
    try:
        return bool(page.evaluate(JS_HAS_DELETE_BTN))
    except Exception:
        return False


def js_find_rows(page) -> list:
    try:
        return page.evaluate(JS_FIND_CHECKBOXES) or []
    except Exception:
        return []


def js_click_id(page, id_: str) -> bool:
    try:
        return bool(page.evaluate(JS_CLICK_BY_ID, id_))
    except Exception:
        return False


def js_delete(page) -> bool:
    try:
        res = page.evaluate(JS_CLICK_DELETE)
        return bool(res and res.get("ok"))
    except Exception:
        return False


def js_confirm(page) -> bool:
    try:
        return bool(page.evaluate(JS_CONFIRM_DELETE))
    except Exception:
        return False


def js_filter_open(page) -> bool:
    try:
        return bool(page.evaluate(JS_IS_FILTER_OPEN))
    except Exception:
        return False


def js_close_filter(page) -> bool:
    try:
        return bool(page.evaluate(JS_CLOSE_FILTER))
    except Exception:
        return False


def scroll(page):
    try:
        page.evaluate(
            """() => {
                (document.scrollingElement || document.documentElement).scrollBy(0, 550);
                const m = document.querySelector('main');
                if (m) m.scrollBy(0, 550);
            }"""
        )
        page.wait_for_timeout(SCROLL_PAUSE_MS)
    except Exception:
        pass


def main():
    print("=" * 68)
    print("INSTAGRAM — УДАЛЕНИЕ СВОИХ КОММЕНТАРИЕВ (v4, Bloks/JS)")
    print(COMMENTS_URL)
    print("=" * 68)
    print("Профиль:", PROFILE_DIR.resolve())
    print("Журнал:", LOG_FILE.resolve())
    print("Пачка:", BATCH_SIZE)
    print("\nУдаление необратимо.")

    done = read_done()
    deleted = failed = 0
    empty = fails = 0

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR.resolve()),
            executable_path=str(chrome_path()),
            headless=False,
            viewport={"width": 1280, "height": 960},
            locale="ru-RU",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(COMMENTS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        print("URL:", page.url)
        input(
            "Войди в IG при необходимости. "
            "Должен быть список комментариев. Нажми Enter..."
        )

        if "your_activity" not in page.url:
            page.goto(COMMENTS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

        print("Старт.")

        for rnd in range(1, MAX_ROUNDS + 1):
            page.wait_for_timeout(300)

            if js_filter_open(page):
                print(f"[{rnd}] Закрываю фильтр")
                js_close_filter(page)
                dismiss(page)
                page.wait_for_timeout(400)

            # Select mode
            if not js_has_delete(page):
                ok = js_select_mode(page)
                if not ok:
                    print(f"[{rnd}] «Выбрать» не найдена — скролл")
                    if rnd <= 3:
                        save_debug(page, "no_select")
                    scroll(page)
                    empty += 1
                    if empty >= EMPTY_LIMIT:
                        print("Не нахожу «Выбрать». Смотри instagram_delete_debug/")
                        break
                    continue
                page.wait_for_timeout(700)
                if js_filter_open(page):
                    print(f"[{rnd}] Открылся фильтр вместо Select — закрываю")
                    js_close_filter(page)
                    dismiss(page)
                    fails += 1
                    if fails >= FAIL_LIMIT:
                        break
                    continue

            page.wait_for_timeout(350)
            rows = js_find_rows(page)
            # фильтруем уже удалённые
            fresh = []
            for r in rows:
                key = fp(r["text"][:500])
                if key not in done:
                    fresh.append((r, key))

            if not fresh:
                empty += 1
                print(f"[{rnd}] Нет строк для отметки ({empty})")
                dismiss(page)
                scroll(page)
                if empty >= EMPTY_LIMIT:
                    save_debug(page, "empty_rows")
                    break
                continue

            empty = 0
            batch = fresh[:BATCH_SIZE]
            print("\n" + "-" * 60)
            print(f"[{rnd}] Отмечаю {len(batch)}:")
            keys, snippets = [], []
            for r, key in batch:
                print(" ·", r["text"][:120].replace("\n", " "))
                if js_click_id(page, r["id"]):
                    keys.append(key)
                    snippets.append(r["text"][:350])
                    page.wait_for_timeout(200)

            if not keys:
                print("[ОШИБКА] Не удалось кликнуть строки")
                save_debug(page, "click_rows_fail")
                fails += 1
                if fails >= FAIL_LIMIT:
                    break
                continue

            page.wait_for_timeout(400)
            if not js_delete(page):
                print("[ОШИБКА] Delete не найден")
                save_debug(page, "no_delete")
                dismiss(page)
                failed += 1
                fails += 1
                if fails >= FAIL_LIMIT:
                    break
                continue

            page.wait_for_timeout(500)
            js_confirm(page)
            page.wait_for_timeout(AFTER_DELETE_MS)

            if js_filter_open(page):
                js_close_filter(page)
                dismiss(page)

            for k, sn in zip(keys, snippets):
                write_log(k, sn, "deleted_confirmed")
                done.add(k)
            deleted += len(keys)
            fails = 0
            print(f"[УДАЛЕНО] +{len(keys)}  всего={deleted}  ошибок={failed}")
            scroll(page)

        print("\n" + "=" * 68)
        print("ГОТОВО  удалено≈", deleted, "  ошибок=", failed)
        print("Журнал:", LOG_FILE.resolve())
        input("Enter — закрыть...")
        ctx.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ОШИБКА:", type(e).__name__, e)
        input("Enter...")
