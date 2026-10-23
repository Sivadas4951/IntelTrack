"""
report_builder.py
-----------------
Stage 4: sourced report generator. Reports are built from claim IDs and database
rows only (templates fill values from the DB; no free text, no LLM). Every sentence
carries claim IDs plus a citation (source link, observation date, origin), and every
report is run through analytics.report_checker before it is saved.

Two report types
  change      one detected change: what changed, value impact, ranking impact
  comparison  side-by-side facts for a market on chosen criteria

Lanes: "evidence" sentences quote verified claims. "model" sentences are computed
from those claims (metrics, rankings, judgements) and are labelled as such.

Run from the project root:
    python -m analytics.report_builder                    # all recent changes + all markets
    python -m analytics.report_builder --show             # also print the full reports
    python -m analytics.report_builder --change <change_id> --show
    python -m analytics.report_builder --market isp --criteria price,cost_per_mbps --show
    python -m analytics.report_builder --md               # also write data/reports/<id>.md
"""

import argparse
import os
from datetime import datetime, timedelta, timezone

from analytics.claim_store import get_claims_by_ids, get_recent_changes, now_iso, parse_number
from analytics.config_loader import canonical_field, get_market_rules, supported_markets
from analytics.bundle_decomposition import build_sentences as build_bundle_sentences
from analytics.bundle_decomposition import decompose as decompose_bundle
from analytics.fact_checks import build_citations, fmt_num
from analytics.personas import list_personas
from analytics.ranking import compare_competitors, rank_market
from analytics.report_checker import check_report
from analytics.report_store import ensure_tables, save_report
from analytics.value_engine import analyze_change
from analytics.vis_score import build_vis_sentences, vis_for_change
from analytics.value_functions import METRIC_LABELS

MARKET_NAMES = {"isp": "ISP", "dairy": "Dairy", "headphones": "Headphones"}

FIELD_LABELS = {
    "price": "price", "speed_mbps": "speed", "fup_cap_gb": "FUP data cap",
    "post_fup_speed_mbps": "post-FUP speed", "ott_bundle": "OTT bundle", "plan_name": "plan name",
    "pack_size_ml": "pack size", "fat_percentage": "fat content", "product_name": "product name",
    "battery_hours": "battery life", "anc_enabled": "noise cancellation",
    "warranty_months": "warranty", "model_name": "model name",
}

ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
            6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth"}

LANE_NOTE = ("Evidence-lane sentences quote verified claims. Model-lane sentences are computed "
             "from those claims and are labelled as such.")


# ── Small helpers ────────────────────────────────────────────────────────────

def _label(field: str) -> str:
    return FIELD_LABELS.get(field, field.replace("_", " "))


def _name(key: str) -> str:
    return METRIC_LABELS.get(key) or FIELD_LABELS.get(key) or key.replace("_", " ")


def _ordinal(n) -> str:
    return ORDINALS.get(int(n), "a lower place")


def _with_unit(raw, unit) -> str:
    raw = "" if raw is None else str(raw)
    if unit == "INR":
        return f"Rs {raw}"
    if unit == "%":
        return f"{raw}%"
    return f"{raw} {unit}" if unit else raw


def _just_before(ts: str) -> str:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - timedelta(microseconds=1)).isoformat()


class _Draft:
    """Collects sentences. A sentence without a claim ID is never added."""

    def __init__(self):
        self.sentences: list[dict] = []
        self.gaps: list[str] = []

    def add(self, section, lane, text, claim_ids, persona_id=None, is_vis=False) -> bool:
        ids = [i for i in dict.fromkeys(claim_ids) if i]
        if not ids:
            return False
        item = {
            "id": f"s{len(self.sentences) + 1}", "section": section, "lane": lane,
            "text": text, "claim_ids": ids, "citations": build_citations(ids),
        }
        if persona_id:
            item["persona_id"] = persona_id
        if is_vis:
            item["is_vis"] = True
        self.sentences.append(item)
        return True

def _finish(report_id, report_type, market, subject, title, draft: _Draft):
    origins = sorted({c["origin"] for s in draft.sentences for c in s["citations"]})
    disclosure = ""
    if "simulated" in origins:
        disclosure = "Contains simulated demo data (origin = simulated), not live observations."
    elif "archived" in origins:
        disclosure = "Contains archived observations (origin = archived)."
    report = {
        "report_id": report_id, "report_type": report_type, "market": market,
        "subject": subject, "title": title, "created_at": now_iso(),
        "lane_note": LANE_NOTE, "disclosure": disclosure, "origins": origins,
        "gaps": draft.gaps, "sentences": draft.sentences,
    }
    return report, check_report(report)


# ── Change report ────────────────────────────────────────────────────────────

def _phrase(state: str, entry: dict | None, res: dict) -> str:
    if state == "ranked":
        return (f"{_ordinal(entry['rank'])} place "
                f"({res['label']} {fmt_num(entry['value'])} {res['unit']})")
    if state == "ineligible":
        return f"ineligible ({'; '.join(entry['reasons'])})"
    if state == "unrankable":
        return f"not rankable ({'; '.join(entry['reasons'])})"
    return "not present"


def _state_map(res: dict) -> dict:
    out = {}
    for e in res["ranked"]:
        out[(e["competitor"], e["plan_or_sku"])] = ("ranked", e)
    for e in res["ineligible"]:
        out[(e["competitor"], e["plan_or_sku"])] = ("ineligible", e)
    for e in res["unrankable"]:
        out[(e["competitor"], e["plan_or_sku"])] = ("unrankable", e)
    return out


def _add_ranking_sentences(draft: _Draft, market: str, after_ts: str):
    before_ts = _just_before(after_ts)
    views = [(p["id"], p["label"]) for p in list_personas(market)] + [(None, None)]
    for pid, plabel in views:
        try:
            before = rank_market(market, pid, as_of=before_ts)
            after = rank_market(market, pid, as_of=after_ts)
        except Exception:
            continue  # a persona that cannot be ranked must not block the report
        bm, am = _state_map(before), _state_map(after)
        for key in sorted(set(bm) | set(am)):
            bs, be = bm.get(key, ("absent", None))
            as_, ae = am.get(key, ("absent", None))
            if bs == as_ and (be or {}).get("rank") == (ae or {}).get("rank"):
                continue
            ids = list((be or {}).get("evidence_claim_ids", [])) + list((ae or {}).get("evidence_claim_ids", []))
            view = f'the persona "{plabel}"' if pid else "the market-wide view"
            text = (f"For {view}, {key[0]} {key[1]} moved from {_phrase(bs, be, before)} "
                    f"to {_phrase(as_, ae, after)}.")
            draft.add("Ranking impact", "model", text, ids, persona_id=pid)


def build_change_report(change_id: str):
    """Returns (report, checker_result), or None if the change cannot be analysed."""
    analysis = analyze_change(change_id)
    if analysis is None:
        return None
    fl, vl, sl = analysis["fact_layer"], analysis["value_layer"], analysis["strategy_layer"]
    market, comp, plan = fl["market"], fl["competitor"], fl["plan_or_sku"]
    field = fl["field_canonical"]
    draft = _Draft()

    # 1. What changed (evidence lane: values quoted from the two claims)
    if fl["claim_id_old"]:
        text = (f"{comp} changed the {_label(field)} of {plan} from "
                f"{_with_unit(fl['old_value'], fl['unit'])} to {_with_unit(fl['new_value'], fl['unit'])}.")
        ids = [fl["claim_id_old"], fl["claim_id_new"]]
    else:
        text = f"{comp} added a {_label(field)} for {plan}: {_with_unit(fl['new_value'], fl['unit'])}."
        ids = [fl["claim_id_new"]]
    draft.add("What changed", "evidence", text, ids)

    # 2. Price context (evidence lane): the price claims used by the metrics
    pool = set(sl["evidence_claim_ids"])
    for m in vl["metrics"]:
        pool.update(m["inputs_before"] + m["inputs_after"])
    claims = get_claims_by_ids(list(pool))
    price_claims = sorted(
        (c for c in claims.values() if canonical_field(market, c["field"]) == "price"),
        key=lambda c: (c["observed_at"], c["claim_id"]),
    )
    if field != "price":
        if price_claims:
            first, last = price_claims[0], price_claims[-1]
            v1, v2 = parse_number(first["value"]), parse_number(last["value"])
            p1, p2 = _with_unit(first["value"], first["unit"]), _with_unit(last["value"], last["unit"])
            if len(price_claims) == 1:
                text = f"The listed price is {p1}."
            elif v1 == v2:
                text = f"The listed price stayed at {p1} before and after the change."
            else:
                text = f"The listed price moved from {p1} to {p2}."
            draft.add("What changed", "evidence", text, [first["claim_id"], last["claim_id"]])
        else:
            draft.gaps.append("price: not found in the cited claims")

    # 3. Value impact (model lane: metrics recomputed from the cited claims)
    for m in vl["metrics"]:
        if m["before"] is None or m["after"] is None:
            continue
        ids = m["inputs_before"] + m["inputs_after"]
        if m["verdict"] in ("worse", "better"):
            move = "rose" if m["delta"] and m["delta"] > 0 else "fell"
            text = f"{m['label']} {move} from {fmt_num(m['before'])} to {fmt_num(m['after'])} {m['unit']}"
            if m["delta_pct"] is not None:
                text += f" ({m['delta_pct']:+.2f}%)"
            text += f", which is {m['verdict']} for the customer."
        elif m["verdict"] == "unchanged":
            text = f"{m['label']} was unchanged at {fmt_num(m['after'])} {m['unit']}."
        else:
            continue
        draft.add("Value impact", "model", text, ids)

    if vl["is_value_stealth"]:
        basis = ", ".join(_name(b) for b in vl["stealth_basis"])
        text = ("This is a value-stealth change: the listed price did not change, "
                f"but the customer receives less value ({basis}).")
        draft.add("Value impact", "model", text, sl["evidence_claim_ids"])

    for q in ([] if field == "ott_bundle" else vl["qualitative_losses"]):
        if q["lost"] == [q["field"]]:
            text = f"{_label(q['field'])} was switched off ({q['value']})."
        else:
            text = f"{_label(q['field'])}: lost {', '.join(q['lost'])} ({q['value']})."
        draft.add("Value impact", "model", text, [q["claim_id_before"], q["claim_id_after"]])

    # 3b. Bundle value: a cited price, or "unquantified value loss". Never invented.
    if field == "ott_bundle":
        decomp = decompose_bundle(change_id)
        if decomp:
            for s in build_bundle_sentences(decomp)[1:]:  # [0] repeats "What changed"
                draft.add("Bundle value", s["lane"], s["text"], s["claim_ids"])

     # 4. Ranking impact (model lane)
    _add_ranking_sentences(draft, market, fl["new_observed_at"])

    # 5. Value Integrity Score (model lane, Stage 6b)
    vis_result = vis_for_change(change_id)
    if vis_result:
        for s in build_vis_sentences(vis_result):
            draft.add(s["section"], s["lane"], s["text"], s["claim_ids"], is_vis=True)

    title = f"{comp} {plan}: change in {_label(field)}"
    return _finish(f"rpt_change_{change_id}", "change", market, change_id, title, draft)


# ── Comparison report ────────────────────────────────────────────────────────

def _crit_label(crit: str) -> str:
    return METRIC_LABELS.get(crit) or _label(crit)


def build_comparison_report(market: str, criteria: list[str] | None = None,
                            competitors: list[str] | None = None):
    """Returns (report, checker_result). Raises ValueError for unknown criteria."""
    table = compare_competitors(market, criteria=criteria, competitors=competitors)
    rules = get_market_rules(market)
    directions = rules.get("field_direction", {})
    draft = _Draft()

    for row in table["rows"]:
        comp, plan = row["competitor"], row["plan_or_sku"]
        parts, fact_ids, metric_cells = [], [], []
        for crit in table["criteria"]:
            cell = row["cells"][crit]
            if cell["status"] == "not found":
                draft.gaps.append(f"{comp} {plan}: {_crit_label(crit)} not found")
                continue
            if cell["kind"] == "fact":
                part = f"{_label(crit)} {_with_unit(cell['raw'], cell['unit'])}"
                if cell["status"] == "ambiguous":
                    part += f" (ambiguous: {cell.get('ambiguity')})"
                parts.append(part)
                fact_ids.append(cell["claim_id"])
            else:
                metric_cells.append((crit, cell))
        if parts:
            draft.add("Facts as observed", "evidence", f"{comp} {plan}: " + "; ".join(parts) + ".", fact_ids)
        for crit, cell in metric_cells:
            text = f"{comp} {plan}: {_crit_label(crit)} is {fmt_num(cell['value'])} {cell['unit']}"
            if cell["status"] == "ambiguous":
                text += " (based on an ambiguous input)"
            draft.add("Derived cost metrics", "model", text + ".", cell["claim_ids"])

    by_key = {f"{r['competitor']} / {r['plan_or_sku']}": r for r in table["rows"]}
    for crit in table["criteria"]:
        keys = table["best"].get(crit) or []
        if not keys:
            continue
        is_metric = crit in rules.get("metrics", [])
        direction = "lower_is_better" if is_metric else directions.get(crit)
        if direction not in ("lower_is_better", "higher_is_better"):
            continue
        cells = [by_key[k]["cells"][crit] for k in keys]
        value = cells[0].get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if is_metric:
            ids = [i for c in cells for i in c["claim_ids"]]
            value_text = f"{fmt_num(value)} {cells[0]['unit']}"
        else:
            ids = [c["claim_id"] for c in cells]
            value_text = _with_unit(cells[0]["raw"], cells[0]["unit"])
        word = "Lowest" if direction == "lower_is_better" else "Highest"
        text = f"{word} {_crit_label(crit)} in this comparison: {', '.join(keys)} at {value_text}."
        draft.add("Best in comparison", "model", text, ids)

    slug = "_".join(table["criteria"])
    default = list(rules.get("criteria_default", []))
    suffix = "" if (table["criteria"] == default and not competitors) else f"_{slug}"
    if competitors:
        suffix += "_" + "-".join(sorted(c.lower() for c in competitors))
    title = f"{MARKET_NAMES.get(market, market)} comparison"
    return _finish(f"rpt_compare_{market}{suffix}", "comparison", market, market, title, draft)


# ── Rendering ────────────────────────────────────────────────────────────────

def render_text(report: dict, passed: bool) -> str:
    lines = [f"\n{report['title']}  [{report['report_type']} | {report['market']} | "
             f"checker: {'PASSED' if passed else 'REJECTED'}]"]
    if report.get("disclosure"):
        lines.append("NOTE: " + report["disclosure"])
    lines.append(report["lane_note"])
    current = None
    for s in report["sentences"]:
        if s["section"] != current:
            current = s["section"]
            lines.append(f"\n== {current} ==")
        lines.append(f"{s['id']} ({s['lane']} lane) {s['text']}")
        for c in s["citations"]:
            lines.append(f"      cite {c['claim_id']} | {c['source_url']} | "
                         f"observed {c['observed_at']} | {c['origin']}")
    if report["gaps"]:
        lines.append("\nNot found: " + "; ".join(report["gaps"]))
    return "\n".join(lines)


def render_markdown(report: dict) -> str:
    lines = [f"# {report['title']}", "", f"*{report['report_type']} report, market: {report['market']}*", ""]
    if report.get("disclosure"):
        lines += [f"> {report['disclosure']}", ""]
    lines += [report["lane_note"], ""]
    current = None
    for s in report["sentences"]:
        if s["section"] != current:
            current = s["section"]
            lines += [f"## {current}", ""]
        cites = "; ".join(
            f"`{c['claim_id']}` ([source]({c['source_url']}), observed {c['observed_at']}, {c['origin']})"
            for c in s["citations"]
        )
        lines.append(f"- **{s['lane']}**: {s['text']} {cites}")
    if report["gaps"]:
        lines += ["", "**Not found:** " + "; ".join(report["gaps"])]
    return "\n".join(lines) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────────

def _emit(report: dict, result: dict, args):
    status = "PASSED" if result["passed"] else "REJECTED"
    origins = ", ".join(report["origins"]) or "none"
    print(f"[{status}] {report['report_id']}: {len(report['sentences'])} sentences (origins: {origins})")
    if result["passed"]:
        save_report(report, True)
    else:
        for i in result["issues"]:
            print(f"    {i['sentence_id']} {i['code']}: {i['message']}")
    if args.show:
        print(render_text(report, result["passed"]))
    if args.md and result["passed"]:
        os.makedirs(os.path.join("data", "reports"), exist_ok=True)
        path = os.path.join("data", "reports", f"{report['report_id']}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_markdown(report))
        print(f"    wrote {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Person 3 sourced report generator")
    parser.add_argument("--change", help="build the report for one change ID")
    parser.add_argument("--market", help="build a comparison report for one market")
    parser.add_argument("--criteria", help="comma-separated criteria for --market")
    parser.add_argument("--competitors", help="comma-separated competitor names for --market")
    parser.add_argument("--hours", type=int, default=24 * 30, help="look-back window for changes")
    parser.add_argument("--show", action="store_true", help="print the full reports")
    parser.add_argument("--md", action="store_true", help="also write markdown to data/reports/")
    args = parser.parse_args()

    ensure_tables()
    criteria = [c.strip() for c in args.criteria.split(",")] if args.criteria else None
    competitors = [c.strip() for c in args.competitors.split(",")] if args.competitors else None

    if args.change:
        built = build_change_report(args.change)
        if built is None:
            print(f"Change {args.change} not found or its claims are missing.")
        else:
            _emit(*built, args)
    elif args.market:
        _emit(*build_comparison_report(args.market, criteria, competitors), args)
    else:
        for ch in reversed(get_recent_changes(hours=args.hours)):
            built = build_change_report(ch["change_id"])
            if built:
                _emit(*built, args)
        for mkt in supported_markets():
            _emit(*build_comparison_report(mkt), args)