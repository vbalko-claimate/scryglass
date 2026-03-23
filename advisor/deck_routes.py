"""FastAPI routes for deck lifecycle management.

Thin wrappers around DeckService — no business logic here.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .deck_lifecycle import DeckService

router = APIRouter(prefix="/api/decks", tags=["decks"])


class CreateDeckRequest(BaseModel):
    name: str
    deck_list: str


class AddVersionRequest(BaseModel):
    deck_list: str


class GenerateRulesRequest(BaseModel):
    mode: str = "mechanical"  # or "mechanical+llm"


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


@router.post("/{deck_id}/undeploy")
async def undeploy_version(deck_id: str):
    svc = DeckService()
    try:
        return svc.undeploy_version(deck_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{deck_id}/promote")
async def promote_stub(deck_id: str):
    svc = DeckService()
    try:
        return svc.promote_stub(deck_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
