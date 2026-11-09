"""Functions the model can call.

Each tool is an OpenAI-style schema (what the model sees, via the chat template)
plus a Python implementation (what the app runs when the model asks for it).
The "local" group runs on this Mac and never touches the network; the "web" group
drives a private headless Chrome through agent-browser (app/browser.py) and is offered
only when Web is on.
"""

import ast
import asyncio
import json
import math
import operator
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import browser

REMINDERS = Path(__file__).resolve().parent.parent / "data" / "reminders.json"
_reminder_lock = threading.Lock()


# ---- get_current_time ------------------------------------------------------
def _local_zone_name():
    target = os.path.realpath("/etc/localtime")  # e.g. /var/db/timezone/zoneinfo/Asia/Dubai
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def get_current_time(timezone: str = ""):
    timezone = str(timezone or "").strip() or _local_zone_name()
    try:
        now = datetime.now(ZoneInfo(timezone)) if timezone else datetime.now().astimezone()
    except (ZoneInfoNotFoundError, ValueError):
        return {"error": f"Unknown time zone {timezone!r}. Use an IANA name such as Asia/Dubai."}
    return {
        "timezone": timezone or str(now.tzinfo),
        "date": now.strftime("%A %d %B %Y"),
        "time": now.strftime("%H:%M"),
        "utc_offset": now.strftime("%z"),
    }


# ---- calculate ---------------------------------------------------------------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log, "log10": math.log10,
    "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (
            abs(right) > 1000 or abs(left) > 1 and math.log10(abs(left)) * abs(right) > 10_000
        ):
            raise ValueError("that number would be too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(arg) for arg in node.args])
    raise ValueError("only numbers, + - * / // % **, parentheses and basic math functions are allowed")


def calculate(expression: str):
    cleaned = str(expression).replace(",", "").replace("×", "*").replace("÷", "/").replace("^", "**")
    cleaned = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", cleaned)  # "17.5%" -> (17.5/100)
    try:
        value = _eval(ast.parse(cleaned, mode="eval"))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        return {"expression": expression, "error": str(exc)}
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    elif isinstance(value, float):
        value = round(value, 10)
    return {"expression": expression, "result": value}


# ---- reminders -----------------------------------------------------------------
def _load_reminders():
    try:
        return json.loads(REMINDERS.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def add_reminder(text: str, when: str = ""):
    with _reminder_lock:
        reminders = _load_reminders()
        item = {"text": str(text), "when": str(when), "saved_at": time.strftime("%Y-%m-%d %H:%M")}
        reminders.append(item)
        REMINDERS.parent.mkdir(parents=True, exist_ok=True)
        REMINDERS.write_text(json.dumps(reminders, indent=2, ensure_ascii=False))
    return {"saved": True, **item, "total_reminders": len(reminders),
            "note": "Saved to the reminder list in this app. It does not send notifications or alarms."}


def list_reminders():
    reminders = _load_reminders()
    return {"count": len(reminders), "reminders": reminders}


def clear_reminders():
    with _reminder_lock:
        REMINDERS.unlink(missing_ok=True)


# ---- registry ------------------------------------------------------------------
def _schema(name, description, properties=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
    }}


TOOLS = {  # name -> (group, schema, function)
    "get_current_time": ("local",
        _schema("get_current_time", "Get the current date and time, optionally in another time zone.",
                {"timezone": {"type": "string", "description": "IANA time zone such as Asia/Dubai or Europe/London. Leave empty for the user's local time."}}),
        get_current_time),
    "calculate": ("local",
        _schema("calculate", "Evaluate an arithmetic expression exactly. Use it for any calculation.",
                {"expression": {"type": "string", "description": "For example (6 - 2) * 3.5 or 17.5% * 2340 or sqrt(2)"}},
                ["expression"]),
        calculate),
    "add_reminder": ("local",
        _schema("add_reminder", "Save a reminder for the user.",
                {"text": {"type": "string", "description": "What to be reminded about"},
                 "when": {"type": "string", "description": "When, in the user's words"}},
                ["text"]),
        add_reminder),
    "list_reminders": ("local",
        _schema("list_reminders", "List the reminders the user has saved."),
        list_reminders),
    "web_search": ("web",
        _schema("web_search", "Search the web. Use it for news, prices, weather, opening hours and other facts that change or that you don't know.",
                {"query": {"type": "string", "description": "What to search for"}},
                ["query"]),
        browser.web_search),
    "open_page": ("web",
        _schema("open_page", "Open a web page in the browser and read its text and the numbered things on it: links, buttons and text boxes.",
                {"url": {"type": "string", "description": "A full address such as https://example.com, or the number of a search result"}},
                ["url"]),
        browser.open_page),
    "click": ("web",
        _schema("click", "Click a link, button or checkbox on the page that is open in the browser, by its number.",
                {"number": {"type": "integer", "description": "The element's number from the open page"}},
                ["number"]),
        browser.click),
    "type_text": ("web",
        _schema("type_text", "Type into a text box on the page that is open in the browser, by its number. Set submit to true to press Enter afterwards, for example to run a search.",
                {"number": {"type": "integer", "description": "The text box's number from the open page"},
                 "text": {"type": "string", "description": "What to type"},
                 "submit": {"type": "boolean", "description": "Press Enter after typing"}},
                ["number", "text"]),
        browser.type_text),
    "go_back": ("web",
        _schema("go_back", "Go back to the previous page in the browser."),
        browser.go_back),
}
WEB_TOOLS = {name for name, (group, _, _) in TOOLS.items() if group == "web"}


def schemas(web=False):
    return [schema for group, schema, _ in TOOLS.values() if web or group == "local"]


DESCRIPTIONS = [{"name": n, "group": g, "description": s["function"]["description"]} for n, (g, s, _) in TOOLS.items()]

_CALL = re.compile(r"<tool_call>\s*<function=([^>\n]+)>(.*?)</function>\s*</tool_call>", re.DOTALL)
_PARAM = re.compile(r"<parameter=([^>\n]+)>\n?(.*?)\n?</parameter>", re.DOTALL)


def parse_tool_calls(text):
    """The model writes calls as <tool_call><function=name><parameter=key>value</parameter>..."""
    calls = []
    for name, body in _CALL.findall(text):
        arguments = {}
        for key, value in _PARAM.findall(body):
            try:
                arguments[key.strip()] = json.loads(value)
            except json.JSONDecodeError:
                arguments[key.strip()] = value
        calls.append({"type": "function", "function": {"name": name.strip(), "arguments": arguments}})
    return calls


def strip_tool_markup(text):
    return re.sub(r"<tool_call>.*?(</tool_call>|$)", "", text, flags=re.DOTALL).strip()


async def run_tool(call, web=False):
    """Run one call. Problems come back as {"error": ...} so the model can read them."""
    name = call["function"]["name"]
    arguments = call["function"].get("arguments") or {}
    if name not in TOOLS or (name in WEB_TOOLS and not web):
        return {"error": f"There is no tool called {name}."}
    fn = TOOLS[name][2]
    try:
        if asyncio.iscoroutinefunction(fn):
            return await fn(**arguments)
        return await asyncio.to_thread(fn, **arguments)
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}
    except Exception as exc:
        return {"error": f"{name} failed: {exc}"}


def for_model(result):
    """The result without the page-only fields (those start with an underscore)."""
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if not k.startswith("_")}
    return result
