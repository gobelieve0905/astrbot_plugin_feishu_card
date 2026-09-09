"""CardKit API only; reuses the adapter client, never opens a websocket."""
import asyncio
import json
import uuid

from lark_oapi.api.cardkit.v1 import (
    Card, CreateCardRequest, CreateCardRequestBody,
    UpdateCardRequest, UpdateCardRequestBody,
)


class DeliveryError(RuntimeError):
    pass


class Transport:
    def __init__(self, bot):
        self.bot = bot

    async def create(self, body):
        req = CreateCardRequest.builder().request_body(
            CreateCardRequestBody.builder().type("card_json").data(json.dumps(body, ensure_ascii=False)).build()
        ).build()
        resp = await asyncio.wait_for(self.bot.cardkit.v1.card.acreate(req), 12)
        if not resp.success() or not resp.data or not resp.data.card_id:
            raise DeliveryError(f"card_create:{resp.code}")
        return resp.data.card_id

    async def update(self, card_id, body, sequence):
        req = UpdateCardRequest.builder().card_id(card_id).request_body(
            UpdateCardRequestBody.builder().card(Card.builder().type("card_json").data(
                json.dumps(body, ensure_ascii=False)).build()).sequence(sequence).uuid(str(uuid.uuid4())).build()
        ).build()
        resp = await asyncio.wait_for(self.bot.cardkit.v1.card.aupdate(req), 12)
        if not resp.success():
            raise DeliveryError(f"card_update:{resp.code}")
