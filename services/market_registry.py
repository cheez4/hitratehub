from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class MarketDefinition:
    key: str
    display: str
    sport: str
    entity: str
    source: str
    aliases: tuple[str, ...] = ()

MARKETS = {
    "batter_hits": MarketDefinition("batter_hits","Hits","MLB","batter","h",("hits","hit")),
    "batter_total_bases": MarketDefinition("batter_total_bases","Total Bases","MLB","batter","tb",("total bases","total base","tb")),
    "batter_home_runs": MarketDefinition("batter_home_runs","Home Runs","MLB","batter","hr",("home runs","home run","hr")),
    "batter_runs": MarketDefinition("batter_runs","Runs","MLB","batter","runs_scored",("runs","run")),
    "batter_rbis": MarketDefinition("batter_rbis","RBI","MLB","batter","rbi",("rbi","rbis")),
    "batter_hits_runs_rbis": MarketDefinition("batter_hits_runs_rbis","Hits + Runs + RBI","MLB","batter","h+runs_scored+rbi",("hits runs rbi","hits + runs + rbi","hits+runs+rbi","hrr")),
    "pitcher_strikeouts": MarketDefinition("pitcher_strikeouts","Pitcher Strikeouts","MLB","pitcher","strikeouts",("pitcher strikeouts","strikeouts","strikeout","so","k","ks")),
    "pitcher_outs": MarketDefinition("pitcher_outs","Pitcher Outs","MLB","pitcher","outs_from_innings",("pitcher outs","outs","outs recorded","recorded outs")),
    "pitcher_earned_runs": MarketDefinition("pitcher_earned_runs","Pitcher Earned Runs","MLB","pitcher","earned_runs",("pitcher earned runs","earned runs","earned runs allowed","pitcher earned runs allowed")),
    "pitcher_earned_runs_allowed": MarketDefinition("pitcher_earned_runs_allowed","Pitcher Earned Runs Allowed","MLB","pitcher","earned_runs",()),
    "pitcher_hits_allowed": MarketDefinition("pitcher_hits_allowed","Pitcher Hits Allowed","MLB","pitcher","hits_allowed",("pitcher hits allowed","hits allowed")),
    "pitcher_walks": MarketDefinition("pitcher_walks","Pitcher Walks","MLB","pitcher","walks_allowed",("pitcher walks","walks allowed","pitcher walks allowed","walks")),
    "pitcher_walks_allowed": MarketDefinition("pitcher_walks_allowed","Pitcher Walks Allowed","MLB","pitcher","walks_allowed",()),
    "pitcher_runs_allowed": MarketDefinition("pitcher_runs_allowed","Pitcher Runs Allowed","MLB","pitcher","runs_allowed",("pitcher runs allowed","runs allowed")),
}

def normalize_market_text(value) -> str:
    return " ".join(str(value or "").strip().lower().replace("_"," ").replace("-"," ").split())

_ALIAS_TO_KEY = {}
for market_key, market in MARKETS.items():
    _ALIAS_TO_KEY[normalize_market_text(market_key)] = market_key
    _ALIAS_TO_KEY[normalize_market_text(market.display)] = market_key
    for alias in market.aliases:
        _ALIAS_TO_KEY[normalize_market_text(alias)] = market_key

def resolve_market(provider_market_key=None, display_prop=None) -> Optional[MarketDefinition]:
    raw_key = str(provider_market_key or "").strip()
    if raw_key in MARKETS:
        return MARKETS[raw_key]
    normalized_key = normalize_market_text(raw_key)
    if normalized_key in _ALIAS_TO_KEY:
        return MARKETS[_ALIAS_TO_KEY[normalized_key]]
    normalized_prop = normalize_market_text(display_prop)
    if normalized_prop in _ALIAS_TO_KEY:
        return MARKETS[_ALIAS_TO_KEY[normalized_prop]]
    return None

def canonical_market_key(provider_market_key=None, display_prop=None):
    market = resolve_market(provider_market_key, display_prop)
    return market.key if market else None
