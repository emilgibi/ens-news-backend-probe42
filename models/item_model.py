import concurrent.futures
from typing import List, Optional, Tuple, Dict, Any
import urllib.parse
import asyncio
import aiohttp
from selectolax.parser import HTMLParser
from random import uniform
import requests
from fastapi import Request, HTTPException
from .custom_link_decoder import execute_decoding_concurrently
from .llm_analysis import *
from datetime import date, timedelta
from typing import List, Tuple, Union
from datetime import datetime
import math
from dotenv import load_dotenv
from aiohttp import ClientSession
import json
import threading
import concurrent.futures
import concurrent.futures
import time  # Import time to use sleep
from schemas.logger import logger
from .llm_analysis import require_llm_response_speed
import os
from requests.adapters import HTTPAdapter
from utils.db_utils import *
from urllib3.util.retry import Retry



load_dotenv()
CONFIG_TYPE = os.getenv('CONFIG')
SCRAPER_URL = os.getenv('NEWS_SCRAPER_URL')

# -------- Scraper HTTP client (NO retries, with timeouts) --------
SCRAPER_TIMEOUT = (3.05, 90)  # (connect timeout, read timeout) seconds

_scraper_session = requests.Session()
_adapter = HTTPAdapter(max_retries=0, pool_connections=50, pool_maxsize=50)
_scraper_session.mount("http://", _adapter)
_scraper_session.mount("https://", _adapter)


def format_date(value):
    if isinstance(value, (date, datetime)):
        return value.strftime('%Y-%m-%d')
    return value

# A simple in-memory database replacement
class ItemModel:
    def __init__(self, id: int, name: str, description: Optional[str] = None):
        self.id = id
        self.name = name
        self.description = description


# Fake in-memory database, simulating persistence
items_db: List[ItemModel] = [
    ItemModel(id=1, name="Item One", description="This is the first item."),
    ItemModel(id=2, name="Item Two", description="This is the second item."),
    ItemModel(id=3, name="Item Three", description="This is the third item."),
]


def read_dummy_file():
    with open('./dummy.json', 'r') as file:
        data = json.load(file)
    return data


dummyData = read_dummy_file()

news_link_extraction_flag = False
def run_analysis_pipeline_on_article(article_dict: dict, n: int, name: str, domain: str, flag: str, company: str,
                                     demo_config: bool, plot: bool) -> tuple[dict[Any, Any], int, int]:
    """
    Main analysis orchestrator to run LLM prompts after the full articles have been extracted
    :param article_dict: dicts with mandatory keys:{'title': title, 'date': date, 'link': link, 'full_article': full_article}
    :param name: str name of entity to check article relevance
    :param domain: str query domain to check article relevance
    :return: news (all the extracted and analysed articles), keywords_data_agg (aggregated keywords across all articles)

    *Note: previously called extract_article_and_sentiment
    """
    article_text = article_dict["full_article"]
    article_after_removing_html_tag = re.sub(r'<[^>]+>', '', article_text)
    words = article_text.split(" ")


    article = " ".join(words[:700])

    title = article_dict['title']

    # Initialise content warning for this article
    content_filter_triggered = False
    total_token = 0
    logger.info(f"---------- STARTING ANALYSIS FOR ARTICLE # {n} : {title} ---------------")

    # print("CHECK 1: Related to Person")
    article_modified = None
    article_modified = extract_context_around_mentions(article_after_removing_html_tag, name)

    if len(article_modified) == 0:
        return {}, 400, total_token
    ans1, status_code, related_to_person_token = related_to_person(name, article_modified, flag)
    total_token = total_token + related_to_person_token
    # print(status_code,"CHECK 1: RESULT - Related_to_person____________", ans1, n)
    if status_code == 429:
        # print('CHECK 1: Error - Token Limit Triggered, Breaking Loop')
        return {}, 429, total_token
    elif status_code == 404:
        # print(status_code, "CHECK 1: Error - Unknown, Skipping Article")
        return {}, 404, total_token
    elif status_code == 201:
        content_filter_triggered = True
        # print('CHECK 1: Error - Content Warning Triggered - Retrying With Title')
        ans1, retry_status_code, related_to_person_token = related_to_person(name, title, flag)
        total_token = total_token + related_to_person_token
        # print(status_code,"CHECK 1: RESULT - Related_to_person____________", ans1, n)
        if retry_status_code != 200:
            # print(status_code, "CHECK 1: Error - Failed for Title As Well, Skipping Article")
            return {}, 404, total_token
    ans2 = 'Y'
    if (company != '') and (flag == 'POI') and (plot == False) and ('y' in ans1.lower()):
        # print("CHECK 2: Related to Company - POI - Searching for Company ", company)
        ans2, status_code, related_to_company_token = related_to_company(company, article, flag)
        # print(status_code," CHECK 2: RESULT - Related_to_company____________", ans2, n)
        total_token = total_token + related_to_company_token
        if status_code == 429:
            # print('CHECK 2: Error - Token Limit Triggered, Breaking Loop')
            return {}, 429, total_token
        elif status_code == 404:
            # print('CHECK 2: Error - Unknown, Skipping Article')
            return {}, 404, total_token
        elif status_code == 201:
            # print('CHECK 2: Error - Content Warning Triggered - Default Not Related to Company, Article Will Be Skipped')
            content_filter_triggered = True
            ans2 = 'N'

    # If related to person: proceed
    # print("CHECK 3: Related to Domain (Non-Filtering Check)")
    if "y" in ans1.lower() and "y" in ans2.lower():
        if plot == True:
            # CHeck only for sentiment
            senti, sentiment_status_code, sentiment_token = sentiment(article_modified, name, flag)
            # print(sentiment_status_code, f"CHECK 5: RESULT - Sentiment for article {n}: {senti}, status code: {sentiment_status_code}")
            if sentiment_status_code == 429:
                # print('CHECK 5: Error - Token Limit Triggered, Breaking Loop')
                return {}, 429, total_token  # Token limit
            elif sentiment_status_code == 404:
                # print('CHECK 5: Error - Unknown, Skipping Article')
                return {}, 404, total_token
            elif sentiment_status_code == 201:
                # print('CHECK 5: Error - Content Warning Triggered, Fallback Sentiment Returning Negative, Proceed')
                content_filter_triggered = True
            total_token = total_token + sentiment_token
            if senti.lower() == 'negative':
                summary, status_code, summarize_token = summarize_text(title, article, name, flag)
                # print(status_code, f"CHECK 4: RESULT - Summary for article {n}: {summary}, status code: {status_code}")
                if status_code == 429:
                    # print('CHECK 4: Error - Token Limit Triggered, Breaking Loop')
                    return {}, 429, total_token  # Token limit
                elif status_code == 404:
                    # print('CHECK 4: Error - Unknown, Skipping Article')
                    return {}, 404, total_token  # skip this article
                elif status_code == 201:
                    # print('CHECK 4: Error - Content Warning Triggered, Fallback Summary Returning Title, Proceed')
                    content_filter_triggered = True
                total_token = total_token + summarize_token
                categories = categorize_news(article)
                category = next(iter(categories))
                topic = categories[category]
                kpi_verification, verification_status_code, verification_token = cross_verifying_kpi(summary, name, topic)
                if verification_status_code == 429:
                    # print('CHECK 2: Error - Token Limit Triggered, Breaking Loop')
                    return {}, 429, total_token
                elif verification_status_code == 404:
                    # print('CHECK 2: Error - Unknown, Skipping Article')
                    return {}, 404, total_token
                elif verification_status_code == 201:
                    # print('CHECK 2: Error - Content Warning Triggered - Default Not Related to Company, Article Will Be Skipped')
                    content_filter_triggered = True
                    kpi_verification = 'N'
                total_token = total_token + verification_token
                if kpi_verification.lower() == 'y':
                    final_category = category
                else:
                    final_category = "General"
            else:
                final_category = "None"
                summary = "None"
            analysed_article = {
                'name': name,
                'title': title,
                'category': final_category,
                'summary': summary,
                'date': article_dict['date'],
                'link': article_dict['decoding']['decoded_url'],
                'sentiment': senti,
                'content_filtered': content_filter_triggered
            }
            logger.info(f"---------- FINISHED ANALYSIS FOR ARTICLE # {n} ---------------")
            return analysed_article, 200, total_token

        domain_results = {}
        for domains in domain:
            response, status_code, domains_token = related_to_domain(domains, article, flag)
            # print(status_code, f"CHECK 3: RESULT - related_to_domain_{domains}____________", response, n)
            total_token = total_token + domains_token
            if status_code == 429:
                # print('CHECK 3: Error - Token Limit Triggered, Breaking Loop')
                return {}, 429, total_token
            elif status_code == 404:
                # print('CHECK 3: Error - Unknown, Skipping Article')
                return {}, 404, total_token
            elif status_code == 201:
                # print('CHECK 3: Error - Content Warning Triggered, Fallback "N" Unrelated to Domain, Proceed')
                content_filter_triggered = True

            domain_results[domains] = True if response.lower() == 'y' else False

        # print("CHECK 4: Summarisation")
        # Perform Summarisation
        summary, status_code, summary_token = summarize_text(title, article, name, flag)
        total_token = total_token + summary_token
        # print(status_code, f"CHECK 4: RESULT - Summary for article {n}: {summary}, status code: {status_code}")

        if status_code == 429:
            # print('CHECK 4: Error - Token Limit Triggered, Breaking Loop')
            return {}, 429, total_token  # Token limit
        elif status_code == 404:
            # print('CHECK 4: Error - Unknown, Skipping Article')
            return {}, 404, total_token  # skip this article
        elif status_code == 201:
            # print('CHECK 4: Error - Content Warning Triggered, Fallback Summary Returning Title, Proceed')
            content_filter_triggered = True

        # print("CHECK 5: Sentiment")
        # Perform Sentiment Analysis
        senti, sentiment_status_code, sentiment_token = sentiment(article_after_removing_html_tag, name, flag)
        total_token = total_token + sentiment_token
        # print(sentiment_status_code, f"CHECK 5: RESULT - Sentiment for article {n}: {senti}, status code: {sentiment_status_code}")

        if sentiment_status_code == 429:
            # print('CHECK 5: Error - Token Limit Triggered, Breaking Loop')
            return {}, 429, sentiment_token  # Token limit
        elif sentiment_status_code == 404:
            # print('CHECK 5: Error - Unknown, Skipping Article')
            return {}, 404, sentiment_token
        elif sentiment_status_code == 201:
            # print('CHECK 5: Error - Content Warning Triggered, Fallback Sentiment Returning Negative, Proceed')
            content_filter_triggered = True

        # print("CHECK 6: Keyword Extraction")
        # Perform Keyword Extraction
        key, keyword_status_code, keyword_token = keyword(summary, flag)
        total_token = total_token + keyword_token
        # print(keyword_status_code, f"CHECK 6: RESULT - Keywords {key}, status code: {keyword_status_code}")

        if keyword_status_code == 429:
            # print('CHECK 6: Error - Token Limit Triggered, Breaking Loop')
            return {}, 429, total_token
        elif keyword_status_code == 404:
            # print('CHECK 6: Error - Unknown, Skipping Article')
            return {}, 404, total_token
        elif keyword_status_code == 201:
            # print('CHECK 6: Error - Content Warning Triggered, Fallback Keywords Returns [], Proceed')
            content_filter_triggered = True

        # print("CHECK 7: Keywords Categorisation")
        article_keywords_categorised, article_keywords = keyword_categorisation(article, key)

        if demo_config:  # Replacing article for demo purpose to reduce I/O
            article = ""

        # Append Final Results
        analysed_article = {
            'title': title,
            'date': article_dict['date'],
            'link': article_dict['link'],
            'full_article': article,
            'summary': summary,
            'sentiment': senti,
            'keywords': article_keywords,
            'keywords_categorised': article_keywords_categorised,
            'domain': domain_results,
            'content_filtered': content_filter_triggered
        }
        logger.info(f"---------- FINISHED ANALYSIS FOR ARTICLE # {n} ---------------")
        return analysed_article, 200, total_token
    else:
        logger.info(f"---------- NON-RELEVANT ARTICLE # {n} ---------------")
        return {}, 400, total_token


async def execute_analysis_pipeline_concurrent(news, name, domain, article_analysis_cap, flag, company, demo_config,
                                               batch_size_article_analysis, request, plot):
    if isinstance(domain, str):
        domain = [domain]

    final_news = []
    count = 0

    # Split the news into chunks of size `batch_size_article_analysis`
    chunks = [news[i:i + batch_size_article_analysis] for i in range(0, len(news), batch_size_article_analysis)]
    total_token = 0
    # Process one batch at a time
    for chunk_index, chunk in enumerate(chunks):
        logger.info(f"Processing chunk {chunk_index + 1}/{len(chunks)}...")  # Debug: track chunk processing
        # Process using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size_article_analysis) as executor:
            future_to_article = {}

            # Submit tasks for articles in the current chunk
            for n, article_dict in enumerate(chunk):
                future = executor.submit(run_analysis_pipeline_on_article, article_dict, n, name, domain, flag, company,
                                         demo_config, plot)
                future_to_article[future] = article_dict  # Map future to the article

            # Wait for the futures to complete
            for future in concurrent.futures.as_completed(future_to_article):
                article = future_to_article[future]
                try:
                    analysed_article, analysis_status_code, tokens = future.result()
                    total_token = total_token + tokens
                    if await request.is_disconnected():
                        logger.warning("Client disconnected, cancelling news extraction.")
                        raise HTTPException(status_code=499, detail="Client Closed Request")
                    if analysis_status_code == 429:
                        logger.error("Received 429: Too Many Requests, Halt Process and Return News Analysed So Far...")
                        if plot == True:
                            print("429 error")
                            return {},429, total_token
                        else:
                            return final_news, 429, total_token
                    if analysis_status_code == 404:
                        logger.error("Received 404: Uncategorised Error, Skipping")
                        if plot == True:
                            return {}, 404, total_token
                        else:
                            continue
                    if analysis_status_code == 400:
                        logger.info("Received 400: Unrelated Article, Skipping")
                        continue
                    if analysis_status_code == 200 or analysis_status_code == 201:
                        final_news.append(analysed_article)
                        if analysed_article['sentiment'].lower() == 'negative':
                            count += 1

                    logger.info(f"Processed article count: {count}")

                    if count >= article_analysis_cap:
                        logger.debug(f"Reached article analysis cap: {article_analysis_cap}")
                        break
                except Exception as e:
                    logger.debug("entered exception")
                    if plot == True:
                        return {}, 429, total_token
                    logger.error(f"Error processing article {article['title']}: {str(e)}")

        # Stop processing when cap is reached
        if count >= article_analysis_cap:
            logger.info(f"Stopping after processing {count} articles.")  # Debug: show cap reached
            break

        # Add delay to avoid hitting the API too fast and overwhelming the service, but only if cap isn't reached
        if count < article_analysis_cap:
            logger.info(f"Batch {chunk_index + 1} processed. Waiting before processing next batch...")
            time.sleep(10)

    logger.info(f"Final processed articles: {len(final_news)}")
    logger.info(f"total token used {total_token}")
    return final_news, 200, total_token


async def extract_article_content(news: list, request: Request) -> list:
    """
    Invoking custom crawling endpoint to extract the full news article content given the google news link

    :param news: list of dicts of format {'title': title, 'date': date, 'link': link} -> these fields are mandatory
    :return: news with ["full_article"] added to each
    """

    logger.info("Starting Article Extraction - Calling Scraper API (/scrapeArticles)")

    if not SCRAPER_URL:
        logger.error("SCRAPER_URL is not set (env var SCRAPER).")
        raise HTTPException(status_code=500, detail="Scraper URL not configured")

    url = f"{SCRAPER_URL}/scrapeArticles"
    headers = {"Content-Type": "application/json"}

    try:
        response = _scraper_session.post(url, headers=headers, json=news, timeout=SCRAPER_TIMEOUT)
    except requests.RequestException as e:
        logger.exception(f"Failed to call scraper at {url}: {e}")
        raise HTTPException(status_code=502, detail="Scraper connection failed")

    if response.status_code >= 400:
        body_preview = (response.text or "")[:500]
        logger.error(f"Scraper error {response.status_code} calling {url}. Body[:500]={body_preview}")
        raise HTTPException(status_code=502, detail=f"Scraper error {response.status_code}")

    try:
        article_scraping_result = response.json()
    except ValueError:
        body_preview = (response.text or "")[:500]
        logger.error(f"Scraper returned non-JSON response. Body[:500]={body_preview}")
        raise HTTPException(status_code=502, detail="Scraper returned invalid JSON")

    news = []
    for result in article_scraping_result:
        if await request.is_disconnected():
            logger.warning("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")
        if result["success"]:
            # at least 50 chars long for the extracted article - TODO should be handled better
            if len(result['scraped']['content']) > 50:
                flattened_result = result["original"]
                flattened_result.update(
                    {"scraped_title": result['scraped']['title'],
                     "full_article": result['scraped']['content'],
                     "scraping_timestamp": result['scraped']['timestamp'],
                     "scraping_contentlength": result['scraped']['contentLength'],
                     "scraping_success": result["success"]
                     })
                news.append(flattened_result)

    return news


def scrape_article(article):
    """Function to scrape a single article by making a POST request."""
    logger.info(f"Starting Article Extraction - Calling API for article {article.get('title')}")

    url = f"{SCRAPER_URL}/scrapeSingleArticle"
    payload = json.dumps(article)
    headers = {'Content-Type': 'application/json'}

    try:
        response = _scraper_session.post(url, headers=headers, json=article, timeout=SCRAPER_TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"Scraper call failed for article '{article.get('title')}'. Error={e}")
        return None

    if response.status_code >= 400:
        logger.warning(
            f"Scraper returned {response.status_code} for article '{article.get('title')}'. Body[:200]={(response.text or '')[:200]}"
        )
        return None

    try:
        result = response.json()
    except ValueError:
        logger.warning(
            f"Scraper returned non-JSON for article '{article.get('title')}'. Body[:200]={(response.text or '')[:200]}"
        )
        return None

    if result.get("success"):
        scraped = result.get("scraped") or {}
        content = scraped.get("content") or ""
        if len(content) > 50:
            flattened_result = result["original"]
            flattened_result.update(
                {
                    "scraped_title": scraped.get("title"),
                    "full_article": content,
                    "scraping_timestamp": scraped.get("timestamp"),
                    "scraping_contentlength": scraped.get("contentLength"),
                    "scraping_success": True,
                }
            )
            return flattened_result

    return None


def process_batch(batch):
    """Function to process a batch of articles concurrently."""
    with concurrent.futures.ThreadPoolExecutor() as executor:
        return list(executor.map(scrape_article, batch))


async def extract_article_content_concurrent(news: list, request: Request, batch_size_article_scraping) -> list:
    """
    Invoking custom crawling endpoint to extract the full news article content given the google news link

    :param news: list of dicts of format {'title': title, 'date': date, 'link': link} -> these fields are mandatory
    :return: news with ["full_article"] added to each
    """
    article_scraping_result = []
    batch_size = batch_size_article_scraping

    # Split news-batches of length "batch_size"
    for i in range(0, len(news), batch_size):
        batch = news[i:i + batch_size]
        logger.info(f"Processing batch {i // batch_size + 1} of {math.ceil(len(news) / batch_size)}")

        batch_results = process_batch(batch)  # Process the current batch concurrently
        article_scraping_result.extend(batch_results)  # add results

    article_scraping_result = [article for article in article_scraping_result if article]

    return article_scraping_result


# Function to extract links using aiohttp and Selectolax for parsing
async def news_link_extraction(name: str, company: str, start_date: date, end_date: date, country: str,
                               request: Request):
    """
    Extract the google news links for the provided search parameters
    :param name: name of entity
    :param start_date: required to create date range to iterate through
    :param end_date: same.
    :return: news - list of dicts of format: {'title': title, 'date': date, 'link': link}
    """
    duration = []
    # print(start_date,end_date.strftime("%Y-%m-%d"))
    if end_date.year != start_date.year:
        start_tuple = (start_date.strftime("%Y-%m-%d"), datetime(start_date.year, 12, 31).strftime("%Y-%m-%d"))
        duration.append(start_tuple)
        current_year = start_date.year + 1
        while current_year < end_date.year:
            current_year_tuple = (datetime(current_year, 1, 1).strftime("%Y-%m-%d"),
                                  datetime(current_year, 12, 31).strftime("%Y-%m-%d"))
            duration.append(current_year_tuple)
            current_year += 1

        end_date_tuple = (datetime(end_date.year, 1, 1).strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        duration.append(end_date_tuple)
    else:
        duration.append((start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")))

    # duration = list(range(start_date.year, end_date.year + 1))
    # print("duration", duration, start_date.year, end_date.year)
    # duration = [str(num) for num in duration]
    base_url = 'https://news.google.com/search?q='
    news = []
    # logger.debug(duration)

    async with aiohttp.ClientSession() as session:

        for n in duration:
            if await request.is_disconnected():
                logger.warning("Client disconnected, cancelling news extraction.")
                raise HTTPException(status_code=499, detail="Client Closed Request")
            duration_search = f'after:{n[0]} before:{n[1]}' if len(
                duration) > 1 else f'after:{start_date} before:{end_date}'
            country_specific_url = f'&gl={country}&&hl=en-{country}&ceid={country}:en'
            hco_url = base_url + urllib.parse.quote(f'{name} {company} {duration_search}') + country_specific_url
            logger.info(f"Fetching URL: {hco_url}")

            html = await fetch(session, hco_url)
            if html is None:
                logger.info(f"No content retrieved for year {n}")
                continue

            tree = HTMLParser(html)
            articles = tree.css('.D9SJMe .IFHyqb.DeXSAc')

            if not articles:
                logger.info(f'No news found for year: {n}')
                continue

            for article in articles:
                try:
                    if await request.is_disconnected():
                        logger.warning("Client disconnected, cancelling news extraction.")
                        raise HTTPException(status_code=499, detail="Client Closed Request")
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
                        logger.info(f"Invalid link found for article: {title}")
                except Exception as e:
                    logger.error(f"Error parsing article for year {n}: {e}")

            logger.info(f"{len(news)} articles found for year {n}")

        return news


# Function used in news_link_extraction
async def fetch(session, url, proxy=None, retries=1):
    global news_link_extraction_flag
    """
    Fetch the HTML content of a URL asynchronously with increased delay.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
    }

    try:
        async with session.get(url, headers=headers, proxy=proxy) as response:
            if response.status == 200:
                news_link_extraction_flag = True
                print("flag:",response.status,news_link_extraction_flag)
                # Increase delay to handle dynamic content
                await asyncio.sleep(10)  # Adjusted delay to ensure content loads
                return await response.text()
            elif response.status == 429:
                news_link_extraction_flag = False
                print("flag:", response.status, news_link_extraction_flag)
                logger.error(f"Received 429 for {url}, retrying after delay...")
                if retries > 0:
                    await asyncio.sleep(uniform(5, 10))  # Longer delay before retry
                    return await fetch(session, url, proxy, retries - 1)
                else:
                    return None
            else:
                logger.error(f"Failed to fetch {url} with status code {response.status}")
                return None
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None


# Process a single duration range asynchronously
async def process_duration(session, n, name, company, country, base_url):
    """
    Process a single duration range asynchronously.
    """
    try:
        duration_search = f'after:{n[0]} before:{n[1]}'
        country_specific_url = f'&gl={country}&hl=en-{country}&ceid={country}:en'
        hco_url = base_url + urllib.parse.quote(f'{name} {company} {duration_search}') + country_specific_url
        logger.info(f"Fetching URL: {hco_url}")

        html = await fetch(session, hco_url)
        if html is None:
            logger.info(f"No content retrieved for duration {n}")
            return []

        # Process HTML content (this is a placeholder for your actual parsing logic)
        news = []  # Replace with actual parsing logic
        tree = HTMLParser(html)
        articles = tree.css('.D9SJMe .IFHyqb.DeXSAc')

        if not articles:
            logger.info(f'No news found for year: {n}')

        async def process_article(article):
            """
            Process a single article asynchronously.
            """
            try:
                # if await request.is_disconnected():
                #     print("Client disconnected, cancelling news extraction.")
                #     raise HTTPException(status_code=499, detail="Client Closed Request")

                date_element = article.css_first('.hvbAAd')
                date = date_element.attributes.get('datetime')[:10] if date_element else None

                title_element = article.css_first('.JtKRv')
                title = title_element.text() if title_element else None

                link = title_element.attributes.get('href') if title_element else None
                if link:
                    link = urllib.parse.urljoin('https://news.google.com', link)

                if link:
                    return {'title': title, 'date': date, 'link': link}
                else:
                    logger.info(f"Invalid link found for article: {title}")
                    return None
            except Exception as e:
                logger.error(f"Error parsing article for year {n}: {e}")
                return None

        # Fetch articles concurrently
        tasks = [process_article(article) for article in articles]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out None values and exceptions
        news = [result for result in results if result is not None]

        logger.info(f"{len(news)} articles found for duration {n}")
        return news

    except Exception as e:
        logger.error(f"Error processing duration {n}: {e}")
        return []


# Main concurrent news extraction function
async def news_link_extraction_concurrent(name: str, company: str, start_date: date, end_date: date, country: str,
                                          request: Request):
    """
    Extract Google News links for the provided search parameters concurrently using asyncio.
    """
    logger.debug("Inside news_link_extraction_concurrent")
    duration = []
    if end_date.year != start_date.year:
        start_tuple = (start_date.strftime("%Y-%m-%d"), datetime(start_date.year, 12, 31).strftime("%Y-%m-%d"))
        duration.append(start_tuple)
        current_year = start_date.year + 1
        while current_year < end_date.year:
            current_year_tuple = (datetime(current_year, 1, 1).strftime("%Y-%m-%d"),
                                  datetime(current_year, 12, 31).strftime("%Y-%m-%d"))
            duration.append(current_year_tuple)
            current_year += 1

        end_date_tuple = (datetime(end_date.year, 1, 1).strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        duration.append(end_date_tuple)
    else:
        duration.append((start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")))

    base_url = 'https://news.google.com/search?q='
    news = []
    # logger.debug(duration)

    async with ClientSession() as session:
        tasks = [
            process_duration(session, n, name, company, country, base_url)
            for n in duration
        ]

        for task in asyncio.as_completed(tasks):
            if await request.is_disconnected():
                logger.error("Client disconnected, cancelling news extraction.")
                raise HTTPException(status_code=499, detail="Client Closed Request")

            try:
                result = await task
                news.extend(result)
            except Exception as e:
                logger.error(f"Error processing a task: {e}")

    return news


async def get_data(name: str, start_date: date, end_date: date, domain: str, flag: str, company: str, country: str,
                   request: Request, request_type: str) -> dict:
    """
    Main orchestrator function which runs the query analysis. Intakes the key parameters of the query and returns the
    full analysis
    :param name: name of the entity to be analysed
    :param start_date: start date from query - as int
    :param end_date: end year from query - as int
    :param domain: domain to relate the extracted news to the original query intent
    :return: response dictionary containing status code, message and
            "news" list -> list of dicts containing the extracted articles and analysis
    """
    total_tokens=0
    # BATCHING CONFIG FOR DEMO VS LOCAL
    demo_config = False
    plot = False
    if CONFIG_TYPE.lower() == "demo":
        demo_config = True

        if request_type == "single":
            article_attempts_allowed_cap = 20  # Allow extraction of up to this many articles
            article_analysis_cap = 20  # Once we are able to analyse this many relevant articles, terminate

            batch_size_article_scraping = 10
            batch_size_article_analysis = 10
        else:
            article_attempts_allowed_cap = 7  # Allow extraction of up to this many articles
            article_analysis_cap = 7  # Once we are able to analyse this many relevant articles, terminate

            batch_size_article_scraping = 7
            batch_size_article_analysis = 7
    else:
        article_attempts_allowed_cap = 20  # Allow extraction of up to this many articles
        article_analysis_cap = 20  # Once we are able to analyse this many relevant articles, terminate

        batch_size_article_scraping = 10
        batch_size_article_analysis = 10

    # Handle Other Cases of Country:
    if country.lower() == "zz":
        country = "US"

    # Dummy Case
    dummycheck = name.lower().replace(" ", "")
    if dummycheck in dummyData:
        time.sleep(5)
        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")
        return {"status": 200, "message": "successful", "data": dummyData[dummycheck]["data"],
                "keywords-data-agg": dummyData[dummycheck]["keywords-data-agg"]}

    start_link_scraping = datetime.now()
    logger.info(f"Begin News Screening Analysis for: {name} from {start_date} to {end_date}")

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    # Get Google News Links - and retry if unsuccessful
    if demo_config:
        news = await news_link_extraction_concurrent(name, company, start_date, end_date, country, request)
    else:
        news = await news_link_extraction(name, company, start_date, end_date, country, request)
    count = 0

    while not news and count < 5:
        count += 1
        logger.debug(f"Retrying... attempt {count}")
        # time.sleep(2)  # Wait before retrying
        news = await news_link_extraction(name, company, start_date, end_date, country, request)

    if not news:
        return {"status": 404, "message": "no news found", "data": []}

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    start_sorting = datetime.now()
    news = link_sorting_and_demo_reordering(news, 3, demo_flag=demo_config)

    # Decode News Links - Concurrency used here
    news = news[:article_attempts_allowed_cap]

    start_decoding = datetime.now()
    news = execute_decoding_concurrently(news)

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    # Scrape Article Content
    start_article_extraction = datetime.now()

    if demo_config:
        news = await extract_article_content_concurrent(news, request, batch_size_article_scraping)
    else:
        news = await extract_article_content(news, request)

    no_of_extracted_articles = len(news)

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    # Run Analysis Pipeline
    start_analysis = datetime.now()
    news, status_code, total_tokens = await execute_analysis_pipeline_concurrent(news, name, domain, article_analysis_cap, flag,
                                                                   company, demo_config, batch_size_article_analysis,
                                                                   request, plot)

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    # Run Keyword Aggregation
    start_keyword_agg = datetime.now()
    keywords_data_agg, keyword_agg_token = await keyword_aggregation(news, name, company)
    total_tokens = total_tokens + keyword_agg_token
    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    # Run Sentiment Date-wise Aggregation
    try:
        sentiment_data_agg = await sentiment_aggregation(news, plot)
    except:
        try:
            counts = defaultdict(lambda: defaultdict(int))
            for entry in news:
                date = entry["date"]
                sentiment = entry["sentiment"]
                counts[date][sentiment] += 1

            aggregated_sentiment_counts = []
            for date, sentiments in counts.items():
                result = dict()
                result["month"] = date
                result["negative"] = 0
                result["positive"] = 0
                result["neutral"] = 0
                for sentiment, count in sentiments.items():
                    result[sentiment] = count

                aggregated_sentiment_counts.append(result)

            sentiment_data_agg = aggregated_sentiment_counts
            sentiment_data_agg = sorted(sentiment_data_agg, key=lambda x: datetime.strptime(x.get('month'), '%Y-%m-%d'),
                                        reverse=False)  # simple resorting
        except:
            sentiment_data_agg = []

    stopped = datetime.now()

    # Force final sorting:
    if len(news):
        try:
            news = sorted(news, key=lambda x: datetime.strptime(x.get('date'), '%Y-%m-%d'),
                          reverse=True)  # simple resorting
        except:
            news = news

    if status_code == 429:
        logger.critical('--------- API LIMIT HIT --------', status_code)

    analysis_metadata = {
        "analysis_limit_hit": status_code == 429,
        "total_extracted_articles": no_of_extracted_articles,
        "final_relevant_articles": len(news)
    }

    logger.info(f"link extraction time: {start_decoding - start_link_scraping}")
    logger.info(f"decoding time: {start_article_extraction - start_decoding}")
    logger.info(f"article extraction: {start_analysis - start_article_extraction}")
    logger.info(f"analysis: {start_keyword_agg - start_analysis}")
    logger.info(f"aggregation process: {stopped - start_keyword_agg}")
    logger.info(f"Total Time ---- {stopped - start_link_scraping}")
    if (not len(news)) and (status_code == 429):
        response = {
            "status": 200,
            "message": "successful",
            "data": [],
            'keywords-data-agg': [],
            'sentiment-data-agg': [],
            "analysis-metadata": analysis_metadata
        }
    elif not len(news):
        response = {
            "status": 404,
            "message": "no news found",
            "data": []}
    else:
        response = {
            "status": 200,
            "message": "successful",
            "data": news,
            'keywords-data-agg': keywords_data_agg,
            'sentiment-data-agg': sentiment_data_agg,
            "analysis-metadata": analysis_metadata}

    return response
    # return {"status": 200, "message": "successful", "data": news, 'keywords-data-agg': keywords_data_agg, 'sentiment-data-agg':sentiment_data_agg, "analysis-metadata": analysis_metadata} if len(news) else {"status": 404, "message": "no news found", "data": []}


async def get_news_ens_data(name: str, start_date: date, end_date: date, domain: str, flag: str, company: str,
                            country: str,
                            request: Request, request_type: str,mode:str) -> dict:
    demo_config = False
    batch_size_article_scraping = 20
    batch_size_article_analysis = 10
    article_analysis_cap = 5
    no_of_extracted_articles = 0
    plot = True
    filtered_articles = None

    logger.info(f"========= RECEIVED REQUEST FOR -----> {name}, {start_date} - {end_date}")

    # Determine if the input is a country code or a country name
    if len(country) == 2 and country.isalpha():
        country_code = country.upper()
    else:
        # Load country data from JSON and convert country name to country code
        with open("country_data.json", "r") as file:
            country_data = json.load(file)
        country_code = get_country_code_google(country, country_data)
        if not country_code:
            country_code = "US"

    if country_code.lower() == "zz":
        country_code = "US"
    # if request_type == 'bulk':
    #     is_deleted = delete_articles_by_name_daterange_country(name, start_date, end_date, country_code)
    #     if is_deleted:
    #         print("Records deleted successfully.")
    #     else:
    #         print("No matching records found.")
    articles_in_db_date_range = check_existing_articles_in_db_for_daterange(name, start_date, end_date, country_code,mode)
    all_articles_in_db = check_existing_articles_in_db_with_name(name, country_code,mode)
    logger.debug(f"articles within date range: {len(articles_in_db_date_range)}")
    logger.debug(f"all articles in the db with negative sentiment {len(all_articles_in_db)}")
    logger.debug(f"article in db {len(articles_in_db_date_range)}, {len(all_articles_in_db)}")
    if articles_in_db_date_range:
        if start_date.year == 2026:
            logger.info("year 2026")
            date_strings = [
                to_date_str(x.get('end_date') or x.get('date'))
                for x in articles_in_db_date_range
                if x.get('end_date') or x.get('date')
            ]
            if not date_strings:
                start_date = None  # or datetime.today().date()
            else:
                start_date = datetime.strptime(max(date_strings), '%Y-%m-%d').date() + timedelta(days=1)

            logger.info(f'start date, {start_date}')
            if start_date >= end_date:
                article_analysis_cap=0
            else:
                article_analysis_cap = 5
                filtered_articles = [article for article in articles_in_db_date_range if article.get('sentiment') != 'N/A']
        else:
            logger.debug("data in database")
            article_analysis_cap = 0
    elif all_articles_in_db:
        article_analysis_cap = max(5 - len(all_articles_in_db), 0)
    else:
        article_analysis_cap = 5
    logger.info(f"the analysis cap: {article_analysis_cap}")
    start_link_scraping = datetime.now()
    if article_analysis_cap > 0:
        logger.info(f"Begin News Screening Analysis for: {name} from {start_date} to {end_date}")

        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")

        news = await news_link_extraction_concurrent(name, company, start_date, end_date, country_code, request)
        count = 0
        logger.info(f"no of articles scrapped{len(news)}")
        if not news and news_link_extraction_flag is False:
            # Last attempt failed with a 429 from Google News, not a transient
            # error. news_link_extraction() builds the same search URL and
            # hits the same endpoint, so retrying here would just repeat the
            # same rate limit rather than recover from it.
            logger.info("Skipping fallback retry — concurrent attempt was rate-limited (429)")
        else:
            while not news and count < 1:
                count += 1
                logger.info(f"Retrying... attempt {count}")
                news = await news_link_extraction(name, company, start_date, end_date, country_code, request)

        if not news:
            logger.info(f"news flag: {news_link_extraction_flag}")
            if news_link_extraction_flag == True:
                logger.info("no news found")
                article_data = {'name': name, 'title': 'N/A', 'category': 'N/A', 'summary': 'No News for this year',
                                'date': start_date, 'link': 'N/A', 'sentiment': 'N/A', 'content_filtered': False}
            else:
                logger.info("no news found")
                article_data = {'name': name, 'title': 'N/A', 'category': 'N/A', 'summary': 'News link extraction:429',
                                'date': start_date, 'link': 'N/A', 'sentiment': 'N/A', 'content_filtered': False}
            article_data = [article_data]
            delete_articles_by_name_daterange_country_error(name,start_date,end_date,country,mode)
            insert_article_into_db(all_articles=article_data, country=country_code, start_date=start_date, end_date=end_date,mode=mode)
            return {"status": 404, "message": "no news found", "data": []}

        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")

        start_decoding = datetime.now()
        news = link_sorting_and_demo_reordering(news, 3, demo_flag=demo_config)

        news = execute_decoding_concurrently(news)

        # print('example of decoding')

        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")

        # Scrape Article Content for new articles
        start_article_extraction = datetime.now()
        new_articles = await extract_article_content_concurrent(news, request, batch_size_article_scraping)
        no_of_extracted_articles = len(new_articles)
        # print("len of articles scrapped", no_of_extracted_articles)

        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")

        # Run Analysis Pipeline for new articles
        start_analysis = datetime.now()
        new_articles, status_code, total_token = await execute_analysis_pipeline_concurrent(new_articles, name, domain,
                                                                               article_analysis_cap, flag,
                                                                               company, demo_config,
                                                                               batch_size_article_analysis,
                                                                               request, plot)
        payload = {
            "name": name,
            "flag": flag,
            "company": company,
            "domain": domain,
            "start_date": start_date.strftime('%Y-%m-%d'),
            "end_date": end_date.strftime('%Y-%m-%d'),
            "country": country,
            "request_type": request_type
            }
        payload = json.dumps(payload)
        model = "ens-dev-gpt-4-32k" if require_llm_response_speed or (CONFIG_TYPE.lower() == "demo") else "gpt-4o"
        insert_token_usage_into_db(payload,total_token, model,mode)
        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")

        all_articles = []
        for article in new_articles:
            if article['sentiment'].lower() == 'negative':
                all_articles.append(article)
        if len(all_articles) > 0:
            logger.info(f"total negative article: {len(all_articles)}")
            delete_articles_by_name_daterange_country_error(name, start_date, end_date, country,mode)
            insert_article_into_db(all_articles, country_code, start_date, end_date,mode)
            if filtered_articles:
                all_articles = all_articles + filtered_articles
                all_articles = link_sorting_and_demo_reordering(all_articles, 3, demo_flag=demo_config)
                all_articles = all_articles[:5]
        else:
            if status_code == 429:
                article_data = {'name': name, 'title': 'N/A', 'category': 'N/A',
                                'summary': 'Error:429',
                                'date': start_date, 'link': 'N/A', 'sentiment': 'N/A',
                                'content_filtered': False}
                article_data = [article_data]
            elif status_code == 404:
                article_data = {'name': name, 'title': 'N/A', 'category': 'N/A',
                                'summary': 'Error:404',
                                'date': start_date, 'link': 'N/A', 'sentiment': 'N/A',
                                'content_filtered': False}
                article_data = [article_data]
            else:
                logger.info("no negative news found")
                article_data = {'name': name, 'title': 'N/A', 'category': 'N/A',
                                'summary': 'No Negative News for this year',
                                'date': start_date, 'link': 'N/A', 'sentiment': 'N/A', 'content_filtered': False}
                article_data = [article_data]
            delete_articles_by_name_daterange_country_error(name, start_date, end_date, country,mode)
            insert_article_into_db(all_articles=article_data, country=country_code, start_date=start_date, end_date=end_date,mode=mode)
    else:
        status_code = 200
        all_articles = link_sorting_and_demo_reordering(articles_in_db_date_range, 3, demo_config)
        all_articles = all_articles[:5]
        logger.info(f"article exist in DB {len(all_articles)}")

    # Run Sentiment Date-wise Aggregation
    try:
        sentiment_data_agg = await sentiment_aggregation(all_articles, plot)
    except Exception as e:
        try:
            counts = defaultdict(lambda: defaultdict(int))
            for entry in all_articles:
                date = entry["news_date"]
                sentiment = entry["sentiment"]

                counts[date][sentiment] += 1
            aggregated_sentiment_counts = []
            for date, sentiments in counts.items():
                result = dict()
                result["month"] = date
                result["negative"] = 0
                result["Not Negative"] = 0
                for sentiment, count in sentiments.items():
                    result[sentiment] = count
                aggregated_sentiment_counts.append(result)
            sentiment_data_agg = aggregated_sentiment_counts
            sentiment_data_agg = sorted(sentiment_data_agg, key=lambda x: datetime.strptime(x.get('month'), '%Y-%m-%d'),
                                        reverse=False)  # simple resorting
            logger.debug(sentiment_data_agg)
        except:
            sentiment_data_agg = []

    stopped = datetime.now()

    if len(all_articles):
        try:
            all_articles = sorted(all_articles, key=lambda x: datetime.strptime(x.get('date'), '%Y-%m-%d'),
                                  reverse=True)  # simple resorting
        except:
            all_articles = all_articles

    if status_code == 429:
        logger.critical(f"--------- API LIMIT HIT --------,{status_code}")

    analysis_metadata = {
        "analysis_limit_hit": status_code == 429,
        "total_extracted_articles": no_of_extracted_articles,
        "final_relevant_articles": len(all_articles)
    }
    try:
        logger.info(f"link extraction time: {start_decoding - start_link_scraping}")
    except NameError:
        logger.debug("link extraction skipped")

    try:
        logger.info(f"decoding time: {start_article_extraction - start_decoding}")
    except NameError:
        logger.debug("decoding skipped")

    try:
        logger.info(f"article extraction: {start_analysis - start_article_extraction}")
    except NameError:
        logger.debug("article extraction skipped")

    try:
        logger.info(f"analysis: {stopped - start_analysis}")
    except NameError:
        logger.info("analysis skipped")

    try:
        logger.debug(f"Total Time ---- {stopped - start_link_scraping}")
    except NameError:
        logger.debug("Total time calculation skipped")

    if (not len(all_articles)) and (status_code == 429):
        response = {
            "status": 429,
            "message": "API Limit Hit",
            "data": [],
            'keywords-data-agg': [],
            'sentiment-data-agg': [],
            "analysis-metadata": analysis_metadata
        }
    elif not len(all_articles):
        response = {
            "status": 404,
            "message": "no news found/uncategorized error",
            "data": []}
    else:
        response = {
            "status": 200,
            "message": "successful",
            "data": all_articles,
            'sentiment-data-agg': sentiment_data_agg,
            "analysis-metadata": analysis_metadata
        }
        # name = name.replace(" ", '_').strip()
        # file_name = f'{name}_{start_date}_{end_date}.json'
        # folder_name = 'plotdata'
        # file_path = os.path.join(folder_name, file_name)
        # if not os.path.exists(folder_name):
        #     os.makedirs(folder_name)
        # with open(file_path, "w") as json_file:
        #     json.dump(sentiment_data_agg, json_file, indent=2)

    return response


def get_country_code_google(country_name, country_data):
    for country in country_data:
        if country["countryName"].lower() == country_name.lower():
            return country["countryCode"]
    return None


def get_country_google(country_code, country_data):
    country_mapping = {entry['countryCode']: entry['countryName'] for entry in country_data}
    country = country_mapping.get(country_code, "not found")
    return country


async def get_google_link(name: str, country: str, request: Request, request_type: str, language: str) -> dict:
    batch_size_article_scraping = 10
    demo_config = False
    if CONFIG_TYPE.lower() == "demo":
        demo_config = True
    # Handle Other Cases of Country:
    if country.lower() == "zz":
        country = "US"

    logger.info(f"Begin News Screening Analysis for: {name}")

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    # Get Google Links - and retry if unsuccessful
    news = await google_link_extraction(name, country, request, language)
    count = 0
    while not news and count < 5:
        count += 1
        logger.debug(f"Retrying... attempt {count}")
        # time.sleep(2)  # Wait before retrying
        news = await bing_link_extraction(name, country, request, language)
    if not news:
        return {"status": 404, "message": "no news found", "data": []}

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    # Scrape Article Content
    news = await extract_article_content_concurrent(news, request, batch_size_article_scraping)

    if await request.is_disconnected():
        logger.error("Client disconnected, cancelling news extraction.")
        raise HTTPException(status_code=499, detail="Client Closed Request")

    return {"status": 200, "message": "successful", "data": news} if len(news) else {"status": 404,
                                                                                     "message": "no news found",
                                                                                     "data": []}


async def google_link_extraction(name: str, country: str, request: Request, language: str):
    """
    Extract the google news links for the provided search parameters
    :param name: name of entity
    :param start_date: required to create date range to iterate through
    :param end_date: same.
    :return: news - list of dicts of format: {'title': title, 'date': date, 'link': link}
    """
    base_url = 'https://www.google.com/search?q='
    results = []
    async with aiohttp.ClientSession() as session:
        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")
        country_specific_url = f'&gl={country}&hl={language}-{country}&ceid={country}:{language}'
        # country_specific_url = f'&gl={country}&hl=en-{country}&ceid={country}:en'
        hco_url = base_url + urllib.parse.quote(f'{name}') + country_specific_url
        logger.info(f"Fetching URL: {hco_url}")
        x = []
        html = await fetch(session, hco_url)
        if html is None:
            logger.info(f"No content retrieved")
            return x
        parser = HTMLParser(html)
        body_elements = parser.css(".Gx5Zad.xpd.EtOod.pkphOe")
        for n in range(2, len(body_elements)):
            title = body_elements[n - 1]
            title = title.css_first(".BNeawe.vvjwJb.AP7Wnd.UwRFLe")
            link = body_elements[n - 1].css_first('a')
            try:
                title = title.text()
            except:
                continue
            try:
                link = link.attributes.get('href')
                link = link.strip('/url?esrc=s&q=&rct=j&sa=U&url=')
                link = link.split('&ved=')[0]
            except:
                continue
            if title and link:
                x.append({'title': title, 'date': date, 'link': link})
    return x


async def bing_link_extraction(name: str, country: str, request: Request, language: str):
    # called when google extraction fails
    base_url = 'https://www.bing.com/search?q='
    results = []
    async with aiohttp.ClientSession() as session:
        if await request.is_disconnected():
            logger.error("Client disconnected, cancelling news extraction.")
            raise HTTPException(status_code=499, detail="Client Closed Request")
        country_specific_url = f'&cc={country}&mkt={language}-{country}'
        hco_url = base_url + urllib.parse.quote(f'{name}') + country_specific_url
        logger.info(f"Fetching URL: {hco_url}")
        x = []
        html = await fetch(session, hco_url)
        if html is None:
            logger.info(f"No content retrieved")
            return x
        parser = HTMLParser(html)
        body_element = parser.css_first(".b_respl")
        list_element = body_element.css_first("#b_content")
        list_element1 = list_element.css_first("#b_results")
        list_element2 = list_element1.css(".b_algo")
        for l in list_element2:
            title_element = l.css_first("h2")
            title_element = title_element.css_first("a")
            title = title_element.text()
            link = title_element.attributes.get('href')
            if title and link:
                x.append({'title': title, 'link': link})
    return x


from datetime import datetime, timedelta, date

def to_date_str(value):
    if isinstance(value, (datetime, date)):
        return value.strftime('%Y-%m-%d')
    return value  # assume it's already a string

