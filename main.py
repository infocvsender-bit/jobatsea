import os
import re
import json
import asyncio
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_API_ID = int(
    os.getenv("TELEGRAM_API_ID", "0")
)

TELEGRAM_API_HASH = os.getenv(
    "TELEGRAM_API_HASH",
    "",
)

TELEGRAM_SESSION = os.getenv(
    "TELEGRAM_SESSION",
    "",
)

TELEGRAM_TARGET = os.getenv(
    "TELEGRAM_TARGET",
    "@Cvsendler_bot",
)

CREWLINK_URL = os.getenv(
    "CREWLINK_URL",
    "https://crewlink.me/dashboard-navigator/joburi?days=1",
)

SENT_FILE = Path(
    os.getenv(
        "SENT_FILE",
        "/app/sent_jobs.json",
    )
)

POLL_INTERVAL = int(
    os.getenv(
        "POLL_INTERVAL",
        "300",
    )
)


# ============================================================
# EMAIL FILTER
# ============================================================

EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "mail.ru",
    "yandex.ru",
    "ukr.net",
}


def get_corporate_emails(text: str) -> list[str]:
    emails = set(
        EMAIL_PATTERN.findall(text)
    )

    corporate_emails = []

    for email in emails:
        domain = email.lower().split("@")[-1]

        if domain not in FREE_EMAIL_DOMAINS:
            corporate_emails.append(email)

    return sorted(corporate_emails)


# ============================================================
# SENT JOBS
# ============================================================

def load_sent_jobs() -> set[str]:
    if not SENT_FILE.exists():
        return set()

    try:
        data = json.loads(
            SENT_FILE.read_text(
                encoding="utf-8",
            )
        )

        return set(data)

    except Exception:
        return set()


def save_sent_jobs(sent_jobs: set[str]) -> None:
    SENT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    SENT_FILE.write_text(
        json.dumps(
            sorted(sent_jobs),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# DOWNLOAD PAGE
# ============================================================

async def download_page(
    session: aiohttp.ClientSession,
) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/131.0 Safari/537.36"
        )
    }

    async with session.get(
        CREWLINK_URL,
        headers=headers,
        timeout=60,
    ) as response:

        response.raise_for_status()

        return await response.text()


# ============================================================
# PARSE VACANCIES
# ============================================================

def parse_vacancies(html: str) -> list[dict]:
    soup = BeautifulSoup(
        html,
        "lxml",
    )

    vacancies = []

    for item in soup.select(
        "article, .job, .vacancy, .card"
    ):
        text = item.get_text(
            "\n",
            strip=True,
        )

        if not text:
            continue

        link = item.find("a")

        url = ""

        if link and link.get("href"):
            url = link["href"]

        vacancies.append(
            {
                "text": text,
                "url": url,
            }
        )

    return vacancies


# ============================================================
# TELEGRAM
# ============================================================

async def send_telegram(
    client: TelegramClient,
    text: str,
) -> None:
    await client.send_message(
        TELEGRAM_TARGET,
        text,
        link_preview=False,
    )


# ============================================================
# MAIN PROCESSING
# ============================================================

async def process_vacancies(
    client: TelegramClient,
) -> None:

    sent_jobs = load_sent_jobs()

    timeout = aiohttp.ClientTimeout(
        total=60,
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
    ) as session:

        html = await download_page(session)

    vacancies = parse_vacancies(html)

    print(
        f"Найдено вакансий: {len(vacancies)}"
    )

    for vacancy in vacancies:
        text = vacancy["text"]
        url = vacancy["url"]

        emails = get_corporate_emails(text)

        if not emails:
            print(
                "Пропущено: корпоративный email "
                "не найден."
            )
            continue

        job_id = url or text

        if job_id in sent_jobs:
            print(
                "Пропущено: ваканция уже отправлялась."
            )
            continue

        message = (
            f"{text}\n\n"
            f"Corporate email: "
            f"{', '.join(emails)}"
        )

        await send_telegram(
            client,
            message,
        )

        sent_jobs.add(job_id)
        save_sent_jobs(sent_jobs)

        print(
            "Вакансия отправлена в Telegram."
        )


# ============================================================
# APPLICATION
# ============================================================

async def main() -> None:
    if not TELEGRAM_API_ID:
        raise RuntimeError(
            "Не задан TELEGRAM_API_ID."
        )

    if not TELEGRAM_API_HASH:
        raise RuntimeError(
            "Не задан TELEGRAM_API_HASH."
        )

    if not TELEGRAM_SESSION:
        raise RuntimeError(
            "Не задан TELEGRAM_SESSION."
        )

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
    )

    await client.start()

    print("Скрипт запущен.")

    while True:
        try:
            await process_vacancies(client)

        except Exception as error:
            print(
                f"Ошибка: {error}"
            )

        await asyncio.sleep(
            POLL_INTERVAL
        )


if __name__ == "__main__":
    asyncio.run(main())
