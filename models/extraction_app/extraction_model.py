import urllib
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.service import Service
from selenium.webdriver.edge.options import Options
import urllib.parse
import time
import json
from fake_useragent import UserAgent
import asyncio
import aiohttp
from selectolax.parser import HTMLParser
import urllib.parse
import asyncio
from random import uniform
from bs4 import BeautifulSoup
import random


def init():
    return

def run(http_request, app_logger) -> dict:

    global LOGGER
    LOGGER = app_logger

    http_request = json.loads(http_request)
    print(http_request)

    query = http_request

    # query = {
    #     "search_params": {
    #             "name": "",
    #             "start_date": "",
    #             "end_date": "",
    #             "domain":"",
    #             "company":""
    #     },
    #     "link_count_limit": 10,
    #     "article_count_limit": 5
    # }

    name = query.get("search_params",{}).get("name")
    start_date = int(query.get("search_params", {}).get("start_date"))
    end_date = int(query.get("search_params", {}).get("end_date"))
    domain = query.get("search_params", {}).get("domain")
    company = query.get("search_params", {}).get("company")
    print(name, start_date)

    link_count_limit = query.get("link_count_limit")
    article_count_limit = query.get("article_count_limit")

    # print(query)
    # return True


    # ---- GET DRIVER ---- #
    # try:
    #     driver = generate_selenium_instance()
    # except Exception as err:
    #     # stage_failure_flag = True
    #     msg = 'Link extraction error: {}'.format(err)
    #     LOGGER.exception(msg)
    #     # return error here if error so http endpoint can return error
    #
    # # ---- RUN LINK EXTRACTION ---- #
    links = []
    try:
        links =asyncio.run(link_extraction(name, start_date, end_date))
    except Exception as err:
        # stage_failure_flag = True
        msg = 'Link extraction error: {}'.format(err)
        LOGGER.exception(msg)
    print("link extraction done")
    try:
        driver = generate_selenium_instance()
        print("driver triggered")
    except Exception as err:
        # stage_failure_flag = True
        msg = 'Link extraction error: {}'.format(err)
        LOGGER.exception(msg)

    # ---- RUN ARTICLE EXTRACTION ---- #
    compiled_articles = []
    successful_article_extraction_count = 0
    for link in links[:link_count_limit]:
        link_with_article = asyncio.run(article_extraction(link, driver))
        compiled_articles.append(link_with_article)
        # while successful_article_extraction_count <= article_count_limit:
        #     print('while loop entered')
        #     link_with_article = asyncio.run(article_extraction(link, driver))
        #     compiled_articles.append(link_with_article)
        #     successful_article_extraction_count +=1

    # ---- FORMAT AND RETURN ---- #
    response_body = {
        "query_searched": query,
        "no_of_links_found": len(links),
        "no_of_articles_found": len(compiled_articles),
        "compiled_articles": compiled_articles
    }

    return response_body

def generate_selenium_instance():

    ua = UserAgent()
    options = Options()
    options.add_argument(f"user-agent={ua.random}")  # Rotate user agent
    # options.add_argument("--headless")
    # options.add_argument("--disable-gpu")
    options.use_chromium = True  # Necessary for Edge Chromium
    # options.add_argument("--headless")  # Optional: Run in headless mode
    options.add_argument("--disable-blink-features=AutomationControlled")

    ser_obj = Service(
        r"driver3/msedgedriver.exe"
    )
    driver = webdriver.Edge(service=ser_obj, options=options)

    return driver

async def fetch(session, url, proxy=None, retries=3):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'
    }

    try:
        async with session.get(url, headers=headers, proxy=proxy) as response:
            if response.status == 200:
                # Delay after request to let dynamic content load
                await asyncio.sleep(5)  # Wait for 3 seconds to allow the full page to load
                return await response.text()
            elif response.status == 429:
                print(f"Received 429 for {url}, retrying after delay...")
                await asyncio.sleep(uniform(1, 3))  # Delay before retrying
                return await fetch(session, url, proxy, retries - 1) if retries > 0 else None
            else:
                print(f"Failed to fetch {url} with status code {response.status}")
                return None
    except Exception as e:
        print(f"Error fetching {url}: {e}")
        return None

async def link_extraction(name, start_date, end_date):
    await asyncio.sleep(1)
    end_date = int(end_date) + 1
    duration = list(range(start_date, end_date))
    duration = [str(num) for num in duration]
    base_url = 'https://news.google.com/search?q='
    news = []

    async with aiohttp.ClientSession() as session:
        for n in duration:
            hco_url = base_url + urllib.parse.quote(f'{name} after:{n}-01-01 before:{n}-12-31')
            print(f"Fetching URL: {hco_url}")

            html = await fetch(session, hco_url)
            if html is None:
                print(f"No content retrieved for year {n}")
                continue

            tree = HTMLParser(html)
            articles = tree.css('.D9SJMe .IFHyqb.DeXSAc')

            if not articles:
                print(f'No news found for year: {n}')
                continue

            for article in articles:
                try:
                    date_element = article.css_first('.hvbAAd')
                    date = date_element.attributes.get('datetime')[:10] if date_element else None

                    title_element = article.css_first('.JtKRv')
                    title = title_element.text() if title_element else None

                    link = title_element.attributes.get('href') if title_element else None
                    if link:
                        link = urllib.parse.urljoin('https://news.google.com', link)

                    if link:
                        news.append({'title': title, 'date': date, 'link': link})
                    else:
                        print(f"Invalid link found for article: {title}")
                except Exception as e:
                    print(f"Error parsing article for year {n}: {e}")

            print(f"{len(news)} articles found for year {n}")

        return news

async def article_extraction(link, driver):
    final_news = []
    count = 0
    print("entered article extraction")
    driver.get(link['link'])
    time.sleep(4)
    try:
        accept_cookies_button = driver.find_element(By.XPATH, "//button[contains(text(),'Accept')]")
        if accept_cookies_button:
            accept_cookies_button.click()
    except:
        print("No cookies popup found.")
    await asyncio.sleep(random.uniform(2, 5))  # Random delay between requests
    page_source = driver.page_source
    soup = BeautifulSoup(page_source, "html.parser")
    text_content = " ".join(soup.stripped_strings)
    link["full_article"] = text_content
    link_with_article_content = link
    return link_with_article_content


if __name__ == "__main__":
    init()
