# api/main.py: two small edits for 4.3 (no full replacement needed)

1. Add this import next to the other analytics imports:

    from analytics.value_map import build_value_map

2. Add this endpoint below the `/api/leaderboard` function:

    @app.get("/api/value-map")
    def value_map(market: str, region: str | None = None):
        _check_market(market)
        return build_value_map(market, region=region)
