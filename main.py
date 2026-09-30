import asyncio
import json
import os
import re
import logging
from urllib.parse import urljoin
from playwright.async_api import async_playwright
from telethon import TelegramClient

# --- Конфигурация ---
# Загрузка переменных окружения (настройте их в Railway или локально)
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION_NAME = os.getenv("TELEGRAM_SESSION", "job_bot")
TARGET_CHAT = os.getenv("TELEGRAM_CHAT_ID", "me") # Куда отправлять вакансии
JOBS_URL = "https://cv.offshorecrew.no/jobs/"

# Список бесплатных доменов для фильтрации
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "mail.ru", 
    "yandex.ru", "rambler.ru", "list.ru", "bk.ru", "inbox.ru"
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Вспомогательные функции ---

def is_free_email(email: str) -> bool:
    domain = email.split('@')[-1].lower()
    return domain in FREE_EMAIL_DOMAINS

def get_business_emails(emails: list[str]) -> list[str]:
    return [e for e in emails if not is_free_email(e)]

def load_sent_jobs():
    if os.path.exists("sent_jobs.json"):
        with open("sent_jobs.json", "r") as f:
            return set(json.load(f))
    return set()

def save_sent_jobs(sent_jobs):
    with open("sent_jobs.json", "w") as f:
        json.dump(list(sent_jobs), f)

# --- Функции парсинга ---

async def get_job_links(page, jobs_url: str) -> list[str]:
    await page.goto(jobs_url, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(5000) # Ждем прогрузки SPA

    # Прокрутка для lazy loading
    for _ in range(3):
        await page.mouse.wheel(0, 1200)
        await page.wait_for_timeout(1000)

    links = await page.locator("a").evaluate_all(
        "elements => elements.map(a => a.href || '').filter(href => href.includes('/postings/'))"
    )
    
    # Нормализация ссылок для работы с SPA
    unique_links = set()
    for link in links:
        if "#/postings/" in link:
            # Превращаем #/postings/ в полный URL, если нужно
            clean_url = "https://cv.offshorecrew.no/jobs/" + link.split("#", 1)[1]
            unique_links.add(clean_url)
    
    return list(unique_links)

async def get_job_details(page, url: str) -> dict | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4000)

        text = await page.locator("body").inner_text()
        if not text.strip():
            return None

        # Очистка текста
        apply_markers = ["Apply for this position", "Apply for this job", "Apply"]
        clean_text = text
        for marker in apply_markers:
            if marker in clean_text:
                clean_text = clean_text.split(marker, 1)[0]
                break

        emails = sorted(set(EMAIL_RE.findall(clean_text)))
        business_emails = get_business_emails(emails)
        
        if not business_emails: # Игнорируем, если нет корпоративных e-mail
            return None

        return {
            "url": url,
            "title": (await page.title()).strip(),
            "emails": business_emails,
            "text": clean_text[:500] + "..." # Обрезаем для превью в Telegram
        }
    except Exception as e:
        logger.error(f"Ошибка при парсинге {url}: {e}")
        return None

# --- Основной цикл ---

async def main():
    if not API_ID or not API_HASH:
        logger.error("Не заданы TELEGRAM_API_ID или TELEGRAM_API_HASH")
        return

    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    sent_jobs = load_sent_jobs()

    async with client:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            logger.info("Начинаю поиск вакансий...")
            links = await get_job_links(page, JOBS_URL)
            logger.info(f"Найдено ссылок: {len(links)}")

            for link in links:
                if link in sent_jobs:
                    continue

                job = await get_job_details(page, link)
                if job:
                    message = f"🆕 **{job['title']}**\n\n{job['text']}\n\n📧 Contacts: {', '.join(job['emails'])}\n🔗 {job['url']}"
                    await client.send_message(TARGET_CHAT, message, parse_mode='md')
                    sent_jobs.add(link)
                    save_sent_jobs(sent_jobs)
                    logger.info(f"Отправлена вакансия: {link}")
                
                await asyncio.sleep(2) # Пауза между запросами

            await browser.close()
            logger.info("Сканирование завершено.")

if __name__ == "__main__":
    asyncio.run(main())
