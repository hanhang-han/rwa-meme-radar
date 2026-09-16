"""Daily AI briefing: change-oriented snapshot assembled from the unified
data, narrated by DeepSeek in zh/en, pre-generated every 30 minutes with the
streak ledger. Ports the Node briefing pipeline including its hard wording
rules."""
import json
import os
import re
import time
from datetime import datetime, timezone

from . import state
from .ai import ai_enabled, ai_narrate

STREAK_FILE = "data/ai-streaks.json"
ASCII_ONLY = re.compile(r"^[\x20-\x7E]+$")

BRIEFING_TASK = """为加密雷达首页写今日简报。必须用以下固定结构（节标题逐字保留，中文版用中文标题，英文版用 Today in one line / Worth a look / Caution / Data bounds）：
今日一句话：<一句话给今天定性，点出最重要的一个发现或异常>
值得看：
- <2到4条，每条一个资产，资产符号必须用【】包裹（如【BNC】），格式：资产+关键数字+流动性定性+为什么值得看>
警惕：
- <1到3条风险信号：同一ticker在24小时内出现3条以上同名新币属批量碰币模式要点名；仅名称匹配无已验证关系；疑似池子异常>
数据边界：<一句话覆盖率>
规则：
1. liquidityTierLabel 只是流动性规模描述，不是可交易性结论；严禁使用"可交易""适合买入"等判断词，只陈述"池流动性约X美元"；低于$1千的可补充"进出场可能困难"。
2. streakDays 大于等于2的资产标注"连续第N天上榜"（英文 the Nth straight day）。
3. 新增关系记录数必须表述为"新增关系记录X条（含未核验名称线索）"，严禁说成"已验证关系"或"已核验关系"。
4. 同名新币数量只能描述为"同名新币密集出现，符合批量发行特征"，不得断言"骗局"或"碰瓷模式成立"；无已核验关系就说"仅有名称匹配"。
5. 正股类代币跌幅超过50%时表述为"跌幅远超正股市场正常范围，价格数据待核查"，不下"异常"结论以外的判断。
6. 流动性为0或字段缺失的配对不进"值得看"；只依据输入JSON的数字，不预测涨跌不给建议；英文版禁止出现中文；总长150到220字。"""


def _load_streaks() -> dict:
    try:
        return json.load(open(STREAK_FILE))
    except Exception:
        return {}


def _save_streaks(hist: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STREAK_FILE), exist_ok=True)
        json.dump(hist, open(STREAK_FILE, "w"), ensure_ascii=False)
    except Exception:
        pass


def streak_of(hist: dict, symbol: str, current: list[str]) -> int:
    def has(i: int) -> bool:
        d = datetime.fromtimestamp(time.time() - i * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
        return symbol in (hist.get(d) or []) or (i == 0 and symbol in current)

    n = 0
    for i in range(15):
        if has(i):
            n += 1
        elif i != 0:
            break
    return n


def update_streaks(symbols: list[str]) -> None:
    hist = _load_streaks()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hist[today] = symbols
    cutoff = datetime.now(timezone.utc).timestamp() - 15 * 86400
    for d in list(hist):
        try:
            if datetime.fromisoformat(d).timestamp() < cutoff:
                del hist[d]
        except ValueError:
            del hist[d]
    _save_streaks(hist)


def tier_label(v, lang: str) -> str | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if lang == "en":
        return "pool liquidity ≥$10k" if n >= 10000 else ("pool liquidity $1k–$10k" if n >= 1000 else "pool liquidity <$1k")
    return "池流动性≥$1万" if n >= 10000 else ("池流动性$1千-$1万" if n >= 1000 else "池流动性<$1千")


async def briefing_base() -> dict:
    u = (await state.DATA.payload())["unified"]
    day_ago = time.time() * 1000 - 86_400_000
    verified_relations = [r for r in u["relations"] if r.get("status") == "verified"]
    verified_tokens = {r["token"] for r in verified_relations}

    new_verified_raw = [
        {"ticker": r.get("ticker"), "chain": r.get("chainName", "X Layer"),
         "poolLiquidityUsd": r.get("liquidityUsd") if (r.get("liquidityUsd") or 0) > 0 else None}
        for r in verified_relations if (r.get("firstSeen") or 0) >= day_ago
    ][:8]

    movers_candidates = [
        a for a in u["assets"]
        if a.get("token") in verified_tokens and a.get("change24h") is not None
    ]
    try:
        movers_candidates.sort(key=lambda a: -abs(float(a["change24h"])))
    except (TypeError, ValueError):
        pass
    mover_symbols = [a.get("symbol") for a in movers_candidates[:4]]
    hist = _load_streaks()
    movers = [
        {
            "symbol": a.get("symbol"), "chain": a.get("chainName", "X Layer"),
            "change24hPercent": a.get("change24h"), "volume24hUsd": a.get("volume24h"),
            "liquidityUsd": a.get("liquidity"),
            "streakDays": streak_of(hist, a.get("symbol", ""), mover_symbols),
            "priceAnomalySuspected": float(a.get("change24h") or 0) <= -50,
            "note": "跌幅远超正股市场正常范围，价格数据待核查" if float(a.get("change24h") or 0) <= -50 else None,
        }
        for a in movers_candidates[:4]
    ]

    premiums = [
        {"symbol": s.get("tokenSymbol") or s.get("stockCode"), "chain": s.get("chainName", "X Layer"),
         "premiumPercent": s.get("premium"), "stockPriceUsd": s.get("stockPrice")}
        for s in u["stockTokens"] if s.get("premium") is not None
    ]
    try:
        premiums.sort(key=lambda x: -abs(float(x["premiumPercent"])))
    except (TypeError, ValueError):
        pass
    premiums = premiums[:4]

    m = u["metrics"]
    return {
        "metrics": {
            "verifiedPools": m.get("verifiedPools"),
            "verifiedAssets": m.get("verifiedAssets"),
            "newRelationRecords24h": m.get("newRelations24h"),
            "newRelationRecordsNote": "含未核验的名称线索记录，不等于已核验关系",
        },
        "distribution": u["distribution"],
        "newNameLeads24h": {"total": 0, "byTicker": []},
        "newVerifiedPairs24h": new_verified_raw,
        "verifiedAssetMovers24h": movers,
        "moverSymbols": mover_symbols,
        "crossMarketPremiumTop": premiums,
    }


def briefing_for(base: dict, lang: str) -> dict:
    def with_tier(x, field):
        out = dict(x)
        out["liquidityTierLabel"] = tier_label(x.get(field), lang)
        return out

    movers = [with_tier(x, "liquidityUsd") for x in base["verifiedAssetMovers24h"]]
    verified = [with_tier(x, "poolLiquidityUsd") for x in base["newVerifiedPairs24h"]]
    out = {**base, "verifiedAssetMovers24h": movers, "newVerifiedPairs24h": verified}
    if lang == "en":
        return {
            **out,
            "verifiedAssetMovers24h": [x for x in movers if ASCII_ONLY.match(str(x.get("symbol", "")))],
            "newVerifiedPairs24h": [x for x in verified if ASCII_ONLY.match(str(x.get("ticker", "")))],
            "crossMarketPremiumTop": [x for x in base["crossMarketPremiumTop"] if ASCII_ONLY.match(str(x.get("symbol", "")))],
        }
    return out


async def refresh_briefing() -> None:
    if not ai_enabled():
        return
    base = await briefing_base()
    for lang in ("zh", "en"):
        await ai_narrate("briefing", lang, 30 * 60_000, briefing_for(base, lang), BRIEFING_TASK, force=True)
    update_streaks(base["moverSymbols"])


async def briefing_endpoint(lang: str) -> dict:
    result = await ai_narrate(
        "briefing", lang, 30 * 60_000,
        briefing_for(await briefing_base(), lang), BRIEFING_TASK,
    )
    if result:
        return {"text": result["text"], "at": result["at"], "cached": result.get("cached", False)}
    return {"text": None, "reason": "upstream_failed" if ai_enabled() else "disabled"}
