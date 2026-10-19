"""Weather lookup tool using Open-Meteo."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from telos.agent.tools.types import ExecutableTool, create_executable_tool


GEOCODING_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
WeatherTransport = Callable[[str, dict[str, Any]], dict[str, Any]]

WMO_CODES = {
    0: "晴",
    1: "基本晴",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "中毛毛雨",
    55: "大毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "冰粒",
    80: "阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷阵雨",
    96: "雷阵雨夹小冰雹",
    99: "雷阵雨夹大冰雹",
}


def create_weather_tool(*, transport: WeatherTransport | None = None) -> ExecutableTool:
    return create_executable_tool(
        name="get_weather",
        description="查询指定城市的实时天气信息。",
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "minLength": 1,
                    "description": "城市名，支持中文和英文，例如 上海、Beijing、Tokyo。",
                }
            },
            "required": ["city"],
            "additionalProperties": False,
        },
        handler=lambda args: get_weather(str(args["city"]), transport=transport),
    )


def get_weather(
    city: str,
    *,
    transport: WeatherTransport | None = None,
) -> dict[str, Any]:
    clean_city = str(city or "").strip()
    if not clean_city:
        raise ValueError("city is required")

    sender = transport or _send_open_meteo
    geo = sender(
        GEOCODING_ENDPOINT,
        {"name": clean_city, "count": 1, "language": "zh"},
    )
    results = geo.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError(f"找不到城市：{clean_city}")

    location = results[0]
    if not isinstance(location, dict):
        raise ValueError("Open-Meteo geocoding result must be an object")
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if latitude is None or longitude is None:
        raise ValueError("Open-Meteo geocoding result is missing latitude or longitude")
    city_name = str(location.get("name") or clean_city)

    weather = sender(
        FORECAST_ENDPOINT,
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "weather_code,wind_speed_10m"
            ),
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        },
    )
    current = weather.get("current")
    if not isinstance(current, dict):
        raise ValueError("Open-Meteo forecast response is missing current weather")

    weather_code = _int_value(current.get("weather_code"), "weather_code")
    description = WMO_CODES.get(weather_code, f"代码{weather_code}")
    result = {
        "city": city_name,
        "description": description,
        "temperature_2m": current.get("temperature_2m"),
        "apparent_temperature": current.get("apparent_temperature"),
        "relative_humidity_2m": current.get("relative_humidity_2m"),
        "wind_speed_10m": current.get("wind_speed_10m"),
        "message": (
            f"{city_name} 当前：{description}，"
            f"温度 {current.get('temperature_2m')}°C"
            f"（体感 {current.get('apparent_temperature')}°C），"
            f"湿度 {current.get('relative_humidity_2m')}%，"
            f"风速 {current.get('wind_speed_10m')} km/h"
        ),
    }
    return result


def _send_open_meteo(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{endpoint}?{query}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = response.read().decode("utf-8")
    except Exception as error:  # noqa: BLE001 - normalize tool boundary failures.
        raise ValueError(f"天气查询失败：{error}") from error
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("Open-Meteo response must be a JSON object")
    return parsed


def _int_value(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an integer") from error
