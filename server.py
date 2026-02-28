import os
import json
import uuid
import asyncio
import httpx
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import PlainTextResponse, JSONResponse
from starlette.requests import Request
from sse_starlette.sse import EventSourceResponse
import uvicorn

API_BASE = os.environ.get("NUTRITION_API_URL", "https://garmin-sleep-api.onrender.com")
API_KEY = os.environ.get("NUTRITION_API_KEY", "")

# In-memory session store
sessions: dict[str, asyncio.Queue] = {}

TOOL_DEF = {
    "name": "log_nutrition",
    "description": (
        "Log daily nutrition for John Craig's Ironman training dashboard.\n\n"
        "Accepts meals with macros, exercise burn estimates, and BMR.\n"
        "Merges with existing data for the same date — so you can log\n"
        "breakfast, then lunch, then dinner across multiple calls.\n"
        "Protein target is 175g/day. BMR is 2030 cal.\n\n"
        "Args:\n"
        "  date: Date in YYYY-MM-DD format (Central Time)\n"
        "  meals: List of meal dicts, each with: item (str), calories (int), protein (int), carbs (int), fat (int)\n"
        "  bmr: Basal metabolic rate, default 2030\n"
        "  exercise_calories: Estimated exercise calories burned today\n"
        "  deficit: Calculated caloric deficit\n"
        "  status: 'partial' or 'complete' — whether this is a partial or complete day log"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "Date in YYYY-MM-DD format (Central Time)"},
            "meals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {"type": "string"},
                        "calories": {"type": "integer"},
                        "protein": {"type": "integer"},
                        "carbs": {"type": "integer"},
                        "fat": {"type": "integer"},
                    },
                    "required": ["item", "calories", "protein", "carbs", "fat"],
                },
                "description": "List of meal dicts with item, calories, protein, carbs, fat",
            },
            "bmr": {"type": "integer", "default": 2030, "description": "Basal metabolic rate, default 2030"},
            "exercise_calories": {"type": "integer", "default": 0, "description": "Estimated exercise calories burned today"},
            "deficit": {"type": "integer", "default": 0, "description": "Calculated caloric deficit"},
            "status": {"type": "string", "default": "partial", "description": "'partial' or 'complete'"},
        },
        "required": ["date", "meals"],
    },
}


async def call_log_nutrition(arguments: dict) -> str:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    body = {
        "date": arguments["date"],
        "meals": arguments["meals"],
        "bmr": arguments.get("bmr", 2030),
        "exercise_calories": arguments.get("exercise_calories", 0),
        "deficit": arguments.get("deficit", 0),
        "status": arguments.get("status", "partial"),
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{API_BASE}/log-nutrition", json=body, headers=headers)

    if r.status_code != 200:
        return f"Error: {r.text}"

    data = r.json()
    return (
        f"Logged {data['meal_count']} meal(s) for {data['date']}\n"
        f"Calories: {data['totals']['calories']} | "
        f"Protein: {data['totals']['protein']}g | "
        f"Carbs: {data['totals']['carbs']}g | "
        f"Fat: {data['totals']['fat']}g\n"
        f"Protein: {data['totals']['protein']}g / {data['protein_target']}g target "
        f"({data['protein_remaining']}g remaining)"
    )


def handle_message(msg: dict) -> dict:
    """Process a JSON-RPC message and return a response."""
    method = msg.get("method", "")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "nutrition-logger", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": [TOOL_DEF]},
        }

    if method == "tools/call":
        # Will be handled async
        return "ASYNC_TOOL_CALL"

    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


async def handle_tool_call(msg: dict) -> dict:
    msg_id = msg.get("id")
    params = msg.get("params", {})
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})

    if tool_name != "log_nutrition":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"},
        }

    try:
        result_text = await call_log_nutrition(arguments)
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": result_text}],
                "isError": False,
            },
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                "isError": True,
            },
        }


async def sse_endpoint(request: Request):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue

    async def event_generator():
        # Send the endpoint URL as the first event
        yield {
            "event": "endpoint",
            "data": f"/messages?session_id={session_id}",
        }
        # Then wait for messages to send back to the client
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=30)
                yield {"event": "message", "data": json.dumps(msg)}
            except asyncio.TimeoutError:
                # Send keepalive comment
                yield {"comment": "keepalive"}
            except asyncio.CancelledError:
                break

    try:
        return EventSourceResponse(event_generator())
    finally:
        pass  # Cleanup happens when the generator exits


async def messages_endpoint(request: Request):
    session_id = request.query_params.get("session_id", "")
    if session_id not in sessions:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    body = await request.json()
    queue = sessions[session_id]

    response = handle_message(body)

    if response == "ASYNC_TOOL_CALL":
        # Handle tool call asynchronously
        response = await handle_tool_call(body)

    if response is not None:
        await queue.put(response)

    return JSONResponse({"ok": True}, status_code=202)


async def health(request: Request):
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/health", endpoint=health),
        Route("/sse", endpoint=sse_endpoint),
        Route("/messages", endpoint=messages_endpoint, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
