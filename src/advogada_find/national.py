"""Auditoria e rechecagem conservadora de contatos dos escritórios já catalogados.

Apenas pesquisa e valida sites oficiais. Nunca envia mensagens ou muda DRY_RUN.
"""
from __future__ import annotations

import argparse
import json

from .app import AdvogadaFind
from .config import Settings
from .crawler import WebsiteCrawler


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
    if after_id < 0:
        raise ValueError('after-id não pode ser negativo')
    if not 1 <= limit <= 500:
        raise ValueError("limit deve ficar entre 1 e 500")
    with app.db.connect() as con:
        rows = [dict(r) for r in con.execute("""
            SELECT c.id,c.name,c.domain,c.website,c.city,c.state
            FROM companies c
            LEFT JOIN curated_queue cq ON cq.company_id=c.id
            WHERE cq.company_id IS NULL AND c.website LIKE 'https://%' AND c.id > ?
              AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.company_id=c.id
                 AND a.status IN ('sent','sending','uncertain'))
              AND (?=1 OR c.verified=0 OR c.active=0)
            ORDER BY c.verified ASC,c.id ASC LIMIT ?
        """, (after_id, int(include_verified), limit))]
    stats = {"selected": len(rows), "next_after_id": rows[-1]["id"] if rows else after_id, "verified": 0, "no_contact": 0, "errors": 0,
             "notices": [], "messages_sent": 0}
    crawler = WebsiteCrawler(app.s)
    try:
        for row in rows:
            try:
                result = crawler.crawl(row["website"], row["domain"], city=row["city"])
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
    stats["ready_for_application"] = len(app.db.list_targets(app.s.max_send_attempts, limit=100000))
    return stats


def main(argv=None) -> None:
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
