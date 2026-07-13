from fastapi import APIRouter, HTTPException, Request
from typing import List
from models.item_model import get_data, news_link_extraction_concurrent, get_news_ens_data
from schemas.item_schema import LinkExtractionRequest, BulkExtractionRequest, ArticleExtractionRequest, \
    AddressValidation, AddressValidationOrbis
from models.item_model import get_data, news_link_extraction_concurrent, get_google_link
from schemas.item_schema import  LinkExtractionRequest, BulkExtractionRequest, ArticleExtractionRequest, GoogleLinkExtractionRequest, BulkGoogleLinkExtractionRequest
from models.address_validation import *
from models.address_validation_orbis import *
from typing import Dict
import asyncio
import uuid
import time
from schemas.logger import logger
import random

router = APIRouter()

google_lock = asyncio.Lock()
last_google_hit = 0
THROTTLE_SECONDS = 2

# Create a link_extraction end-point
@router.post("/items/link_extraction", response_model=None, 
    summary="Extract Links Based on Input Data",
    description="""
    This endpoint accepts a POST request to extract links based on the input parameters.
    
    **Request Body:**
    - **name**: The name or topic to search for (required).
    - **start_date**: The start year for filtering results (optional).
    - **end_date**: The end year for filtering results (optional).
    - **domain**: The domain from which links should be extracted (optional).

    **Response:**
    The extracted items, or a 404 error if no items are found.

    **Errors:**
    - `404`: Items not found if no relevant data is available.
    """)
async def get_link_extraction_item(link_extraction_request: LinkExtractionRequest, request: Request):
    """
    View that handles the incoming POST request for link extraction.
    It passes the request data to the controller and returns the response.
    """
    # Call the controller to get the items
    items = await get_data(
        name=link_extraction_request.name,
        start_date=link_extraction_request.start_date,
        end_date=link_extraction_request.end_date,
        domain=link_extraction_request.domain,
        flag= link_extraction_request.flag,
        company=link_extraction_request.company,
        country=link_extraction_request.country,
        request= request,
        request_type=link_extraction_request.request_type
    )

    if not items:
        raise HTTPException(status_code=404, detail="Items not found")

    return items

@router.post("/items/news_ens_data", response_model=None,
             summary="Extract Links Based on Input Data",
             description="""
    This endpoint accepts a POST request to extract links based on the input parameters.
    """)
async def get_news_ens_item(link_extraction_request: LinkExtractionRequest, request: Request):
    # Call the controller to get the items
    items = await get_news_ens_data(
        name=link_extraction_request.name,
        start_date=link_extraction_request.start_date,
        end_date=link_extraction_request.end_date,
        domain=link_extraction_request.domain,
        flag=link_extraction_request.flag,
        company=link_extraction_request.company,
        country=link_extraction_request.country,
        request=request,
        request_type=link_extraction_request.request_type
    )
    if not items:
        raise HTTPException(status_code=404, detail="Items not found")
    return items

@router.post("/items/news_ens_data_throttle", response_model=None,
             summary="Extract Links Based on Input Data",
             description="""
    This endpoint accepts a POST request to extract links based on the input parameters.
    """)
async def get_news_ens_item(link_extraction_request: LinkExtractionRequest, request: Request):
    # Call the controller to get the items
    global last_google_hit
    async with google_lock:
        now = time.time()
        logger.info(f"{last_google_hit} || {now}")
        time_since_last = now - last_google_hit
        if time_since_last < THROTTLE_SECONDS:
            wait_time = THROTTLE_SECONDS - time_since_last
            await asyncio.sleep(wait_time)
        last_google_hit = time.time()
        logger.info(f"Start time: {last_google_hit}")
        logger.info("news screening triggered")
    await asyncio.sleep(random.uniform(0, 2))
    items = await get_news_ens_data(
        name=link_extraction_request.name,
        start_date=link_extraction_request.start_date,
        end_date=link_extraction_request.end_date,
        domain=link_extraction_request.domain,
        flag=link_extraction_request.flag,
        company=link_extraction_request.company,
        country=link_extraction_request.country,
        request=request,
        request_type=link_extraction_request.request_type,
        mode=link_extraction_request.mode
    )
    if not items:
        raise HTTPException(status_code=404, detail="Items not found")
    return items


@router.post("/items/google_link_extraction", response_model=None,
             summary="Extract Google Links Based on Input Data",
             description='google link single')
async def get_link_extraction_item(link_extraction_request: GoogleLinkExtractionRequest, request: Request):
    items = await get_google_link(
        name=link_extraction_request.name,
        country=link_extraction_request.country,
        request=request,
        request_type="single",
        language=link_extraction_request.language
    )

    if not items:
        raise HTTPException(status_code=404, detail="Items not found")

    return items


@router.post("/items/bulk_google_link_extraction", response_model=None, summary="Bulk Link Extraction Service",
             description='xyz')
async def get_link_extraction_item(bulk_extraction_request: BulkGoogleLinkExtractionRequest, request: Request):
    extracted_items_dict: Dict[str, list] = {}
    async def process_request_item(request_item):
        key = (f"{request_item.name}_[{request_item.country},{request_item.language}]")

        # Generate a UUID based on the hash of the unique string
        unique_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, key))
        # print("unique_key", unique_key)

        if key in extracted_items_dict:
            return
        # Call the controller to get the items for each request item
        items = await get_google_link(
            name=request_item.name,
            country=request_item.country,
            request=request,
            request_type="bulk",
            language=request_item.language
        )
        # Store items or handle the case where no items were found
        extracted_items_dict[key] = items if items else "No items found"

    # Run all request items concurrently
    await asyncio.gather(*(process_request_item(item) for item in bulk_extraction_request.bulk_request))

    # If all entries in the dictionary have the "No items found" message, raise a 404 error
    if all(value == "No items found" for value in extracted_items_dict.values()):
        raise HTTPException(status_code=404, detail="No items found for any request")

    return extracted_items_dict  # Return the dictionary with extracted items or messages

@router.post(
    "/items/address_validation",
    response_model=None,
    summary="Address Validation",
    description="Validates an address and classifies it as Residential, Commercial, Mixed, or Industrial using Google Places and LLM."
)
async def address_validation_controller(payload: AddressValidation, request: Request):
    result = await address_validation(payload)

    if result["status"] != 200:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=result["status"],
            detail=result.get("message", "Something went wrong")
        )

    return result


@router.post(
    "/items/address_validation_orbis",
    response_model=None,
    summary="Address Validation",
    description="Validates an address and classifies it as Residential, Commercial, Mixed, or Industrial using Google Places and LLM."
)
async def address_validation_controller(payload: AddressValidationOrbis, request: Request):
    result = await address_validation_orbis(payload)

    if result["status"] != 200:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=result["status"],
            detail=result.get("message", "Something went wrong")
        )

    return result


