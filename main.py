import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from telethon import TelegramClient, StringSession


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "")
TELEGRAM_TARGET = "@fwd19472"

SENT_FILE = Path("sent_jobs.json")
SCAN_INTERVAL = 3600
PAGE_DELAY = 1
MESSAGE_DELAY = 2

SECTIONS = [
    "https://jobatsea.online/jobs/Engine_Officers/",
    "https://jobatsea.online/jobs/Engine_Ratings/",
    "https://jobatsea.online/jobs/Deck_Officers/",
    "https://jobatsea.online/jobs/Deck_Ratings/",
    "https://jobatsea.online/jobs/Catering_Staff/",
    "https://jobatsea.online/jobs/Offshore/",
]

EMAIL_REGEX = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)

# Адреса, которые не считаются контактными e-mail вакансии
IGNORED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
}

MAX_MESSAGE_LENGTH = 3900


# ============================================================
# MEMORY
# ============================================================

def load_sent_jobs() -> set[str]:
    if not SENT_FILE.exists():
        return set()

    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))
        return set(data)
    except (json.JSONDecodeError, OSError):
        return set()


def save_sent_jobs(sent_jobs: set[str]) -> None:
    SENT_FILE.write_text(
        json.dumps(sorted(sent_jobs), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# HELPERS
# ============================================================

def extract_emails(text: str) -> list[str]:
    found = EMAIL_REGEX.findall(text)
    result = []

    for email in found:
        email = email.strip(".,;:()[]<>").lower()
        domain = email.rsplit("@", 1)[-1]

        if domain not in IGNORED_EMAIL_DOMAINS and email not in result:
            result.append(email)

    return result


def clean_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}".strip()

        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                parts.append(current)

            while len(paragraph) > limit:
                parts.append(paragraph[:limit])
                paragraph = paragraph[limit:]

            current = paragraph

    if current:
        parts.append(current)

    return parts


# ============================================================
# PARSING
# ============================================================

async def get_job_links(page, section_url: str, page_number: int) -> list[str]:
    separator = "&" if "?" in section_url else "?"
    page_url = f"{section_url}{separator}p={page_number}"

    try:
        await page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(1000)
    except PlaywrightTimeoutError:
        print(f"Timeout при открытии раздела: {page_url}")
        return []

    links = await page.locator("a[href*='/job/']").evaluate_all(
        """
        elements => elements.map(element => element.href)
        """
    )

    unique_links = []
    for link in links:
        absolute_link = urljoin(page_url, link).split("#")[0]
        if absolute_link not in unique_links:
            unique_links.append(absolute_link)

    return unique_links


async def get_job_data(page, job_url: str) -> dict | None:
    try:
        await page.goto(
            job_url,
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(500)
    except PlaywrightTimeoutError:
        print(f"Timeout при открытии вакансии: {job_url}")
        return None

    try:
        text = await page.locator("body").inner_text()
    except Exception as error:
        print(f"Не удалось получить текст вакансии: {error}")
        return None

    text = clean_text(text)
    emails = extract_emails(text)

    # Вакансии без e-mail пропускаются
    if not emails:
        return None

    return {
        "text": text,
        "emails": emails,
    }


async def scan_section(page, section_url: str, sent_jobs: set[str]) -> list[dict]:
    found_jobs = []
    page_number = 1

    while True:
        print(f"Проверяется: {section_url}?p={page_number}")

        job_links = await get_job_links(page, section_url, page_number)

        if not job_links:
            break

        new_links_found = False

        for job_url in job_links:
            if job_url in sent_jobs:
                continue

            new_links_found = True
            job_data = await get_job_data(page, job_url)

            if job_data:
                found_jobs.append(
                    {
                        "url": job_url,
                        "text": job_data["text"],
                        "emails": job_data["emails"],
                    }
                )

            await asyncio.sleep(PAGE_DELAY)

        # Если на странице только уже обработанные ссылки,
        # продолжаем проверять следующую страницу.
        if not new_links_found and page_number > 100:
            break

        page_number += 1
        await asyncio.sleep(PAGE_DELAY)

    return found_jobs


# ============================================================
# TELEGRAM
# ============================================================

async def send_job(client: TelegramClient, job: dict) -> None:
    message = (
        "JobAtSea\n\n"
        f"{job['text']}\n\n"
        f"Email: {', '.join(job['emails'])}"
    )

    # Ссылка намеренно не добавляется в сообщение
    for part in split_message(message):
        await client.send_message(TELEGRAM_TARGET, part)
        await asyncio.sleep(MESSAGE_DELAY)


# ============================================================
# MAIN
# ============================================================

async def run_once(client: TelegramClient) -> None:
    sent_jobs = load_sent_jobs()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            for section_url in SECTIONS:
                jobs = await scan_section(page, section_url, sent_jobs)

                for job in jobs:
                    try:
                        await send_job(client, job)

                        # Сохраняем URL после успешной отправки
                        sent_jobs.add(job["url"])
                        save_sent_jobs(sent_jobs)

                        print(f"Отправлено: {job['url']}")

                    except Exception as error:
                        print(f"Ошибка отправки вакансии: {error}")

        finally:
            await browser.close()


async def main() -> None:
    if not TELEGRAM_API_ID:
        raise RuntimeError("Не задана переменная TELEGRAM_API_ID")

    if not TELEGRAM_API_HASH:
        raise RuntimeError("Не задана переменная TELEGRAM_API_HASH")

    if not TELEGRAM_SESSION:
        raise RuntimeError("Не задана переменная TELEGRAM_SESSION")

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )

    await client.start()

    try:
        while True:
            print("Начало проверки JobAtSea")
            await run_once(client)
            print(f"Проверка завершена. Следующая через {SCAN_INTERVAL} секунд.")
            await asyncio.sleep(SCAN_INTERVAL)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
