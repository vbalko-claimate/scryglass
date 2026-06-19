"""FastAPI routes for deck lifecycle management.

Thin wrappers around DeckService — no business logic here.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import engine_backend
from .deck_lifecycle import DeckService

router = APIRouter(prefix="/api/decks", tags=["decks"])


class CreateDeckRequest(BaseModel):
    name: str
    deck_list: str


class AddVersionRequest(BaseModel):
    deck_list: str


class GenerateRulesRequest(BaseModel):
    mode: str = "mechanical"  # or "mechanical+llm"


class OptimizeRequest(BaseModel):
    games: int = 12
    steps: int = 3


class ApplySuggestionRequest(BaseModel):
    cut: str
    add: str
    confidence: str = ""
    basis: str = ""


class PortfolioRequest(BaseModel):
    iters: int = 150
    games: int = 12


@router.get("")
async def list_decks():
    svc = DeckService()
    return svc.list_decks()


@router.post("")
async def create_deck(req: CreateDeckRequest):
    svc = DeckService()
    try:
        return svc.create_deck(req.name, req.deck_list)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{deck_id}")
async def get_deck(deck_id: str):
    svc = DeckService()
    result = svc.get_deck(deck_id)
    if not result:
        raise HTTPException(status_code=404, detail="Deck not found")
    return result


@router.delete("/{deck_id}")
async def delete_deck(deck_id: str):
    svc = DeckService()
    svc.delete_deck(deck_id)
    return {"ok": True}


@router.post("/{deck_id}/versions")
async def add_version(deck_id: str, req: AddVersionRequest):
    svc = DeckService()
    try:
        return svc.add_version(deck_id, req.deck_list)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{deck_id}/versions/{v}/generate-rules")
async def generate_rules(deck_id: str, v: int, req: GenerateRulesRequest):
    svc = DeckService()
    try:
        return svc.generate_rules(deck_id, v, req.mode)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{deck_id}/versions/{v}/deploy")
async def deploy_version(deck_id: str, v: int):
    svc = DeckService()
    try:
        return svc.deploy_version(deck_id, v)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{deck_id}/versions/{v}/apply-suggestion")
async def apply_suggestion(deck_id: str, v: int, req: ApplySuggestionRequest):
    """Apply an optimizer swap as a new version AND log it for outcome tracking."""
    svc = DeckService()
    try:
        return svc.apply_suggestion(
            deck_id, v, req.cut, req.add, req.confidence, req.basis
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{deck_id}/recommendations")
async def list_recommendations(deck_id: str):
    """Accepted optimizer suggestions for this deck, each enriched with the
    record of matches played since it was applied (the outcome-loop readout)."""
    from .deck_lifecycle import recommendation_outcomes

    return recommendation_outcomes(deck_id)


@router.post("/{deck_id}/versions/{v}/portfolio")
async def deck_portfolio(deck_id: str, v: int, req: PortfolioRequest):
    """Diverse build variants for a version's decklist (MAP-Elites portfolio)."""
    svc = DeckService()
    detail = svc.get_deck(deck_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Deck not found")
    version = next(
        (ver for ver in detail.get("versions", []) if ver.get("version_number") == v),
        None,
    )
    if not version:
        raise HTTPException(status_code=404, detail=f"Version {v} not found")
    deck_list = (version.get("deck_list") or "").strip()
    if not deck_list:
        raise HTTPException(status_code=400, detail="Version has no decklist")
    report = await engine_backend.portfolio_deck(deck_list, iters=req.iters, games=req.games)
    if report is None:
        raise HTTPException(
            status_code=503,
            detail="Engine optimizer unavailable (is the glass-server sidecar running?)",
        )
    return report


@router.post("/{deck_id}/versions/{v}/optimize")
async def optimize_version(deck_id: str, v: int, req: OptimizeRequest):
    """Run the engine optimizer on a version's decklist → cut→add suggestions
    + manabase analysis. Delegates the sim to the glass-server /optimize
    sidecar; returns 503 if it isn't reachable."""
    svc = DeckService()
    detail = svc.get_deck(deck_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Deck not found")
    version = next(
        (ver for ver in detail.get("versions", []) if ver.get("version_number") == v),
        None,
    )
    if not version:
        raise HTTPException(status_code=404, detail=f"Version {v} not found")
    deck_list = (version.get("deck_list") or "").strip()
    if not deck_list:
        raise HTTPException(status_code=400, detail="Version has no decklist to optimize")
    report = await engine_backend.optimize_deck(
        deck_list, games=req.games, steps=req.steps
    )
    if report is None:
        raise HTTPException(
            status_code=503,
            detail="Engine optimizer unavailable (is the glass-server sidecar running?)",
        )
    return report


@router.post("/{deck_id}/undeploy")
async def undeploy_version(deck_id: str):
    svc = DeckService()
    try:
        return svc.undeploy_version(deck_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{deck_id}/versions/{v}/decklist")
async def update_decklist(deck_id: str, v: int, req: AddVersionRequest):
    svc = DeckService()
    try:
        return svc.update_decklist(deck_id, v, req.deck_list)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{deck_id}/promote")
async def promote_stub(deck_id: str):
    svc = DeckService()
    try:
        return svc.promote_stub(deck_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
