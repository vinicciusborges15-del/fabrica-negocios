from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .core import Factory, load_json
from .runner import doctor
from .marketplace import search_superteam


def brl(value):
    try:
        result = Decimal(value.replace(",", "."))
        if not result.is_finite() or result <= 0 or result * 100 != (result * 100).to_integral_value():
            raise ValueError()
        return int(result * 100)
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("Informe reais positivos, com no máximo dois decimais") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fábrica de Negócios: Codex, experimentos pequenos e resultados verificáveis")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("FABRICA_HOME", str(Path(__file__).resolve().parents[1] / "data"))))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Iniciar com limite mensal de R$ 300")
    commands.add_parser("doctor", help="Verificar Codex e login sem revelar credenciais")
    commands.add_parser("status")
    new = commands.add_parser("new", help="Adicionar hipótese à fila")
    new.add_argument("specification", type=Path)
    tick = commands.add_parser("tick", help="Executar uma rodada finita")
    tick.add_argument("--dry-run", action="store_true")
    tick.add_argument("--max-jobs", type=int, default=1)
    run = commands.add_parser("run", help="Repetir rodadas com limites diários; Ctrl+C encerra")
    run.add_argument("--iterations", type=int, default=1)
    run.add_argument("--interval", type=int, default=60)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("business_id")
    reconcile = commands.add_parser("reconcile-costs", help="Registrar conferência humana de todos os custos")
    reconcile.add_argument("business_id")
    reconcile.add_argument("--note", required=True)
    expense = commands.add_parser("expense", help="Registrar despesa efetiva em reais")
    expense.add_argument("business_id")
    expense.add_argument("amount", type=brl)
    expense.add_argument("description")
    resume = commands.add_parser("resume")
    resume.add_argument("business_id")
    resume.add_argument("--stage", choices=["research", "build", "validating"], required=True)
    stripe = commands.add_parser("stripe-event", help="Importar webhook real com assinatura Stripe")
    stripe.add_argument("payload", type=Path)
    stripe.add_argument("--signature-file", type=Path, required=True)
    commands.add_parser("opportunities", help="Consultar catálogo de plataformas remuneradas")
    work = commands.add_parser("work-search", help="Buscar trabalhos que permitem agentes no feed público Superteam")
    work.add_argument("--take", type=int, default=20)
    args = parser.parse_args(argv)
    factory = Factory(args.home)
    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "init":
            result = factory.init()
        elif args.command == "new":
            result = factory.add_experiment(load_json(args.specification))
        elif args.command == "status":
            result = factory.status()
        elif args.command == "tick":
            result = factory.tick(dry_run=args.dry_run, max_jobs=args.max_jobs)
        elif args.command == "run":
            if not 1 <= args.iterations <= 1440 or args.interval < 30:
                raise ValueError("Use 1 a 1440 rodadas e intervalo de pelo menos 30 segundos")
            for index in range(args.iterations):
                print(json.dumps(factory.tick(), ensure_ascii=False), flush=True)
                if index + 1 < args.iterations:
                    time.sleep(args.interval)
            return 0
        elif args.command == "evaluate":
            result = factory.evaluate(args.business_id)
        elif args.command == "reconcile-costs":
            result = factory.reconcile(args.business_id, args.note)
        elif args.command == "expense":
            result = factory.expense(args.business_id, args.amount, args.description)
        elif args.command == "resume":
            result = factory.resume(args.business_id, args.stage)
        elif args.command == "stripe-event":
            secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
            if not secret:
                raise ValueError("STRIPE_WEBHOOK_SECRET não configurado; nenhum pagamento foi importado")
            result = factory.import_stripe(args.payload.read_bytes(), args.signature_file.read_text().strip(), secret)
        elif args.command == "opportunities":
            result = load_json(Path(__file__).parent / "opportunities.json")
        elif args.command == "work-search":
            result = search_superteam(take=args.take)
            path = factory.home / "opportunities"
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            (path / "superteam-latest.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if args.command == "tick" and any(not job.get("ok", True) for job in result["jobs"]):
            return 1
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Rodada interrompida. A próxima execução pausará o trabalho incerto para inspeção.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
