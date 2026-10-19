"""地点搜索结果是坐标的唯一来源；模型只引用已查到的地点编号。"""
import math


LOCATION_SCHEMA = {
    "type": ["string", "object", "null"],
    "description": "日程使用地点文字。提醒事项先用包含城市的地址文字查询 Apple 地图；再用返回的 place_id、trigger=arrive/leave 设置地点提醒，radius 为米（默认200）；null 清除地点提醒。不得编造地点编号或坐标。",
    "properties": {
        "place_id": {"type": "string", "minLength": 1},
        "trigger": {"type": "string", "enum": ["arrive", "leave"]},
        "radius": {"type": "integer", "minimum": 100, "maximum": 10000},
    },
    "required": ["place_id", "trigger"], "additionalProperties": False,
}


def validate_location(location):
    if not isinstance(location, dict) or set(location) != {
        "title", "address", "latitude", "longitude", "trigger", "radius"
    }:
        raise ValueError("地点提醒必须引用已查询的完整位置。")
    for key, bound in (("latitude", 90), ("longitude", 180)):
        value = location[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > bound:
            raise ValueError("地点坐标无效。")
    radius = location["radius"]
    if isinstance(radius, bool) or not isinstance(radius, int) or not 100 <= radius <= 10000:
        raise ValueError("地点提醒范围必须是100到10000米的整数。")
    if location["trigger"] not in {"arrive", "leave"} or not str(location["title"]).strip():
        raise ValueError("地点提醒需要名称和到达或离开条件。")
    return location


def location_summary(location):
    if location is None:
        return ["地点提醒：清除"]
    return ["地点：" + location["title"], *(["地址：" + location["address"]] if location["address"] else []),
            "地点提醒：" + ("到达时" if location["trigger"] == "arrive" else "离开时"),
            f"提醒范围：{location['radius']}米"]
