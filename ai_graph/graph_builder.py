"""
ai_graph/graph_builder.py — LangGraph routing state machine.

FIXES (confirmed from terminal logs):
  ✅ food_order: hardcoded "delivered to room  in 15-20 min" removed — LLM generates
  ✅ food_order: items can be list of str OR list of dict — both handled safely
  ✅ escalation: LLM told NOT to say "transfer" or "connect" — share number only
  ✅ event_inquiry: same — no transfer language
  ✅ farewell: should_end_call=True reliably set
  ✅ inquiry: no more hardcoded "I can only provide..." — LLM generates
  ✅ All nodes fully prompt-based, zero hardcoded response strings
"""

from typing import TypedDict, Annotated, List, Dict, Optional
import operator

from langgraph.graph import StateGraph, END
from loguru import logger

from agents.qwen_service import qwen_service
from rag.retrieval_service import retrieval_service
from database.mongodb import mongo_client


# ─────────────────────────────────────────────
# State
# ─────────────────────────────────────────────

class CallState(TypedDict):
    hotel_id: str
    hotel_name: str
    system_prompt: str
    manager_contact: str
    guest_number: str
    guest_room: str
    user_text: str
    intent: str
    extracted_items: List[str]
    messages: Annotated[List[Dict], operator.add]
    rag_context: str
    response_text: str
    should_escalate: bool
    should_end_call: bool


# ─────────────────────────────────────────────
# Helper: normalize items safely
# items can be ["pasta"] or [{"name": "idli", "quantity": 1}] — handle both
# ─────────────────────────────────────────────

def _normalize_items(items: list) -> List[str]:
    """Convert items to a flat list of strings regardless of input format."""
    result = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("item") or str(item)
            qty  = item.get("quantity") or item.get("qty")
            result.append(f"{qty} {name}" if qty else name)
        else:
            result.append(str(item))
    return result


# ─────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────

def route_by_intent(state: CallState) -> str:
    mapping = {
        "food_order":      "food_order",
        "room_cleaning":   "room_cleaning",
        "spa_service":     "spa_service",
        "essential_needs": "essential_needs",
        "inquiry":         "inquiry",
        "event_inquiry":   "event_inquiry",
        "escalation":      "escalation",
        "farewell":        "farewell",
    }
    return mapping.get(state.get("intent", "inquiry"), "inquiry")


# ─────────────────────────────────────────────
# LLM helper
# ─────────────────────────────────────────────

async def _llm(state: CallState, rag: str, override_user_text: str = "") -> str:
    user_content = override_user_text or state["user_text"]
    msgs = state["messages"] + [{"role": "user", "content": user_content}]
    return await qwen_service.get_full_response(
        messages=msgs,
        hotel_id=state["hotel_id"],
        hotel_name=state["hotel_name"],
        system_prompt=state["system_prompt"],
        rag_context=rag,
        manager_contact=state["manager_contact"],
        guest_room=state.get("guest_room", ""),
    )


def _next_msgs(state: CallState, response: str, user_content: str = "") -> List[Dict]:
    content = user_content or state["user_text"]
    return state["messages"] + [
        {"role": "user",      "content": content},
        {"role": "assistant", "content": response},
    ]


# ─────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────

async def food_order(state: CallState) -> Dict:
    # Normalize items — safe against str or dict format from intent classifier
    raw_items = state.get("extracted_items") or []
    items     = _normalize_items(raw_items)

    logger.info(f"🍽️ [FOOD] {state['hotel_id']} | room={state.get('guest_room')} | {items}")

    rag = await retrieval_service.search(state["user_text"], state["hotel_id"], top_k=4)

    if items:
        try:
            await mongo_client.upsert_food_order(
                hotel_id=state["hotel_id"],
                hotel_name=state["hotel_name"],
                guest_number=state["guest_number"],
                guest_room=state.get("guest_room", ""),
                items=items,
            )
        except Exception as e:
            logger.error(f"food_order DB error: {e}")

    items_str = ", ".join(items) if items else "the requested items"
    room      = state.get("guest_room", "")

    # LLM composes the confirmation — no hardcoded delivery time or room string
    instruction = (
        f"The guest has placed a food order. "
        f"Items ordered: {items_str}. "
        f"Guest room: {room if room else 'not yet provided'}. "
        f"Original request: \"{state['user_text']}\". "
        "Confirm the order warmly and naturally. "
        "Mention the items and room number. "
        "Use delivery time information from the hotel knowledge base if available — "
        "do NOT invent a time if it is not in the knowledge base. "
        "Keep it short. No bullet points."
    )

    response = await _llm(state, rag, override_user_text=instruction)

    return {
        "rag_context":     rag,
        "response_text":   response,
        "should_escalate": False,
        "should_end_call": False,
        "messages":        _next_msgs(state, response, user_content=state["user_text"]),
    }


async def room_cleaning(state: CallState) -> Dict:
    raw_items = state.get("extracted_items") or []
    items     = _normalize_items(raw_items) or [state["user_text"]]

    logger.info(f"🧹 [CLEANING] {state['hotel_id']} | room={state.get('guest_room')} | {items}")

    rag = await retrieval_service.search(state["user_text"], state["hotel_id"], top_k=3)

    try:
        await mongo_client.upsert_room_cleaning(
            hotel_id=state["hotel_id"],
            hotel_name=state["hotel_name"],
            guest_number=state["guest_number"],
            guest_room=state.get("guest_room", ""),
            requests=items,
        )
    except Exception as e:
        logger.error(f"room_cleaning DB error: {e}")

    response = await _llm(state, rag)
    return {
        "rag_context":     rag,
        "response_text":   response,
        "should_escalate": False,
        "should_end_call": False,
        "messages":        _next_msgs(state, response),
    }


async def spa_service(state: CallState) -> Dict:
    raw_items = state.get("extracted_items") or []
    items     = _normalize_items(raw_items) or [state["user_text"]]

    logger.info(f"💆 [SPA] {state['hotel_id']} | {items}")

    rag = await retrieval_service.search(state["user_text"], state["hotel_id"], top_k=4)

    try:
        await mongo_client.upsert_spa_service(
            hotel_id=state["hotel_id"],
            hotel_name=state["hotel_name"],
            guest_number=state["guest_number"],
            guest_room=state.get("guest_room", ""),
            services=items,
        )
    except Exception as e:
        logger.error(f"spa_service DB error: {e}")

    response = await _llm(state, rag)
    return {
        "rag_context":     rag,
        "response_text":   response,
        "should_escalate": False,
        "should_end_call": False,
        "messages":        _next_msgs(state, response),
    }


async def essential_needs(state: CallState) -> Dict:
    raw_items = state.get("extracted_items") or []
    items     = _normalize_items(raw_items) or [state["user_text"]]

    logger.info(f"🪥 [ESSENTIALS] {state['hotel_id']} | {items}")

    rag = await retrieval_service.search(state["user_text"], state["hotel_id"], top_k=3)

    try:
        await mongo_client.upsert_essential_needs(
            hotel_id=state["hotel_id"],
            hotel_name=state["hotel_name"],
            guest_number=state["guest_number"],
            guest_room=state.get("guest_room", ""),
            needs=items,
        )
    except Exception as e:
        logger.error(f"essential_needs DB error: {e}")

    response = await _llm(state, rag)
    return {
        "rag_context":     rag,
        "response_text":   response,
        "should_escalate": False,
        "should_end_call": False,
        "messages":        _next_msgs(state, response),
    }


async def inquiry(state: CallState) -> Dict:
    logger.info(f"❓ [INQUIRY] {state['hotel_id']} | {state['user_text'][:60]}")

    rag = await retrieval_service.search(
        state["user_text"],
        state["hotel_id"],
        top_k=5
    )

    try:
        await mongo_client.upsert_inquiry(
            hotel_id=state["hotel_id"],
            hotel_name=state["hotel_name"],
            guest_number=state["guest_number"],
            guest_room=state.get("guest_room", ""),
            question=state["user_text"],
        )
    except Exception as e:
        logger.error(f"inquiry DB error: {e}")

    # ─────────────────────────────────────────────
    # STRICT RAG VALIDATION
    # If no hotel knowledge found → LLM generates response
    # ─────────────────────────────────────────────

    if not rag or len(rag.strip()) < 40:
        logger.warning(
            f"⚠️ No valid hotel knowledge found | hotel_id={state['hotel_id']}"
        )

        instruction = (
            f"The guest asked a question, but there is no relevant information "
            f"about this topic in the {state['hotel_name']} hotel knowledge base. "
            f"Politely explain that you can only answer questions about hotel "
            f"services, facilities, menu, bookings, room services, and hotel policies. "
            f"Suggest they reach out to the manager if they need help. "
            f"Be warm and brief. Do NOT apologize excessively."
        )

        response = await _llm(state, rag="", override_user_text=instruction)

        return {
            "rag_context": "",
            "response_text": response,
            "should_escalate": False,
            "should_end_call": False,
            "messages": _next_msgs(state, response),
        }

    logger.debug(f"RAG len={len(rag)} | preview={rag[:200]}")

    # ─────────────────────────────────────────────
    # IMPORTANT:
    # Force LLM to answer ONLY from RAG
    # ─────────────────────────────────────────────

    strict_instruction = (
        "Answer ONLY using the hotel knowledge provided in RAG context. "
        "If the answer is not clearly available in the hotel knowledge, "
        "say that the information is unavailable in the hotel records. "
        "Do NOT answer general knowledge questions. "
        "Do NOT use your own knowledge."
    )

    response = await _llm(
        state,
        rag,
        override_user_text=(
            strict_instruction
            + "\n\nGuest Question:\n"
            + state["user_text"]
        )
    )

    return {
        "rag_context": rag,
        "response_text": response,
        "should_escalate": False,
        "should_end_call": False,
        "messages": _next_msgs(state, response),
    }


async def event_inquiry(state: CallState) -> Dict:
    logger.info(f"🎉 [EVENT] {state['hotel_id']} → manager")

    # LLM knows manager_contact from system prompt — no hardcoded number here
    instruction = (
        "The guest is asking about an event, party, or special occasion booking. "
        "Politely explain that event bookings are managed by our manager. "
        "Share the manager's contact number from your system context. "
        "Do NOT say you will 'call', 'transfer', or 'connect' the guest. "
        "Just provide the number so they can reach out themselves. "
        "Be warm and brief."
    )

    response = await _llm(state, rag="", override_user_text=instruction)

    return {
        "rag_context":     "",
        "response_text":   response,
        "should_escalate": False,
        "should_end_call": False,
        "messages":        _next_msgs(state, response, user_content=state["user_text"]),
    }


async def escalation(state: CallState) -> Dict:
    logger.info(f"🚨 [ESCALATION] {state['hotel_id']} → {state['manager_contact']}")

    # Key instruction: do NOT say "transfer" or "connect" — confirmed bug in terminal
    instruction = (
        "The guest wants to speak with a manager or has an issue. "
        "Acknowledge their concern warmly. "
        "Let them know you have noted their request. "
        "Share the manager's contact number from your system context. "
        "IMPORTANT: Do NOT say you will 'transfer', 'connect', or 'put them through'. "
        "Simply provide the manager's number and say they can call directly. "
        "Keep it brief and reassuring."
    )

    response = await _llm(state, rag="", override_user_text=instruction)

    return {
        "rag_context":     "",
        "response_text":   response,
        "should_escalate": True,
        "should_end_call": False,
        "messages":        _next_msgs(state, response, user_content=state["user_text"]),
    }


async def farewell(state: CallState) -> Dict:
    logger.info(f"👋 [FAREWELL] {state['hotel_id']}")

    instruction = (
        "The guest is ending the call and saying goodbye. "
        "Give a warm, brief farewell as a hotel concierge. "
        "Wish them a pleasant stay. Keep it to 1-2 sentences. "
        "Do NOT ask if there is anything else — the call is ending."
    )

    response = await _llm(state, rag="", override_user_text=instruction)

    return {
        "rag_context":     "",
        "response_text":   response,
        "should_escalate": False,
        "should_end_call": True,   # ← triggers Twilio hangup in websocket
        "messages":        _next_msgs(state, response, user_content=state["user_text"]),
    }


# ─────────────────────────────────────────────
# Graph assembly
# ─────────────────────────────────────────────

def build_graph():
    g = StateGraph(CallState)

    for name, fn in [
        ("food_order",      food_order),
        ("room_cleaning",   room_cleaning),
        ("spa_service",     spa_service),
        ("essential_needs", essential_needs),
        ("inquiry",         inquiry),
        ("event_inquiry",   event_inquiry),
        ("escalation",      escalation),
        ("farewell",        farewell),
    ]:
        g.add_node(name, fn)

    g.set_conditional_entry_point(
        route_by_intent,
        {n: n for n in ["food_order","room_cleaning","spa_service","essential_needs",
                         "inquiry","event_inquiry","escalation","farewell"]},
    )

    for node in ["food_order","room_cleaning","spa_service","essential_needs",
                 "inquiry","event_inquiry","escalation","farewell"]:
        g.add_edge(node, END)

    return g.compile()


hotel_graph = build_graph()


async def run_graph(
    hotel_id: str,
    hotel_name: str,
    system_prompt: str,
    manager_contact: str,
    user_text: str,
    intent: str,
    extracted_items: List[str],
    messages: List[Dict],
    guest_number: str,
    guest_room: str,
) -> CallState:
    initial: CallState = {
        "hotel_id":        hotel_id,
        "hotel_name":      hotel_name,
        "system_prompt":   system_prompt,
        "manager_contact": manager_contact,
        "guest_number":    guest_number,
        "guest_room":      guest_room,
        "user_text":       user_text,
        "intent":          intent,
        "extracted_items": extracted_items,
        "messages":        messages,
        "rag_context":     "",
        "response_text":   "",
        "should_escalate": False,
        "should_end_call": False,
    }
    return await hotel_graph.ainvoke(initial)