import os
from dotenv import load_dotenv
load_dotenv()
import psycopg2
from datetime import datetime, date
from psycopg2 import sql
from schemas.logger import *

def get_db_connection():
    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT")
    )
    return conn

def get_db_connection_orbis():
    conn = psycopg2.connect(
        dbname=os.getenv("ORBIS_DB_NAME"),
        user=os.getenv("ORBIS_DB_USER"),
        password=os.getenv("ORBIS_DB_PASSWORD"),
        host=os.getenv("ORBIS_DB_HOST"),
        port=os.getenv("ORBIS_DB_PORT")
    )
    return conn


def insert_article_into_db(all_articles, country, start_date, end_date, mode):
    conn = get_db_connection() if mode=='probe42' else get_db_connection_orbis()
    cur = conn.cursor()
    try:
        for article_data in all_articles:
            insert_query = sql.SQL("""
                INSERT INTO public.news_master (
                    name, title, category, summary, news_date,
                    link, sentiment, content_filtered, country,
                    start_date, end_date
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name, link, news_date) DO UPDATE 
                SET category = EXCLUDED.category, 
                    summary = EXCLUDED.summary, 
                    news_date = EXCLUDED.news_date, 
                    sentiment = EXCLUDED.sentiment, 
                    content_filtered = EXCLUDED.content_filtered,
                    start_date = EXCLUDED.start_date,
                    end_date = EXCLUDED.end_date;
            """)

            # Ensure date formatting
            def format_date(value):
                if isinstance(value, (date, datetime)):
                    return value.strftime('%Y-%m-%d')
                return value
            values = (
                article_data['name'],
                article_data['title'],
                article_data['category'],
                article_data['summary'],
                format_date(article_data['date']),
                article_data['link'],
                article_data['sentiment'],
                bool(article_data['content_filtered']),
                country,
                format_date(start_date),
                format_date(end_date),
            )
            # Debug: log values if needed
            query_with_values = insert_query.as_string(conn) % tuple(
                f"'{v}'" if isinstance(v, str) else str(v) for v in values
            )
            logger.debug(f"Executing query: {query_with_values}")

            cur.execute(insert_query, values)

        conn.commit()
        logger.info("Inserted successfully.")
    except Exception as e:
        logger.error(f"Error insert_article_into_db: {str(e)}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

def insert_token_usage_into_db(payload, token_used, model,mode):
    conn = get_db_connection() if mode.lower() == 'probe42' else get_db_connection_orbis()
    cur = conn.cursor()
    try:
        insert_query = sql.SQL("""
            INSERT INTO public.token_monitor (
                payload, token_used, openai_model
            )
            VALUES (%s, %s, %s);
        """)

        values = (
            payload,
            token_used,
            model
        )
        # Debug: log values if needed
        query_with_values = insert_query.as_string(conn) % tuple(
            f"'{v}'" if isinstance(v, str) else str(v) for v in values
        )
        logger.debug(f"Executing query: {query_with_values}")

        cur.execute(insert_query, values)

        conn.commit()
        logger.info("Inserted successfully.")
    except Exception as e:
        logger.error(f"Error insert_article_into_db: {str(e)}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

async def check_existing_articles_in_db_with_link(news: list, name: str,mode) -> list:
    conn = get_db_connection() if mode.lower() == 'probe42' else get_db_connection_orbis()
    cur = conn.cursor()
    existing_articles = []

    try:
        for article in news:
            link = article['link']
            select_query = sql.SQL("""
                SELECT name, title, category, summary, news_date, link, sentiment, content_filtered FROM public.news_master 
                WHERE link = %s AND LOWER(name) = %s
            """)
            cur.execute(select_query, (link, name.lower()))
            result = cur.fetchone()

            if result:
                existing_articles.append({
                    'name': result[0],
                    'title': result[1],
                    'category': result[2],
                    'summary': result[3],
                    'date': result[4].strftime('%Y-%m-%d'),
                    'link': result[5],
                    'sentiment': result[6],
                    'content_filtered': result[7]
                })
    except Exception as e:
        logger.error(f"check_existing_articles_in_db_with_link: {e}")
    finally:
        cur.close()
        conn.close()

    return existing_articles

def check_existing_articles_in_db_for_daterange(name: str, start_date, end_date, country,mode) -> list:
    conn = get_db_connection() if mode.lower() == 'probe42' else get_db_connection_orbis()
    cur = conn.cursor()
    existing_articles = []
    error = ('Error:429', 'Error:404', 'News link extraction:429')
    placeholders = sql.SQL(', ').join([sql.Placeholder() for _ in error])
    try:
        select_query = sql.SQL("""
                SELECT name, title, category, summary, news_date, link, sentiment, content_filtered, start_date, end_date
                FROM public.news_master 
                WHERE LOWER(name) = %s AND news_date BETWEEN %s AND %s AND LOWER(country) = %s AND summary NOT IN ({})
            """).format(placeholders)
        params = (name.lower(), start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), country.lower()) + error

        cur.execute(select_query, params)
        logger.debug("SQL Query: %s", select_query.as_string(conn))
        logger.debug("Query Parameters: %s", params)
        results = cur.fetchall()
        existing_articles = []
        if results:
            for result in results:
                existing_articles.append({
                    'name': result[0],
                    'title': result[1],
                    'category': result[2],
                    'summary': result[3],
                    'date': result[4].strftime('%Y-%m-%d'),
                    'link': result[5],
                    'sentiment': result[6],
                    'content_filtered': result[7],
                    'start_date': result[8],
                    'end_date': result[9]
                })
    except Exception as e:
        logger.error(f"Error check_existing_articles_in_db_for_daterange: {str(e)}")
    finally:
        cur.close()
        conn.close()

    return existing_articles

def check_existing_articles_in_db_with_name(name: str, country,mode) -> list:
    conn = get_db_connection() if mode.lower() == 'probe42' else get_db_connection_orbis()
    cur = conn.cursor()
    existing_articles = []

    try:
        select_query = sql.SQL("""
            SELECT name, title, category, summary, news_date, link, sentiment, content_filtered, start_date, end_date
            FROM public.news_master 
            WHERE LOWER(name) = %s AND LOWER(country) = %s AND LOWER(sentiment) = 'negative'
        """)

        cur.execute(select_query, (name.lower(), country.lower()))
        query_with_values = select_query.as_string(conn) % (name, country)
        logger.debug(f"Executing query: {query_with_values}")

        results = cur.fetchall()
        if results:
            for result in results:
                existing_articles.append({
                    'name': result[0],
                    'title': result[1],
                    'category': result[2],
                    'summary': result[3],
                    'date': result[4].strftime('%Y-%m-%d'),
                    'link': result[5],
                    'sentiment': result[6],
                    'content_filtered': result[7],
                    'start_date': result[8],
                    'end_date': result[9]
                })
    except Exception as e:
        logger.error(f"Error check_existing_articles_in_db_with_name: {str(e)}")
    finally:
        cur.close()
        conn.close()

    return existing_articles


def delete_articles_by_name_daterange_country(name: str, start_date, end_date, country: str,mode) -> bool:
    conn = get_db_connection() if mode.lower() == 'probe42' else get_db_connection_orbis()
    cur = conn.cursor()
    deleted = False

    try:
        delete_query = sql.SQL("""
                DELETE FROM public.news_master
                WHERE LOWER(name) = %s AND news_date BETWEEN %s AND %s AND LOWER(country) = %s
            """)

        cur.execute(delete_query, (name.lower(), start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), country.lower()))
        conn.commit()  # Commit the transaction

        deleted = cur.rowcount > 0  # Check if any rows were deleted
        logger.info(f"Deleted {cur.rowcount} records from news_master.")

    except Exception as e:
        logger.error(f"Error delete_articles_by_name_daterange_country: {str(e)}")
        conn.rollback()  # Rollback in case of error
    finally:
        cur.close()
        conn.close()

    return deleted  # Returns True if deletion was successful, False otherwise


def delete_articles_by_name_daterange_country_error(name: str, start_date, end_date, country: str,mode) -> bool:
    conn = get_db_connection() if mode.lower() == 'probe42' else get_db_connection_orbis()
    cur = conn.cursor()
    deleted = False

    try:
        delete_query = sql.SQL("""
                DELETE FROM public.news_master
                WHERE LOWER(name) = %s AND news_date BETWEEN %s AND %s AND LOWER(country) = %s AND sentiment = 'N/A'
            """)

        cur.execute(delete_query, (name.lower(), start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"), country.lower()))
        conn.commit()  # Commit the transaction

        deleted = cur.rowcount > 0  # Check if any rows were deleted
        logger.info(f"Deleted {cur.rowcount} records from news_master.")

    except Exception as e:
        logger.error(f"Error delete_articles_by_name_daterange_country: {str(e)}")
        conn.rollback()  # Rollback in case of error
    finally:
        cur.close()
        conn.close()

    return deleted

