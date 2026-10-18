"""Consulta pública somente leitura; não registra agentes nem envia trabalhos."""

from __future__ import annotations

import datetime as dt
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://superteam.fun"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def search_superteam(*, take=20, now=None, opener=urlopen):
    if type(take) is not int or not 1 <= take <= 50:
        raise ValueError("Consulte entre 1 e 50 oportunidades por rodada")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Use horário com fuso")
    url = BASE + "/api/listings/live?" + urlencode({"take": take, "deadline": now.isoformat()})
    request = Request(url, headers={"User-Agent": "FabricaNegocios/0.1 (public-read-only)", "Accept": "application/json"})
    with opener(request, timeout=20) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Resposta pública excede o limite de tamanho")
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise ValueError("Formato inesperado no feed público Superteam")
    allowed = []
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        # Falhar fechado: permissão precisa estar explícita na resposta atual.
        if row.get("agentAccess") not in ("AGENT_ALLOWED", "AGENT_ONLY") or row.get("status") != "OPEN":
            rejected += 1
            continue
        try:
            deadline = dt.datetime.fromisoformat(row["deadline"].replace("Z", "+00:00"))
            slug = row["slug"]
            if deadline <= now or not isinstance(slug, str) or not slug or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in slug):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            rejected += 1
            continue
        allowed.append({
            "id": row.get("id"), "title": row.get("title"), "url": BASE + "/earn/listing/" + slug,
            "agent_access": row["agentAccess"], "deadline": row["deadline"],
            "type": row.get("type"), "token": row.get("token"), "total_reward": row.get("rewardAmount"),
            "compensation_type": row.get("compensationType"), "pro_required": row.get("isPro"),
            "requires_eligibility_review": True,
            "payment_note": "Recompensa do anúncio não é receita recebida. Pode haver competição, aprovação, cadastro humano, carteira e KYC.",
        })
    return {
        "checked_at": now.isoformat(), "source": url,
        "public_listings_checked": len(rows), "excluded": rejected,
        "agent_opportunities": allowed,
        "scope": "Feed anônimo Global; não inclui todas as vagas AGENT_ONLY. Cadastro de agente é separado.",
        "automatic_submission": False,
        "documentation": "https://superteam.fun/earn/agents",
    }
