import os
import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("NUTRITION_API_URL", "https://garmin-sleep-api.onrender.com")
API_KEY = os.environ.get("NUTRITION_API_KEY", "")

mcp = FastMCP("nutrition-logger")


@mcp.tool()
async def log_nutrition(
    date: str,
    meals: list[dict],
    bmr: int = 2030,
    exercise_calories: int = 0,
    deficit: int = 0,
    status: str = "partial",
) -> str:
    """Log daily nutrition for John Craig's Ironman training dashboard.

    Accepts meals with macros, exercise burn estimates, and BMR.
    Merges with existing data for the same date — so you can log
    breakfast, then lunch, then dinner across multiple calls.
    Protein target is 175g/day. BMR is 2030 cal.

    Args:
        date: Date in YYYY-MM-DD format (Central Time)
        meals: List of meal dicts, each with: item (str), calories (int), protein (int), carbs (int), fat (int)
        bmr: Basal metabolic rate, default 2030
        exercise_calories: Estimated exercise calories burned today
        deficit: Calculated caloric deficit
        status: "partial" or "complete" — whether this is a partial or complete day log
    """
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY

    body = {
        "date": date,
        "meals": meals,
        "bmr": bmr,
        "exercise_calories": exercise_calories,
        "deficit": deficit,
        "status": status,
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
