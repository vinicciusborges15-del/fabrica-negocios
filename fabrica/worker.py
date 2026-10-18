"""Monitor em nuvem: só executa Codex quando iniciado com --armed."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import signal
import tempfile
import threading

from .core import Factory
from .marketplace import search_superteam
from .runner import doctor


def private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_path = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_path, path)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def cycle(factory: Factory, *, armed=False, search=search_superteam, diagnose=doctor):
    result = {
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "armed": bool(armed),
        "marketplace": None,
        "work": {"jobs": [], "executed": False},
        "errors": [],
    }
    try:
        opportunities = search(take=20)
        private_json(factory.home / "opportunities" / "superteam-latest.json", opportunities)
        result["marketplace"] = {
            "public_listings_checked": opportunities["public_listings_checked"],
            "agent_opportunities": len(opportunities["agent_opportunities"]),
            "source": opportunities["source"],
            "automatic_submission": False,
        }
    except Exception as error:
        # Uma indisponibilidade de rede não é uma consulta com zero vagas.
        result["errors"].append({"component": "marketplace", "error_type": type(error).__name__, "message": "Consulta indisponível; resultado atual desconhecido."})
    if armed:
        try:
            auth = diagnose()
            if auth.get("auth_method") != "chatgpt" or auth.get("authenticated") is not True:
                result["errors"].append({"component": "codex", "message": "Entre no Codex com ChatGPT no servidor. O monitor não alterna para API paga."})
            else:
                work = factory.tick()
                result["work"] = {
                    "executed": bool(work.get("jobs")),
                    "jobs": [{key: job.get(key) for key in ("id", "stage", "status", "ok")} for job in work.get("jobs", [])],
                    "blocked": work.get("blocked"),
                }
                if any(job.get("ok") is False for job in work.get("jobs", [])):
                    result["errors"].append({"component": "codex", "message": "Trabalho pausado após falha; consulte os logs privados antes de retomar."})
        except Exception as error:
            result["errors"].append({"component": "controller", "error_type": type(error).__name__, "message": "Rodada falhou; estado preservado para inspeção."})
    status = factory.status()
    result["accounting"] = {
        "monthly_recorded_expense_cents": status["monthly_recorded_expense_cents"],
        "monthly_budget_cents": status["config"]["monthly_budget_cents"],
        "businesses": len(status["businesses"]),
        "currency": "BRL",
    }
    private_json(factory.home / "state" / "worker-status.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path("/data"))
    parser.add_argument("--interval", type=int, default=3600, help="Intervalo entre rodadas, mínimo 300 segundos")
    parser.add_argument("--armed", action="store_true", help="Permitir rodadas locais do Codex usando ChatGPT")
    parser.add_argument("--once", action="store_true", help="Executar só uma rodada e sair")
    args = parser.parse_args(argv)
    if args.interval < 300:
        parser.error("Use intervalo de pelo menos 300 segundos")
    factory = Factory(args.home)
    factory.init()
    stop = threading.Event()
    previous_handlers = {}
    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        while not stop.is_set():
            result = cycle(factory, armed=args.armed)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return 1 if result["errors"] else 0
            stop.wait(args.interval)
        return 0
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
