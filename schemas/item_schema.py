from pydantic import BaseModel, Field, HttpUrl, constr, conlist
from typing import List, Optional
from datetime import date
from enum import Enum

# Enum for sentiment
class Sentiment(str, Enum):
    neg = "negative"
    neutral = "neutral"
    positive = "positive"

# Enum for flag
class Flag(str, Enum):
    poi = "POI"
    entity = "Entity"

class RequestType(str, Enum):
    single = "single"
    bulk = "bulk"

class NewsItem(BaseModel):
    title: str
    date: Optional[date]  # The date is optional if it's not always available
    link: HttpUrl  # Using HttpUrl to validate proper URL format

    def __getitem__(self, item):
        return getattr(self, item)

class ArticleExtractionRequest(BaseModel):
    news: List[NewsItem]  # A list of news items (articles)
    name: str
    domain: str

class LinkExtractionRequest(BaseModel):
    name: str = Field(..., description="Mandatory name field.")
    flag: Flag = Field(..., description='"POI" or "Entity" - Mandatory')
    company: Optional[str] = Field(None, description="Optional company name.")
    domain: Optional[conlist(str, max_length=3)] = Field(None, description="Optional list of strings, maximum 3.")
    start_date: date = Field(..., description="Mandatory start date in YYYY-MM-DD format.")
    end_date: date = Field(..., description="Mandatory end date in YYYY-MM-DD format.")
    country: constr(strip_whitespace=True) = Field(..., description="Mandatory country field.")
    request_type: RequestType = Field(..., description='"single" or "bulk" - Mandatory')
    mode: str = Field(..., description='"probe42 or orbis" - Mandatory')
    
    class Config:
        class Config:
            json_schema_extra = {  # Updated from schema_extra to json_schema_extra
                "example": {
                    "name": "Sample Entity",
                    "flag": "POI",
                    "company": "Sample Company",
                    "domain": ["Tech", "Finance", "Health"],
                    "start_date": "2024-10-07",
                    "end_date": "2024-11-19",
                    "country": "USA",
                    "request_type": "single"
                }
            }

class BulkExtractionRequest(BaseModel):
    bulk_request: List[LinkExtractionRequest]


class GoogleLinkExtractionRequest(BaseModel):
    name: str = Field(..., description="Mandatory name field.")
    country: constr(strip_whitespace=True) = Field(..., description="Mandatory country field.")
    language: constr(strip_whitespace=True) = Field(..., description="Mandatory country field.")
    request_type: str = Field(..., description="Mandatory name field.")

    class Config:
        class Config:
            json_schema_extra = {  # Updated from schema_extra to json_schema_extra
                "example": {
                    "name": "Sample Entity",
                    "country": "USA",
                    "language": "English",
                    "request_type": "single"
                }
            }


class BulkGoogleLinkExtractionRequest(BaseModel):
    bulk_request: List[GoogleLinkExtractionRequest]

class AddressValidation(BaseModel):
    name: str
    address: str
    identifier: str
    identifier_type: str
    entity_type: str

class AddressValidationOrbis(BaseModel):
    name: str
    address: str
    bvd_id: str