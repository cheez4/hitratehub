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
    # MLB batter props
    "batter_hits": MarketDefinition(
        "batter_hits", "Hits", "MLB", "batter", "h",
        ("hits", "hit")
    ),
    "batter_total_bases": MarketDefinition(
        "batter_total_bases", "Total Bases", "MLB", "batter", "tb",
        ("total bases", "total base", "tb")
    ),
    "batter_home_runs": MarketDefinition(
        "batter_home_runs", "Home Runs", "MLB", "batter", "hr",
        ("home runs", "home run", "hr")
    ),
    "batter_runs": MarketDefinition(
        "batter_runs", "Runs", "MLB", "batter", "runs_scored",
        ("runs", "run")
    ),
    "batter_rbis": MarketDefinition(
        "batter_rbis", "RBI", "MLB", "batter", "rbi",
        ("rbi", "rbis")
    ),
    "batter_hits_runs_rbis": MarketDefinition(
        "batter_hits_runs_rbis", "Hits + Runs + RBI", "MLB", "batter",
        "h+runs_scored+rbi",
        (
            "hits runs rbi", "hits + runs + rbi", "hits+runs+rbi",
            "hits runs rbis", "hits + runs + rbis", "hrr", "h+r+rbi"
        )
    ),
    "batter_singles": MarketDefinition(
        "batter_singles", "Singles", "MLB", "batter", "single",
        ("singles", "single")
    ),
    "batter_doubles": MarketDefinition(
        "batter_doubles", "Doubles", "MLB", "batter", "double",
        ("doubles", "double", "2b")
    ),
    "batter_walks": MarketDefinition(
        "batter_walks", "Walks", "MLB", "batter", "bb",
        ("walks", "walk", "bases on balls", "bb")
    ),
    "batter_stolen_bases": MarketDefinition(
        "batter_stolen_bases", "Stolen Bases", "MLB", "batter", "sb",
        ("stolen bases", "stolen base", "sb")
    ),

    # Provider alternate/ladder markets. These resolve cleanly, but Compare
    # Players still treats Hits / Home Runs as the stat and the threshold as
    # the line (0.5, 1.5, 2.5, etc.).
    "batter_1plus_hits": MarketDefinition(
        "batter_1plus_hits", "1+ Hits", "MLB", "batter", "h",
        ("1+ hits", "1 plus hits")
    ),
    "batter_2plus_hits": MarketDefinition(
        "batter_2plus_hits", "2+ Hits", "MLB", "batter", "h",
        ("2+ hits", "2 plus hits")
    ),
    "batter_3plus_hits": MarketDefinition(
        "batter_3plus_hits", "3+ Hits", "MLB", "batter", "h",
        ("3+ hits", "3 plus hits")
    ),
    "batter_4plus_hits": MarketDefinition(
        "batter_4plus_hits", "4+ Hits", "MLB", "batter", "h",
        ("4+ hits", "4 plus hits")
    ),
    "batter_2plus_home_runs": MarketDefinition(
        "batter_2plus_home_runs", "2+ Home Runs", "MLB", "batter", "hr",
        ("2+ home runs", "2 plus home runs", "2+ hr")
    ),

    # MLB pitcher props
    "pitcher_strikeouts": MarketDefinition(
        "pitcher_strikeouts", "Pitcher Strikeouts", "MLB", "pitcher",
        "strikeouts",
        ("pitcher strikeouts", "strikeouts", "strikeout", "so", "k", "ks")
    ),
    "pitcher_outs": MarketDefinition(
        "pitcher_outs", "Pitcher Outs", "MLB", "pitcher",
        "outs_from_innings",
        ("pitcher outs", "outs", "outs recorded", "recorded outs")
    ),
    "pitcher_earned_runs": MarketDefinition(
        "pitcher_earned_runs", "Pitcher Earned Runs", "MLB", "pitcher",
        "earned_runs",
        (
            "pitcher earned runs", "earned runs", "earned runs allowed",
            "pitcher earned runs allowed"
        )
    ),
    "pitcher_earned_runs_allowed": MarketDefinition(
        "pitcher_earned_runs_allowed", "Pitcher Earned Runs Allowed",
        "MLB", "pitcher", "earned_runs", ()
    ),
    "pitcher_hits_allowed": MarketDefinition(
        "pitcher_hits_allowed", "Pitcher Hits Allowed", "MLB", "pitcher",
        "hits_allowed",
        ("pitcher hits allowed", "hits allowed")
    ),
    "pitcher_walks": MarketDefinition(
        "pitcher_walks", "Pitcher Walks", "MLB", "pitcher",
        "walks_allowed",
        ("pitcher walks", "walks allowed", "pitcher walks allowed", "walks")
    ),
    "pitcher_walks_allowed": MarketDefinition(
        "pitcher_walks_allowed", "Pitcher Walks Allowed", "MLB", "pitcher",
        "walks_allowed", ()
    ),
    "pitcher_runs_allowed": MarketDefinition(
        "pitcher_runs_allowed", "Pitcher Runs Allowed", "MLB", "pitcher",
        "runs_allowed",
        ("pitcher runs allowed", "runs allowed")
    ),

    # WNBA player props. These are registry-ready now; the WNBA gamelog
    # calculator can use them once its stat table is connected.
    "player_points": MarketDefinition(
        "player_points", "Points", "WNBA", "player", "points",
        ("points", "pts")
    ),
    "player_rebounds": MarketDefinition(
        "player_rebounds", "Rebounds", "WNBA", "player", "rebounds",
        ("rebounds", "rebs", "reb")
    ),
    "player_assists": MarketDefinition(
        "player_assists", "Assists", "WNBA", "player", "assists",
        ("assists", "asts", "ast")
    ),
    "player_threes": MarketDefinition(
        "player_threes", "3-Pointers Made", "WNBA", "player", "threes",
        ("threes", "3 pointers", "3-pointers", "three pointers", "3pm")
    ),
    "player_points_rebounds_assists": MarketDefinition(
        "player_points_rebounds_assists", "Points + Rebounds + Assists",
        "WNBA", "player", "points+rebounds+assists",
        ("pra", "points rebounds assists", "points + rebounds + assists")
    ),
    "player_blocks": MarketDefinition(
        "player_blocks", "Blocks", "WNBA", "player", "blocks",
        ("blocks", "blk")
    ),
    "player_steals": MarketDefinition(
        "player_steals", "Steals", "WNBA", "player", "steals",
        ("steals", "stl")
    ),
}

def normalize_market_text(value) -> str:
    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("_", " ")
        .replace("-", " ")
        .split()
    )

_ALIAS_TO_KEY = {}

for market_key, market in MARKETS.items():
    _ALIAS_TO_KEY[normalize_market_text(market_key)] = market_key
    _ALIAS_TO_KEY[normalize_market_text(market.display)] = market_key

    for alias in market.aliases:
        _ALIAS_TO_KEY[normalize_market_text(alias)] = market_key

def resolve_market(
    provider_market_key=None,
    display_prop=None,
) -> Optional[MarketDefinition]:
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

def markets_for(sport: str, entity: str | None = None):
    sport_key = normalize_market_text(sport)

    rows = []

    for market in MARKETS.values():
        if normalize_market_text(market.sport) != sport_key:
            continue

        if entity and normalize_market_text(market.entity) != normalize_market_text(entity):
            continue

        rows.append(market)

    return sorted(rows, key=lambda item: item.display.lower())
