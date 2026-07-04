from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine

from apps.api.config import get_settings
from apps.api.db import get_engine
from apps.api.edgar.client import EdgarClient, FilingNotFoundError, TickerNotFoundError
from apps.api.edgar.sections import SectionExtractionError
from apps.api.research.repository import Repository
from apps.api.research.service import (
    FilingAlreadyIngestedError,
    ResearchService,
    UpstreamError,
)
from apps.api.research.summarizer import ClaudeSummarizer, SummarizationError

router = APIRouter()


@lru_cache
def get_research_service() -> ResearchService:
    settings = get_settings()
    return ResearchService(
        edgar=EdgarClient(user_agent=settings.sec_edgar_user_agent),
        summarizer=ClaudeSummarizer(api_key=settings.anthropic_api_key),
        engine=get_engine(),
    )


def get_read_engine() -> Engine:
    return get_engine()


class ResearchResponse(BaseModel):
    ticker: str
    company_id: int
    filing_id: int
    accession_number: str
    filing_url: str
    summaries: dict[str, str]
    thesis_snapshot_id: int


class SummaryResponse(BaseModel):
    ticker: str
    company_name: str
    accession_number: str
    filing_date: str
    filing_url: str
    summaries: dict[str, str]
    thesis: str


@router.post("/research/{ticker}", response_model=ResearchResponse)
def run_research(
    ticker: str, service: Annotated[ResearchService, Depends(get_research_service)]
) -> ResearchResponse:
    try:
        outcome = service.run(ticker)
    except (TickerNotFoundError, FilingNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FilingAlreadyIngestedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SectionExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (UpstreamError, SummarizationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ResearchResponse(ticker=ticker.upper(), **vars(outcome))


@router.get("/companies/{ticker}/summary", response_model=SummaryResponse)
def latest_summary(
    ticker: str, engine: Annotated[Engine, Depends(get_read_engine)]
) -> SummaryResponse:
    with engine.connect() as conn:
        view = Repository(conn).latest_research(ticker)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no research stored for {ticker!r}")
    return SummaryResponse(
        ticker=view.ticker,
        company_name=view.company_name,
        accession_number=view.accession_number,
        filing_date=view.filing_date.isoformat(),
        filing_url=view.filing_url,
        summaries=view.summaries,
        thesis=view.thesis,
    )
