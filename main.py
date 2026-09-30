import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from telethon import TelegramClient
from telethon.sessions import StringSession


# -----------------------------
# Настройки
# -----------------------------

BASE_URL = "https://jobatsea.online"
SECTION_URL = f"{BASE_URL}/jobs/Engine_Officers/"

TELEGRAM_TARGET = "@fwd19472"

DEBUG_DIR = Path("debug")
SENT_FILE = Path("sent_jobs.json")

PAGE_TIMEOUT = 30000
WAIT_AFTER_LOAD = 5000


# -----------------------------
# Логирование
# -----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# -----------------------------
# Бесплатные e-mail-сервисы
# -----------------------------

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "mail.com",
    "protonmail.com",
    "proton.me",
    "gmx.com",
    "gmx.de",
    "yandex.ru",
    "yandex.com",
    "mail.ru",
    "inbox.ru",
    "list.ru",
    "bk.ru",
    "rambler.ru",
    "ukr.net",
    "inbox.lv",
}


# -----------------------------
# Работа с памятью
# -----------------------------

def load_sent_jobs() -> set[str]:
    if not SENT_FILE.exists():
        return set()

    try:
        data = json.loads(
            SENT_FILE.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return set(str(item) for item in data)

        if isinstance(data, dict):
            return set(str(item) for item in data.keys())

    except Exception:
        logger.exception(
            "Не удалось прочитать %s",
            SENT_FILE,
        )

    return set()


def save_sent_jobs(sent_jobs: set[str]) -> None:
    SENT_FILE.write_text(
        json.dumps(
            sorted(sent_jobs),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# -----------------------------
# E-mail-фильтрация
# -----------------------------

def is_free_email(email: str) -> bool:
    email = email.lower().strip()

    if "@" not in email:
        return True

    domain = email.rsplit("@", 1)[1]
    return domain in FREE_EMAIL_DOMAINS


def get_business_emails(text: str) -> list[str]:
    emails = re.findall(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        text,
        flags=re.IGNORECASE,
    )

    result = []

    for email in emails:
        email = email.lower().strip()

        if not is_free_email(email) and email not in result:
            result.append(email)

    return result


# -----------------------------
# Проверка Cloudflare
# -----------------------------

def detect_protection(html: str, title: str) -> list[str]:
    content = f"{title}\n{html}".lower()

    indicators = [
        "cloudflare",
        "just a moment",
        "checking your browser",
        "verify you are human",
        "captcha",
        "access denied",
    ]

    return [
        indicator
        for indicator in indicators
        if indicator in content
    ]


# -----------------------------
# Ссылки на вакансии
# -----------------------------

def is_job_link(link: str) -> bool:
    if not link:
        return False

    return bool(
        re.search(
            r"/job/[^/?#]+/",
            link,
            flags=re.IGNORECASE,
        )
    )


async def get_job_links(page, url: str) -> list[str]:
    logger.info("Открываем раздел: %s", url)

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

    except PlaywrightTimeoutError:
        logger.warning(
            "Тайм-аут загрузки страницы. "
            "Продолжаем проверку полученного HTML."
        )

    except Exception:
        logger.exception(
            "Ошибка перехода на страницу %s",
            url,
        )

    await page.wait_for_timeout(WAIT_AFTER_LOAD)

    try:
        title = await page.title()
        html = await page.content()

    except Exception:
        logger.exception(
            "Не удалось получить содержимое страницы"
        )
        return []

    protection = detect_protection(html, title)

    if protection:
        DEBUG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        debug_file = DEBUG_DIR / "cloudflare_block.html"

        debug_file.write_text(
            html,
            encoding="utf-8",
        )

        logger.error(
            "Обнаружена защита сайта: %s",
            ", ".join(protection),
        )

        logger.error(
            "Страница сохранена в %s",
            debug_file,
        )

        return []

    try:
        links = await page.eval_on_selector_all(
            "a",
            """
            elements => elements.flatMap(element => {
                const values = [];

                if (element.href) {
                    values.push(element.href);
                }

                for (const attribute of element.attributes) {
                    values.push(attribute.value);
                }

                return values;
            })
            """,
        )

    except Exception:
        logger.exception(
            "Не удалось получить ссылки со страницы"
        )
        return []

    job_links = set()

    for value in links:
        if not value:
            continue

        absolute_url = urljoin(BASE_URL, value)

        if is_job_link(absolute_url):
            clean_url = absolute_url.split("#", 1)[0]
            job_links.add(clean_url)

    result = sorted(job_links)

    logger.info(
        "Найдено ссылок на вакансии: %d",
        len(result),
    )

    if not result:
        DEBUG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        debug_file = DEBUG_DIR / "no_job_links.html"

        debug_file.write_text(
            html,
            encoding="utf-8",
        )

        logger.warning(
            "Ссылки на вакансии не найдены."
        )

        logger.warning(
            "HTML сохранён в %s",
            debug_file,
        )

    return result


# -----------------------------
# Получение информации о вакансии
# -----------------------------

async def get_job_details(page, url: str) -> dict | None:
    logger.info("Открываем вакансию: %s", url)

    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

    except PlaywrightTimeoutError:
        logger.warning(
            "Тайм-аут при открытии вакансии: %s",
            url,
        )

    except Exception:
        logger.exception(
            "Ошибка открытия вакансии: %s",
            url,
        )
        return None

    await page.wait_for_timeout(3000)

    try:
        title = await page.title()
        text = await page.locator("body").inner_text()

    except Exception:
        logger.exception(
            "Не удалось прочитать вакансию: %s",
            url,
        )
        return None

    protection = detect_protection(text, title)

    if protection:
        logger.warning(
            "Вакансия заблокирована защитой сайта: %s",
            url,
        )
        return None

    emails = get_business_emails(text)

    if not emails:
        logger.info(
            "Вакансия пропущена: корпоративные e-mail не найдены"
        )
        return None

    return {
        "url": url,
        "title": title.strip(),
        "text": text.strip(),
        "emails": emails,
    }


# -----------------------------
# Формирование сообщения
# -----------------------------

def make_message(job: dict) -> str:
    text = job["text"]

    # Ограничиваем размер сообщения Telegram
    if len(text) > 3500:
        text = text[:3500] + "\n..."

    return (
        f"⚓ <b>{job['title']}</b>\n\n"
        f"{text}\n\n"
        f"📧 Корпоративные e-mail: "
        f"{', '.join(job['emails'])}\n\n"
        f"🔗 {job['url']}"
    )


# -----------------------------
# Основная функция
# -----------------------------

async def main():
    api_id_raw = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session_string = os.getenv("TELEGRAM_SESSION")

    if not api_id_raw:
        raise ValueError(
            "Не задана переменная TELEGRAM_API_ID"
        )

    if not api_hash:
        raise ValueError(
            "Не задана переменная TELEGRAM_API_HASH"
        )

    if not session_string:
        raise ValueError(
            "Не задана переменная TELEGRAM_SESSION"
        )

    try:
        api_id = int(api_id_raw)
    except ValueError as error:
        raise ValueError(
            "TELEGRAM_API_ID должен содержать только цифры"
        ) from error

    sent_jobs = load_sent_jobs()

    client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash,
    )

    try:
        await client.start()
        logger.info("Telegram-клиент успешно подключён")

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    viewport={
                        "width": 1366,
                        "height": 768,
                    },
                    locale="en-US",
                )

                page = await context.new_page()

                job_links = await get_job_links(
                    page,
                    SECTION_URL,
                )

                if not job_links:
                    logger.warning(
                        "Вакансии не найдены или сайт заблокировал доступ."
                    )
                    return

                for job_url in job_links:
                    if job_url in sent_jobs:
                        logger.info(
                            "Ваканция уже отправлялась: %s",
                            job_url,
                        )
                        continue

                    job = await get_job_details(
                        page,
                        job_url,
                    )

                    if not job:
                        continue

                    message = make_message(job)

                    await client.send_message(
                        TELEGRAM_TARGET,
                        message,
                        parse_mode="html",
                        link_preview=False,
                    )

                    sent_jobs.add(job_url)
                    save_sent_jobs(sent_jobs)

                    logger.info(
                        "Ваканция отправлена: %s",
                        job_url,
                    )

                    await asyncio.sleep(2)

            finally:
                await browser.close()

    finally:
        await client.disconnect()
        logger.info("Telegram-клиент отключён")


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except Exception:
        logger.exception(
            "Критическая ошибка приложения"
        )
        raise
