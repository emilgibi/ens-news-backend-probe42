from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Enum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
import enum

Base = declarative_base()
BaseOrbis = declarative_base()


class StepStatus(str, enum.Enum):
    not_called = "not_called"
    failed     = "failed"
    passed     = "passed"


class GeocodeSource(str, enum.Enum):
    not_called = "not_called"
    google     = "google"
    llm        = "llm"


class AddressZoneMaster(Base):
    __tablename__ = "address_zone_master"

    id               = Column(Integer,     autoincrement=True)
    geo_id           = Column(String(50),  primary_key=True)
    name             = Column(String(255), nullable=True)
    address          = Column(Text,        nullable=True)
    lat              = Column(Float,       nullable=True)
    lng              = Column(Float,       nullable=True)
    identifier       = Column(String(255), nullable=True)
    identifier_type  = Column(String(100), nullable=True)
    entity_type      = Column(String(100), nullable=True)
    places           = Column(JSONB,       nullable=True)
    zone             = Column(String(50),  nullable=True)
    confidence       = Column(Integer,     nullable=True)
    reason           = Column(Text,        nullable=True)
    geocode_status   = Column(Enum(StepStatus,    name="step_status_enum",    schema="public"), nullable=True)
    places_status    = Column(Enum(StepStatus,    name="step_status_enum",    schema="public"), nullable=True)
    llm_status       = Column(Enum(StepStatus,    name="step_status_enum",    schema="public"), nullable=True)
    geocode_source   = Column(Enum(GeocodeSource, name="geocode_source_enum", schema="public"), nullable=True)
    created_at       = Column(DateTime(timezone=True), nullable=True)
    updated_at       = Column(DateTime(timezone=True), nullable=True)

class AddressZoneMasterOrbis(BaseOrbis):
    __tablename__ = "address_zone_master"

    id               = Column(Integer,     autoincrement=True)
    geo_id           = Column(String(50),  primary_key=True)
    name             = Column(String(255), nullable=True)
    address          = Column(Text,        nullable=True)
    lat              = Column(Float,       nullable=True)
    lng              = Column(Float,       nullable=True)
    bvd_id           = Column(String(255), nullable=True)
    places           = Column(JSONB,       nullable=True)
    zone             = Column(String(50),  nullable=True)
    confidence       = Column(Integer,     nullable=True)
    reason           = Column(Text,        nullable=True)
    geocode_status   = Column(Enum(StepStatus,    name="step_status_enum",    schema="public"), nullable=True)
    places_status    = Column(Enum(StepStatus,    name="step_status_enum",    schema="public"), nullable=True)
    llm_status       = Column(Enum(StepStatus,    name="step_status_enum",    schema="public"), nullable=True)
    geocode_source   = Column(Enum(GeocodeSource, name="geocode_source_enum", schema="public"), nullable=True)
    created_at       = Column(DateTime(timezone=True), nullable=True)
    updated_at       = Column(DateTime(timezone=True), nullable=True)