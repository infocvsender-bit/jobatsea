import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright
from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "")

TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "@Cvsendler_bot")

SECTIONS = [
    "https://jobatsea.online/jobs/Engine_Officers/",
    "https://jobatsea.online/jobs/Engine_Ratings/",
    "https://jobatsea.online/jobs/Deck_Officers/",
    "https://jobatsea.online/jobs/Deck_Ratings/",
    "https://jobatsea.online/jobs/Catering_Staff/",
    "https://jobatsea.online/jobs/Offshore/",
]

SENT_FILE = Path(os.getenv("SENT_FILE", "sent_jobs.json"))

EMAIL_REGEX = r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"

SCAN_INTERVAL_SECONDS = 3600
REQUEST_DELAY_SECONDS = 2


# ============================================================
# MEMORY
# ============================================================

def load_memory() -> set[str]:
    """Загружает ссылки на уже отправленные вакансии."""
    if not SENT_FILE.exists():
        return set()

    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))

        if isinstance(data, list):
            return set(data)

    except (json.JSONDecodeError, OSError) as error:
        print(f"Не удалось загрузить файл памяти: {error}")

    return set()


def save_memory(sent_jobs: set[str]) -> None:
    """Сохраняет ссылки на отправленные вакансии."""
    try:
        SENT_FILE.write_text(
            json.dumps(
                sorted(sent_jobs),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as error:
        print(f"Не удалось сохранить файл памяти: {error}")


# ============================================================
# PARSING
# ============================================================

def extract_emails(text: str) -> list[str]:
    """Извлекает уникальные e-mail-адреса из текста."""
    emails = re.findall(EMAIL_REGEX, text)

    result = []
    seen = set()

    for email in emails:
        email = email.strip(".,;:()[]<>").lower()

        if email not in seen:
            seen.add(email)
            result.append(email)

    return result


async def get_job_links(page, section_url: str) -> list[str]:
    """Получает ссылки на вакансии в разделе."""
    links = await page.eval_on_selector_all(
        "a[href]",
        """
        elements => elements
            .map(element => element.href)
            .filter(href => href.includes('/job/'))
        """,
    )

    result = []

    for link in links:
        absolute_link = urljoin(section_url, link)

        if absolute_link not in result:
            result.append(absolute_link)

    return result


async def get_job_details(page, job_url: str) -> dict | None:
    """Открывает вакансию и возвращает данные, если найден e-mail."""
    try:
        await page.goto(
            job_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        await page.wait_for_timeout(1000)

        text = await page.locator("body").inner_text()

    except Exception as error:
        print(f"Ошибка при открытии вакансии {job_url}: {error}")
        return None

    text = text.strip()

    if not text:
        return None

    emails = extract_emails(text)

    # Отправляем только вакансии, где найден хотя бы один e-mail.
    if not emails:
        return None

    return {
        "text": text[:3500],
        "emails": emails,
    }


# ============================================================
# SCANNING
# ============================================================

async def scan_section(
    page,
    section_url: str,
    sent_jobs: set[str],
    client: TelegramClient,
) -> None:
    """Обходит страницы одного раздела."""
    page_number = 1

    while True:
        page_url = f"{section_url}?p={page_number}"

        print(f"Сканирование: {page_url}")

        try:
            await page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            await page.wait_for_timeout(1000)

        except Exception as error:
            print(f"Ошибка при открытии раздела {page_url}: {error}")
            break

        job_links = await get_job_links(page, page_url)

        if not job_links:
            print(f"Вакансии не найдены, раздел завершён: {section_url}")
            break

        new_links_found = False

        for job_link in job_links:
            if job_link in sent_jobs:
                continue

            new_links_found = True
            print(f"Проверка вакансии: {job_link}")

            job_data = await get_job_details(page, job_link)

            if job_data is None:
                continue

            message = (
                "JobAtSea\n\n"
                f"{job_data['text']}\n\n"
                f"E-mail: {', '.join(job_data['emails'])}"
            )

            try:
                await client.send_message(
                    TELEGRAM_TARGET,
                    message,
                    link_preview=False,
                )

                sent_jobs.add(job_link)
                save_memory(sent_jobs)

                print(f"Вакансия отправлена: {job_link}")

                await asyncio.sleep(REQUEST_DELAY_SECONDS)

            except Exception as error:
                print(f"Ошибка отправки в Telegram: {error}")

        # Если на странице были только уже обработанные ссылки,
        # можно перейти к следующей странице. Остановка произойдёт,
        # когда страница станет пустой.
        if not new_links_found:
            print(f"Страница уже обработана: {page_url}")

        page_number += 1
        await asyncio.sleep(1)


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    if not TELEGRAM_API_ID:
        raise ValueError("Не задана переменная TELEGRAM_API_ID")

    if not TELEGRAM_API_HASH:
        raise ValueError("Не задана переменная TELEGRAM_API_HASH")

    if not TELEGRAM_SESSION:
        raise ValueError("Не задана переменная TELEGRAM_SESSION")

    sent_jobs = load_memory()

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )

    print("Подключение к Telegram...")
    await client.start()
    print(f"Telegram подключён. Получатель: {TELEGRAM_TARGET}")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
        )

        page = await browser.new_page()

        try:
            while True:
                for section_url in SECTIONS:
                    await scan_section(
                        page=page,
                        section_url=section_url,
                        sent_jobs=sent_jobs,
                        client=client,
                    )

                print(
                    f"Сканирование завершено. "
                    f"Следующий запуск через {SCAN_INTERVAL_SECONDS} секунд."
                )

                await asyncio.sleep(SCAN_INTERVAL_SECONDS)

        finally:
            await browser.close()
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
