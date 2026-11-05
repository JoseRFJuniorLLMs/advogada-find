"""Auditoria e rechecagem conservadora de contatos dos escritórios já catalogados.

Apenas pesquisa e valida sites oficiais. Nunca envia mensagens ou muda DRY_RUN.
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from typing import TYPE_CHECKING

from .config import Settings
from .crawler import WebsiteCrawler
from .db import iso, utcnow

if TYPE_CHECKING:
    from .app import AdvogadaFind

_CURSOR = 'national_recheck_after_id'
_PRIORITY = 'national_recheck_after_priority'
_NEXT_PASS = 'national_recheck_next_pass_at'


def audit(app: AdvogadaFind) -> dict:
    with app.db.connect() as con:
        summary = dict(con.execute("""SELECT COUNT(*) catalogued,
            SUM(CASE WHEN verified=1 AND active=1 THEN 1 ELSE 0 END) verified,
            SUM(CASE WHEN verified=0 OR active=0 THEN 1 ELSE 0 END) unverified,
            SUM(CASE WHEN verification_note LIKE '%cidade_nao_identificada%' THEN 1 ELSE 0 END) missing_city,
            SUM(CASE WHEN verification_note LIKE '%canal_corporativo_inelegivel%' THEN 1 ELSE 0 END) missing_channel,
            SUM(CASE WHEN verification_note LIKE '%atividade_juridica_nao_identificada%' THEN 1 ELSE 0 END) missing_legal_evidence
            FROM companies""").fetchone())
        summary.update(dict(con.execute("""SELECT
            COUNT(*) contact_records,
            SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) active_contacts
            FROM contacts""").fetchone()))
        summary["ready_for_application"] = len(app.db.list_targets(app.s.max_send_attempts, limit=100000))
        summary["sent"] = app.db.stats()["sent"]
    return {k: (v or 0) for k, v in summary.items()}


def recheck(app: AdvogadaFind, limit: int, include_verified: bool = False, after_id: int = 0) -> dict:
    stats, _ = _recheck_batch(app, limit, include_verified, after_id)
    stats["ready_for_application"] = len(app.db.list_targets(app.s.max_send_attempts, limit=100000))
    return stats


def _recheck_batch(app: AdvogadaFind, limit: int, include_verified: bool, after_id: int,
                   after_priority: int | None = None) -> tuple[dict, int | None]:
    if after_id < 0:
        raise ValueError('after-id não pode ser negativo')
    if not 1 <= limit <= 500:
        raise ValueError("limit deve ficar entre 1 e 500")
    with app.db.connect() as con:
        rows = [dict(r) for r in con.execute("""
            WITH candidates AS (
                SELECT c.id,c.domain,c.website,c.city,c.cnpj_basic,
                    CASE WHEN :prioritize
                         AND (c.verification_note LIKE '%canal_corporativo_inelegivel%'
                              OR c.verification_note LIKE '%cidade_nao_identificada%')
                         AND c.verification_note NOT LIKE '%atividade_juridica_nao_identificada%'
                         AND c.verification_note NOT LIKE '%diretorio_ou_noticias%'
                         AND c.verification_note NOT LIKE '%identidade_site_ausente%'
                         AND c.verification_note NOT LIKE '%cnpj_nao_confirmado_no_site%'
                         THEN 0 ELSE 1 END AS priority
                FROM companies c
                LEFT JOIN curated_queue cq ON cq.company_id=c.id
                WHERE cq.company_id IS NULL AND c.website LIKE 'https://%'
                  AND NOT EXISTS (SELECT 1 FROM registry_exclusions e WHERE e.cnpj_basic=c.cnpj_basic)
                  AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.company_id=c.id
                     AND a.status IN ('sent','sending','uncertain'))
                  AND (:include_verified OR c.verified=0 OR c.active=0)
            )
            SELECT * FROM candidates
            WHERE (priority,id) > (:after_priority,:after_id)
            ORDER BY priority,id LIMIT :limit
        """, {"prioritize": after_priority is not None,
              "after_priority": 1 if after_priority is None else after_priority,
              "after_id": after_id, "include_verified": include_verified, "limit": limit})]
    stats = {"selected": len(rows), "next_after_id": rows[-1]["id"] if rows else after_id, "verified": 0, "no_contact": 0, "errors": 0,
             "notices": [], "messages_sent": 0}
    if not rows:
        return stats, after_priority
    crawler = WebsiteCrawler(app.s)
    try:
        for row in rows:
            try:
                result = crawler.crawl(row["website"], row["domain"], city=row["city"],
                                       registry_cnpj=row["cnpj_basic"] or "")
                app.db.replace_contacts(row["id"], result.contacts, result.careers_signal,
                                        eligible=result.eligible,
                                        evidence=result.evidence + "; motivo=" + result.rejection_reason,
                                        cnpj_basic=result.cnpj_basic)
                if result.eligible:
                    stats["verified"] += 1
                else:
                    stats["no_contact"] += 1
            except Exception as exc:
                stats["errors"] += 1
                if len(stats["notices"]) < 20:
                    stats["notices"].append({"domain": row["domain"], "error": type(exc).__name__})
    finally:
        crawler.close()
    return stats, rows[-1]["priority"]


def scheduled_recheck(app: AdvogadaFind) -> dict | None:
    """Recheck one batch from the discovery worker and retain progress across restarts."""
    limit = app.s.national_recheck_batch_size
    if not limit:
        return None
    with app.db.connect() as con:
        state = dict(con.execute("SELECT key,value FROM runtime WHERE key IN (?,?,?)",
                                 (_CURSOR, _PRIORITY, _NEXT_PASS)).fetchall())
    if state.get(_NEXT_PASS, "") > iso():
        return None
    stats, after_priority = _recheck_batch(app, limit, False, int(state.get(_CURSOR, 0)),
                                          int(state.get(_PRIORITY, 0)))
    after_id = stats["next_after_id"]
    next_pass_at = ""
    if stats["selected"] < limit:
        after_id = after_priority = 0
        next_pass_at = iso(utcnow() + timedelta(days=app.s.city_rescan_days))
    with app.db.connect() as con:
        con.executemany("""INSERT INTO runtime(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            [(_CURSOR, str(after_id)), (_PRIORITY, str(after_priority)),
             (_NEXT_PASS, next_pass_at)])
    return stats


def main(argv=None) -> None:
    from .app import AdvogadaFind

    parser = argparse.ArgumentParser(description="Auditoria e rechecagem nacional sem envio")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("audit")
    scan = sub.add_parser("recheck")
    scan.add_argument("--limit", type=int, default=100)
    scan.add_argument("--after-id", type=int, default=0)
    scan.add_argument("--include-verified", action="store_true")
    args = parser.parse_args(argv)
    app = AdvogadaFind(Settings())
    result = audit(app) if args.command == "audit" else recheck(app, args.limit, args.include_verified, args.after_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
