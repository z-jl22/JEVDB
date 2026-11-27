"""Hybrid SQL + SBF (jev_keys_overlap) + semantic operator query bridge over JEVDB."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stock_research.config import get_settings
from stock_research.storage.ingest.akshare_cn import normalize_cn_symbol
from stock_research.storage.llm_shim import ensure_shim_running
from stock_research.storage.schema import db_session, ensure_baseline_seeded

_VALID_OPS = {"holds", "probability", "choose", "score"}
_VALID_TARGETS = {"news_events", "edgar_filings", "fundamentals", "combined"}


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _build_cache_key(
    *,
    backend: str,
    model: str,
    op: str,
    condition: str,
    payload: dict[str, Any],
    criteria: Any,
    threshold: float | None = None,
    cache_backend: str | None = None,
) -> str:
    eff_cache_backend = str(cache_backend or backend).strip().lower()
    key_parts: list[Any] = [eff_cache_backend, model, op, condition, payload, criteria]
    if op in ("holds", "probability") and threshold is not None:
        key_parts.append(float(threshold))
    blob = json.dumps(
        key_parts,
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _expand_symbol_filter(raw_symbol: Any, market_filter: Any) -> list[str]:
    raw = str(raw_symbol or "").strip()
    if not raw:
        return []
    upper = raw.upper()
    market_norm = str(market_filter or "").strip().upper()
    if not market_norm or market_norm == "CN":
        try:
            code6, _, _ = normalize_cn_symbol(raw)
            return list(dict.fromkeys([code6, upper]))
        except ValueError:
            pass
    return [upper]


def _fetch_candidates(
    con: Any,
    *,
    target: str,
    filters: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_limit = filters.get("limit")
    limit = 50 if raw_limit is None else int(raw_limit)
    if limit < 0:
        raise ValueError(f"filters['limit'] must be non-negative, got {limit}")
    if limit == 0:
        return []
    source_scope = str(filters.get("source_scope") or "all").lower()
    symbol_filter = filters.get("symbol")
    market_filter = filters.get("market")
    sector_filter = filters.get("sector")
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")

    subqueries: list[str] = []
    if target in ("news_events", "combined"):
        subqueries.append(
            """
            SELECT
                event_id AS record_id,
                'news_events' AS record_type,
                COALESCE(symbol, '') AS symbol,
                COALESCE(market, '') AS market,
                COALESCE(sector, '') AS sector,
                CAST(published_at AS VARCHAR) AS event_date,
                COALESCE(title, '') AS title,
                COALESCE(summary, '') AS summary,
                COALESCE(url, '') AS url,
                COALESCE(source, '') AS source,
                COALESCE(keys, CAST([] AS VARCHAR[])) AS keys
            FROM news_events
            """
        )
    if target in ("edgar_filings", "combined"):
        subqueries.append(
            """
            SELECT
                accession_no AS record_id,
                'edgar_filings' AS record_type,
                COALESCE(symbol, '') AS symbol,
                'US' AS market,
                '' AS sector,
                COALESCE(CAST(filing_date AS VARCHAR), '') AS event_date,
                COALESCE(form || ' ' || title, form, '') AS title,
                COALESCE('Form ' || form || ' report_date=' || COALESCE(CAST(report_date AS VARCHAR), ''), '') AS summary,
                COALESCE(url, '') AS url,
                'sec_edgar' AS source,
                list_value('US', COALESCE(symbol, ''), COALESCE(form, ''), 'sec_edgar') AS keys
            FROM edgar_filings
            """
        )
    if target in ("fundamentals", "combined"):
        subqueries.append(
            """
            SELECT
                symbol || ':' || market || ':' || CAST(report_date AS VARCHAR) || ':' || source AS record_id,
                'fundamentals' AS record_type,
                COALESCE(symbol, '') AS symbol,
                COALESCE(market, '') AS market,
                '' AS sector,
                COALESCE(CAST(report_date AS VARCHAR), '') AS event_date,
                symbol || ' fundamentals (' || CAST(report_date AS VARCHAR) || ')' AS title,
                'PE=' || COALESCE(CAST(pe_ttm AS VARCHAR), 'NA')
                    || ', PB=' || COALESCE(CAST(pb AS VARCHAR), 'NA')
                    || ', ROE=' || COALESCE(CAST(roe AS VARCHAR), 'NA')
                    || ', Revenue=' || COALESCE(CAST(revenue AS VARCHAR), 'NA')
                    || ', NetIncome=' || COALESCE(CAST(net_income AS VARCHAR), 'NA')
                    || ', DebtRatio=' || COALESCE(CAST(debt_ratio AS VARCHAR), 'NA') AS summary,
                '' AS url,
                COALESCE(source, '') AS source,
                list_value(COALESCE(market, ''), COALESCE(symbol, ''), 'fundamentals', COALESCE(source, '')) AS keys
            FROM fundamentals
            """
        )

    union_sql = " UNION ALL ".join(subqueries)
    where_clauses: list[str] = []
    params: list[Any] = []

    if source_scope == "static":
        where_clauses.append("c.source = 'static_baseline'")
    elif source_scope == "dynamic":
        where_clauses.append("c.source <> 'static_baseline'")

    if symbol_filter:
        if isinstance(symbol_filter, (list, tuple, set)):
            raw_syms = list(symbol_filter)
        else:
            raw_syms = [symbol_filter]
        syms: list[str] = []
        for s in raw_syms:
            for norm_s in _expand_symbol_filter(s, market_filter):
                if norm_s not in syms:
                    syms.append(norm_s)
        if syms:
            placeholders = ", ".join(["?"] * len(syms))
            where_clauses.append(f"UPPER(c.symbol) IN ({placeholders})")
            params.extend(syms)

    if market_filter:
        where_clauses.append("UPPER(c.market) = ?")
        params.append(str(market_filter).strip().upper())

    if sector_filter:
        sec_str = str(sector_filter).strip()
        where_clauses.append(
            """
            (
                c.sector ILIKE ?
                OR EXISTS (
                    SELECT 1 FROM sector_constituents sc
                    WHERE UPPER(sc.symbol) = UPPER(c.symbol)
                      AND sc.sector_name ILIKE ?
                )
            )
            """
        )
        params.extend([f"%{sec_str}%", f"%{sec_str}%"])

    if date_from:
        where_clauses.append("SUBSTR(c.event_date, 1, 10) >= ?")
        params.append(str(date_from)[:10])

    if date_to:
        where_clauses.append("SUBSTR(c.event_date, 1, 10) <= ?")
        params.append(str(date_to)[:10])

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    if source_scope == "all" and limit >= 4:
        static_where = "WHERE " + " AND ".join([*where_clauses, "c.source = 'static_baseline'"])
        static_sql = f"""
            SELECT c.*
            FROM ({union_sql}) c
            {static_where}
            ORDER BY c.event_date DESC, c.record_id ASC
            LIMIT ?
        """
        static_cur = con.execute(static_sql, [*params, limit])
        cols = [d[0] for d in static_cur.description]
        static_all = [dict(zip(cols, row)) for row in static_cur.fetchall()]

        if static_all:
            reserved_quota = min(len(static_all), max(2, limit // 4))
            dyn_limit = max(0, limit - reserved_quota)
            dyn_rows: list[dict[str, Any]] = []
            if dyn_limit > 0:
                dyn_where = "WHERE " + " AND ".join([*where_clauses, "c.source <> 'static_baseline'"])
                dyn_sql = f"""
                    SELECT c.*
                    FROM ({union_sql}) c
                    {dyn_where}
                    ORDER BY c.event_date DESC, c.record_id ASC
                    LIMIT ?
                """
                dyn_cur = con.execute(dyn_sql, [*params, dyn_limit])
                dyn_rows = [dict(zip(cols, row)) for row in dyn_cur.fetchall()]

            static_keep = max(reserved_quota, limit - len(dyn_rows))
            merged: dict[str, dict[str, Any]] = {}
            for row in dyn_rows + static_all[:static_keep]:
                merged[row["record_id"]] = row
            out = list(merged.values())
            out.sort(key=lambda r: str(r.get("record_id") or ""))
            out.sort(key=lambda r: str(r.get("event_date") or ""), reverse=True)
            return out[:limit]

    sql = f"""
        SELECT c.*
        FROM ({union_sql}) c
        {where_sql}
        ORDER BY c.event_date DESC, c.record_id ASC
        LIMIT ?
    """
    params.append(limit)
    cursor = con.execute(sql, params)
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def hybrid_semantic_query(
    query_spec: dict[str, Any],
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute a hybrid SQL + SBF (jev_keys_overlap) + JEVDB semantic query over static + dynamic tables."""
    settings = get_settings()
    condition = str(query_spec.get("condition") or "").strip()
    if not condition:
        raise ValueError("query_spec['condition'] is required")

    op = str(query_spec.get("op") or "holds").strip().lower()
    if op not in _VALID_OPS:
        raise ValueError(f"Unsupported semantic op: {op!r}")

    target = str(query_spec.get("target") or "news_events").strip().lower()
    if target not in _VALID_TARGETS:
        raise ValueError(f"Unsupported target: {target!r}")

    choices = query_spec.get("choices")
    levels = query_spec.get("levels")
    if op == "choose":
        if not choices or not isinstance(choices, (list, tuple)) or len(choices) < 2:
            raise ValueError("op='choose' requires at least 2 choices")
        criteria_obj: Any = list(choices)
    elif op == "score":
        levels = list(levels or ["very_negative", "negative", "neutral", "positive", "very_positive"])
        if len(levels) < 2 or len(levels) > 10:
            raise ValueError("op='score' requires 2 to 10 ordered levels")
        criteria_obj = levels
    else:
        criteria_obj = None

    backend = str(query_spec.get("backend") or "llm").strip().lower()
    cache_backend = str(query_spec.get("cache_backend") or backend).strip().lower()
    model = str(query_spec.get("model") or settings.deepseek_model).strip()
    threshold = float(query_spec.get("threshold") if query_spec.get("threshold") is not None else 0.5)
    use_cache = bool(query_spec.get("use_cache", True))
    only_matching = bool(query_spec.get("only_matching", False))
    filters = dict(query_spec.get("filters") or {})
    query_keys_raw = query_spec.get("query_keys") or []
    query_keys = [str(k).strip() for k in query_keys_raw if str(k).strip()]

    endpoint = query_spec.get("endpoint")
    if not endpoint and backend == "llm":
        endpoint = ensure_shim_running(settings)

    with db_session(db_path, init=True) as con:
        ensure_baseline_seeded(con)

        candidates = _fetch_candidates(con, target=target, filters=filters)
        if not candidates:
            return {
                "rows": [],
                "stats": {},
                "backend": backend,
                "model": model,
                "endpoint": endpoint,
                "sources": [],
                "cache_hits": 0,
                "llm_evaluated": 0,
            }

        # Check semantic_decisions_cache for already evaluated rows
        cached_results: dict[str, Any] = {}
        row_cache_keys: dict[str, str] = {}
        for row in candidates:
            payload = {
                "symbol": row["symbol"],
                "market": row["market"],
                "sector": row["sector"],
                "title": row["title"],
                "summary": row["summary"],
            }
            ck = _build_cache_key(
                backend=backend,
                model=model,
                op=op,
                condition=condition,
                payload=payload,
                criteria=criteria_obj,
                threshold=threshold,
                cache_backend=cache_backend,
            )
            row_cache_keys[row["record_id"]] = ck

        if use_cache:
            keys_list = list(row_cache_keys.values())
            placeholders = ", ".join(["?"] * len(keys_list))
            cached_rows = con.execute(
                f"SELECT cache_key, result_json FROM semantic_decisions_cache WHERE cache_key IN ({placeholders})",
                keys_list,
            ).fetchall()
            key_to_result = {ck: json.loads(rj) for ck, rj in cached_rows}
            for row in candidates:
                ck = row_cache_keys[row["record_id"]]
                if ck in key_to_result:
                    cached_results[row["record_id"]] = key_to_result[ck]

        # If query_keys are provided, pre-check SBF overlap OR evaluate misses via JEVDB SQL with SBF
        misses = [row for row in candidates if row["record_id"] not in cached_results]
        stats_dict: dict[str, Any] = {}
        evaluated_map: dict[str, Any] = {}

        if misses:
            api_key = query_spec.get("api_key") or settings.deepseek_api_key or "local"
            if backend == "llm":
                con.execute(
                    f"CREATE OR REPLACE SECRET llm (TYPE openai, PROVIDER config, API_KEY {_sql_quote(api_key)})"
                )
            else:
                con.execute(
                    f"CREATE OR REPLACE SECRET jev (TYPE jev, PROVIDER config, API_KEY {_sql_quote(api_key)})"
                )
            con.execute("SET jevdb_backend=?", [backend])
            if endpoint:
                con.execute("SET jevdb_endpoint=?", [str(endpoint)])
            con.execute("SET jevdb_model=?", [model])
            con.execute("SET jevdb_k=?", [int(query_spec.get("k") or settings.llm_batch_k)])
            con.execute("SET jevdb_threads=?", [int(query_spec.get("threads") or settings.llm_threads)])
            con.execute("SET jevdb_threshold=?", [threshold])
            con.execute("SET jevdb_timeout=?", [int(settings.llm_timeout)])

            con.execute(
                """
                CREATE OR REPLACE TEMP TABLE _jev_cand_docs (
                    record_id VARCHAR,
                    record_type VARCHAR,
                    symbol VARCHAR,
                    market VARCHAR,
                    sector VARCHAR,
                    event_date VARCHAR,
                    title VARCHAR,
                    summary VARCHAR,
                    url VARCHAR,
                    source VARCHAR,
                    keys VARCHAR[]
                )
                """
            )
            con.executemany(
                "INSERT INTO _jev_cand_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r["record_id"],
                        r["record_type"],
                        r["symbol"],
                        r["market"],
                        r["sector"],
                        r["event_date"],
                        r["title"],
                        r["summary"],
                        r["url"],
                        r["source"],
                        list(r.get("keys") or []),
                    )
                    for r in misses
                ],
            )

            cond_sql = _sql_quote(condition)
            if query_keys:
                con.execute(
                    "CREATE OR REPLACE TEMP TABLE _jev_query_topic (topic VARCHAR, keys VARCHAR[])"
                )
                con.execute(
                    "INSERT INTO _jev_query_topic VALUES (?, ?)",
                    [condition, query_keys],
                )
                struct_sql = "{'topic': q.topic, 'symbol': d.symbol, 'market': d.market, 'sector': d.sector, 'title': d.title, 'summary': d.summary}"
            else:
                struct_sql = "{'symbol': d.symbol, 'market': d.market, 'sector': d.sector, 'title': d.title, 'summary': d.summary}"

            if op == "holds":
                sem_expr = f"jev_holds({cond_sql}, {struct_sql})"
            elif op == "probability":
                sem_expr = f"jev_probability({cond_sql}, {struct_sql})"
            elif op == "choose":
                choices_sql = "[" + ", ".join(_sql_quote(str(c)) for c in criteria_obj) + "]"
                sem_expr = f"jev_choose({cond_sql}, {struct_sql}, {choices_sql})"
            else:
                levels_sql = "[" + ", ".join(_sql_quote(str(lv)) for lv in criteria_obj) + "]"
                sem_expr = f"jev_score({cond_sql}, {struct_sql}, {levels_sql})"

            if query_keys and op == "holds":
                # Keep SBFOptimizer active: jev_keys_overlap + jev_holds in INNER JOIN ON
                sql_eval = f"""
                    SELECT d.record_id, TRUE AS semantic_value
                    FROM _jev_cand_docs d
                    JOIN _jev_query_topic q
                      ON jev_keys_overlap(d.keys, q.keys)
                     AND {sem_expr}
                """
                # Track which rows passed SBF key overlap so non-matching rows get False
                sbf_passed_ids = {
                    r[0]
                    for r in con.execute(
                        """
                        SELECT d.record_id
                        FROM _jev_cand_docs d
                        JOIN _jev_query_topic q ON jev_keys_overlap(d.keys, q.keys)
                        """
                    ).fetchall()
                }
                matched_rows = con.execute(sql_eval).fetchall()
                matched_ids = {r[0] for r in matched_rows}
                for r in misses:
                    rid = r["record_id"]
                    if rid in matched_ids:
                        evaluated_map[rid] = True
                    elif rid in sbf_passed_ids:
                        evaluated_map[rid] = False
                    else:
                        evaluated_map[rid] = None  # pruned by SBF before LLM call
            elif query_keys:
                sql_eval = f"""
                    SELECT d.record_id, {sem_expr} AS semantic_value
                    FROM _jev_cand_docs d
                    JOIN _jev_query_topic q
                      ON jev_keys_overlap(d.keys, q.keys)
                """
                for rid, val in con.execute(sql_eval).fetchall():
                    evaluated_map[rid] = val
            else:
                sql_eval = f"""
                    SELECT d.record_id, {sem_expr} AS semantic_value
                    FROM _jev_cand_docs d
                """
                for rid, val in con.execute(sql_eval).fetchall():
                    evaluated_map[rid] = val

            stats_cur = con.execute("SELECT * FROM jevdb_stats()")
            stats_cols = [d[0] for d in stats_cur.description]
            stats_row = stats_cur.fetchone()
            if stats_row is not None:
                stats_dict = dict(zip(stats_cols, stats_row))

            # Persist newly evaluated (non-SBF-pruned) decisions to semantic_decisions_cache
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            cache_inserts = []
            for r in misses:
                rid = r["record_id"]
                if rid not in evaluated_map or evaluated_map[rid] is None:
                    continue
                payload = {
                    "symbol": r["symbol"],
                    "market": r["market"],
                    "sector": r["sector"],
                    "title": r["title"],
                    "summary": r["summary"],
                }
                cache_inserts.append(
                    (
                        row_cache_keys[rid],
                        cache_backend,
                        model,
                        op,
                        condition,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(criteria_obj, ensure_ascii=False)
                        if criteria_obj is not None
                        else "",
                        json.dumps(evaluated_map[rid], ensure_ascii=False),
                        now,
                    )
                )
            if cache_inserts:
                con.executemany(
                    """
                    INSERT INTO semantic_decisions_cache (
                        cache_key, backend, model, op, condition, input_json,
                        criteria_json, result_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        result_json = excluded.result_json,
                        created_at = excluded.created_at
                    """,
                    cache_inserts,
                )

        # Assemble final rows preserving candidate order
        result_rows: list[dict[str, Any]] = []
        cache_hits = 0
        llm_evaluated = 0
        sbf_pruned = 0

        for row in candidates:
            rid = row["record_id"]
            if rid in cached_results:
                val = cached_results[rid]
                # If query_keys are provided, verify key overlap even on cached rows
                if query_keys:
                    row_keys_lower = {str(k).lower() for k in (row.get("keys") or [])}
                    q_keys_lower = {k.lower() for k in query_keys}
                    if not (row_keys_lower & q_keys_lower):
                        sbf_pruned += 1
                        continue
                cache_hits += 1
                from_cache = True
            else:
                val = evaluated_map.get(rid)
                if val is None:
                    sbf_pruned += 1
                    continue
                llm_evaluated += 1
                from_cache = False

            if only_matching:
                if op == "holds" and not bool(val):
                    continue
                if op == "probability" and float(val) < threshold:
                    continue

            item = dict(row)
            item["semantic_op"] = op
            item["semantic_result"] = val
            item["from_cache"] = from_cache
            if op == "score" and isinstance(val, (int, float)) and criteria_obj:
                idx = int(round(val))
                if 0 <= idx < len(criteria_obj):
                    item["semantic_label"] = criteria_obj[idx]
            result_rows.append(item)

        return {
            "rows": result_rows,
            "stats": stats_dict,
            "backend": backend,
            "model": model,
            "endpoint": endpoint,
            "sources": sorted({r["source"] for r in result_rows if r.get("source")}),
            "cache_hits": cache_hits,
            "llm_evaluated": llm_evaluated,
            "sbf_pruned": sbf_pruned,
        }
