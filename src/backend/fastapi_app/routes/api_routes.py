import json
import logging
from collections.abc import AsyncGenerator

import fastapi
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from fastapi_app.api_models import ChatRequest, ItemPublic, ItemWithDistance, Message, RetrievalResponse
from fastapi_app.dependencies import ChatClient, CommonDeps, DBSession, EmbeddingsClient
from fastapi_app.postgres_models import Item
from fastapi_app.postgres_searcher import PostgresSearcher
from fastapi_app.rag_advanced import AdvancedRAGChat
from fastapi_app.rag_simple import SimpleRAGChat

router = fastapi.APIRouter()


async def format_as_ndjson(r: AsyncGenerator[RetrievalResponse | Message, None]) -> AsyncGenerator[str, None]:
    """
    Format the response as NDJSON
    """
    try:
        async for event in r:
            yield json.dumps(event.model_dump(), ensure_ascii=False) + "\n"
    except Exception as error:
        logging.exception("Exception while generating response stream: %s", error)
        yield json.dumps({"error": str(error)}, ensure_ascii=False) + "\n"


@router.get("/items/{id}", response_model=ItemPublic)
async def item_handler(database_session: DBSession, id: int) -> ItemPublic:
    """A simple API to get an item by ID."""
    item = (await database_session.scalars(select(Item).where(Item.id == id))).first()
    if not item:
        raise HTTPException(detail=f"Item with ID {id} not found.", status_code=404)
    return ItemPublic.model_validate(item.to_dict())


@router.get("/similar", response_model=list[ItemWithDistance])
async def similar_handler(database_session: DBSession, id: int, n: int = 5) -> list[ItemWithDistance]:
    """A similarity API to find items similar to items with given ID."""
    item = (await database_session.scalars(select(Item).where(Item.id == id))).first()
    if not item:
        raise HTTPException(detail=f"Item with ID {id} not found.", status_code=404)
    closest = await database_session.execute(
        select(Item, Item.embedding.l2_distance(item.embedding))
        .filter(Item.id != id)
        .order_by(Item.embedding.l2_distance(item.embedding))
        .limit(n)
    )
    return [
        ItemWithDistance.model_validate(item.to_dict() | {"distance": round(distance, 2)}) for item, distance in closest
    ]


@router.get("/search", response_model=list[ItemPublic])
async def search_handler(
    context: CommonDeps,
    database_session: DBSession,
    openai_embed: EmbeddingsClient,
    query: str,
    top: int = 5,
    enable_vector_search: bool = True,
    enable_text_search: bool = True,
) -> list[ItemPublic]:
    """A search API to find items based on a query."""
    searcher = PostgresSearcher(
        db_session=database_session,
        openai_embed_client=openai_embed.client,
        embed_deployment=context.openai_embed_deployment,
        embed_model=context.openai_embed_model,
        embed_dimensions=context.openai_embed_dimensions,
    )
    results = await searcher.search_and_embed(
        query, top=top, enable_vector_search=enable_vector_search, enable_text_search=enable_text_search
    )
    return [ItemPublic.model_validate(item.to_dict()) for item in results]


@router.post("/chat", response_model=RetrievalResponse)
async def chat_handler(
    context: CommonDeps,
    database_session: DBSession,
    openai_embed: EmbeddingsClient,
    openai_chat: ChatClient,
    chat_request: ChatRequest,
):
    overrides = chat_request.context.get("overrides", {})

    searcher = PostgresSearcher(
        db_session=database_session,
        openai_embed_client=openai_embed.client,
        embed_deployment=context.openai_embed_deployment,
        embed_model=context.openai_embed_model,
        embed_dimensions=context.openai_embed_dimensions,
    )
    if overrides.get("use_advanced_flow"):
        run_ragchat = AdvancedRAGChat(
            searcher=searcher,
            openai_chat_client=openai_chat.client,
            chat_model=context.openai_chat_model,
            chat_deployment=context.openai_chat_deployment,
        ).run
    else:
        run_ragchat = SimpleRAGChat(
            searcher=searcher,
            openai_chat_client=openai_chat.client,
            chat_model=context.openai_chat_model,
            chat_deployment=context.openai_chat_deployment,
        ).run

    response = await run_ragchat(chat_request.messages, overrides=overrides)
    return response


@router.post("/chat/stream")
async def chat_stream_handler(
    context: CommonDeps,
    database_session: DBSession,
    openai_embed: EmbeddingsClient,
    openai_chat: ChatClient,
    chat_request: ChatRequest,
):
    overrides = chat_request.context.get("overrides", {})

    searcher = PostgresSearcher(
        db_session=database_session,
        openai_embed_client=openai_embed.client,
        embed_deployment=context.openai_embed_deployment,
        embed_model=context.openai_embed_model,
        embed_dimensions=context.openai_embed_dimensions,
    )
    if overrides.get("use_advanced_flow"):
        run_ragchat = AdvancedRAGChat(
            searcher=searcher,
            openai_chat_client=openai_chat.client,
            chat_model=context.openai_chat_model,
            chat_deployment=context.openai_chat_deployment,
        ).run_stream
    else:
        run_ragchat = SimpleRAGChat(
            searcher=searcher,
            openai_chat_client=openai_chat.client,
            chat_model=context.openai_chat_model,
            chat_deployment=context.openai_chat_deployment,
        ).run_stream

    result = run_ragchat(chat_request.messages, overrides=overrides)
    return StreamingResponse(content=format_as_ndjson(result), media_type="application/x-ndjson")
