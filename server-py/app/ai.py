"""DeepSeek narration via the Anthropic-compatible endpoint. Port of the Node
ai.ts: thinking disabled (mandatory for this model), en output must not leak
CJK, one 3s-delayed retry, bounded in-memory cache."""
import asyncio
import os
import re
import time

import httpx

BASE = "https://api.deepseek.com/anthropic/v1/messages"
MODEL = "deepseek-v4-flash"
CJK = re.compile(r"\p{Script=Han}", re.UNICODE) if hasattr(re, "p") else re.compile(r"[一-鿿]")

SYSTEM = "\n".join([
    "你是 RWA Meme Radar（股票相关 Meme 关系雷达）的数据解说员，为页面生成一段简短解读。",
    "硬性规则：",
    "1. 只依据输入 JSON 里给出的字段解读；引用大数字时可四舍五入到便于阅读的精度（如 1615829362 写作约 16.2 亿），但不得改变数量级，不得外推或编造。",
    "2. 不预测价格涨跌，不给任何买卖建议，不用『可能上涨/值得买入』类措辞。",
    "3. 数据缺失就直接说缺失，不推测原因。",
    "4. 关系结论必须与 status 字段一致：verified 是已核验配对；仅有名称匹配是线索；无记录就是无关。",
    "5. 输出 120-180 字（英文 80-120 词），2-4 句，直接给结论，不写开场白和免责声明。",
    "6. 不要使用 Markdown 格式，输出纯文本。",
])


def ai_enabled() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


class _Cache:
    def __init__(self, cap=500, low=400):
        self.map: dict[str, tuple[str, float, str]] = {}
        self.cap = cap
        self.low = low

    def get(self, key, lang):
        hit = self.map.get(key)
        if hit and hit[2] == lang and time.time() - hit[1] < float("inf"):
            return hit
        return None

    def set(self, key, text, lang, ttl):
        self.map[key] = (text, time.time(), lang)
        while len(self.map) > self.cap:
            self.map.pop(next(iter(self.map)))
            if len(self.map) <= self.low:
                break


CACHE = _Cache()


async def ai_narrate(key: str, lang: str, ttl_ms: int, data, task: str, force: bool = False):
    hit = CACHE.get(key, lang)
    now = time.time() * 1000
    if not force and hit and now - hit[1] < ttl_ms:
        return {"text": hit[0], "at": int(hit[1]), "lang": lang, "cached": True}
    token = os.environ.get("DEEPSEEK_API_KEY")
    if not token:
        return None

    async def attempt():
        try:
            payload = {
                "model": MODEL,
                "max_tokens": 8000,
                "thinking": {"type": "disabled"},
                "system": SYSTEM,
                "messages": [{
                    "role": "user",
                    "content": (f"Write in English. {task}" if lang == "en" else f"用简体中文输出。{task}")
                    + "\n\n数据 JSON：\n" + __import__("json").dumps(data, ensure_ascii=False),
                }],
            }
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(BASE, json=payload, headers={
                    "x-api-key": token,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                })
            if resp.status_code >= 400:
                print(f"[ai] {key}/{lang} HTTP {resp.status_code} {resp.text[:150]}")
                return None
            body = resp.json()
            text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text").strip()
            if not text:
                print(f"[ai] {key}/{lang} empty text (stop_reason={body.get('stop_reason')})")
                return None
            if lang == "en" and CJK.search(text):
                print(f"[ai] {key}/{lang} leaked CJK characters")
                return None
            return text
        except Exception as e:
            print(f"[ai] {key}/{lang} {e}")
            return None

    result = await attempt()
    if not result:
        await asyncio.sleep(3)
        result = await attempt()
    if not result:
        return None
    at = time.time() * 1000
    CACHE.set(key, result, lang, ttl_ms)
    return {"text": result, "at": int(at), "lang": lang, "cached": False}
