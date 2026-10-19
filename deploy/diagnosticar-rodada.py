#!/usr/bin/env python3
"""Diagnóstico somente leitura; imprime categorias fixas, nunca logs brutos."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from urllib.parse import quote


HOME_DIR = Path("/var/lib/fabrica")
MAX_LOG_BYTES = 262144
RUNTIME_HELPERS = (Path("/opt/fabrica-runtime/bin/codex-code-mode-host"), Path("/opt/fabrica-runtime/bin/codex-resources/codex-code-mode-host"))
RULES = {
    "autenticacao_remota": r"unauthorized|unauthenticated|invalid_api_key|token.{0,50}(expired|refresh)|please (log|sign) in|autenticação",
    "limite_de_uso": r"usage.limit|quota|rate.limit|too many requests|limite.*uso",
    "sandbox_permissao": r"bubblewrap|bwrap|apparmor|landlock|permission denied|operation not permitted|user namespace|failed to.*sandbox",
    "configuracao": r"error loading configuration|failed to load config|toml|permission profile|unknown field|invalid config",
    "argumentos_cli": r"unexpected argument|unrecognized option|unknown option|invalid value|usage: codex",
    "schema_resposta": r"invalid.schema|json.schema|output.schema|additionalproperties|response.format|esquema solicitado|json exigido",
    "modelo_acesso": r"model.{0,100}(not found|not supported|not available|does not exist)|not have access|access denied|forbidden",
    "rede_tls": r"connection (refused|reset)|network.*unreachable|certificate|tls|dns|timed? out|connection.*closed",
    "disco_cheio": r"no space left|disk quota|read.only file system",
    "ferramenta_execucao": r"tool unavailable|cannot execute|createprocess|no such file or directory|failed to (spawn|start).*process|execve|não foi encontrado",
    "gravacao_artefato": r"failed to write last message|failed to write.*file|entregáveis não encontrados",
}
SAFE_CODES = {"invalid_request_error", "invalid_json_schema", "invalid_api_key", "insufficient_quota", "rate_limit_exceeded", "model_not_found", "usage_limit_reached"}
SAFE_OPTIONS = {"--sandbox", "--output-schema", "--json", "--ephemeral", "--skip-git-repo-check", "sandbox_workspace_write.network_access", "workspace-write"}
SAFE_FRAGMENTS = {
    "unexpected argument", "invalid value", "unknown field", "permission denied",
    "operation not permitted", "failed to initialize", "failed to create thread",
    "failed to write last message", "stream disconnected before completion",
    "not a trusted directory", "no such file or directory", "connection refused",
    "refresh token has already been used", "invalid schema", "response_format",
    "no space left on device", "the following required arguments were not provided",
    "tool unavailable", "cannot execute", "createprocess", "failed to spawn",
    "failed to start", "failed to create process", "execve",
}
SAFE_PATHS = {"PLAN.md", "OWNER_ACTIONS.md", "last-message.txt", "/bin/bash", "/usr/bin/bash", "/bin/sh", "/bin/zsh", "/usr/bin/zsh", "/usr/bin/bwrap", "/usr/bin/rg", "/usr/bin/python3", "apply_patch", "codex-linux-sandbox", "codex-exec-server", "codex-code-mode-host", "node", "bash", "sh", "python3"}


def regular_path(path: Path) -> Path:
    relative = path.relative_to(HOME_DIR)
    current = HOME_DIR
    for piece in (None, *relative.parts):
        if piece is not None:
            current = current / piece
        if current.is_symlink():
            raise ValueError("Caminho com link simbólico; coleta bloqueada.")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Arquivo ausente, especial ou com múltiplos links; coleta bloqueada.")
    return path


def read_log(path: Path) -> tuple[str, int]:
    regular_path(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size > MAX_LOG_BYTES:
            stream.seek(-MAX_LOG_BYTES, os.SEEK_END)
        return stream.read(MAX_LOG_BYTES).decode("utf-8", errors="replace"), size


def classify(text: str) -> dict:
    lowered = text.lower()
    return {
        "categories": [name for name, pattern in RULES.items() if re.search(pattern, lowered)],
        "http_statuses": [code for code in ("400", "401", "402", "403", "404", "408", "409", "422", "429", "500", "502", "503", "504") if re.search(r"\b" + code + r"\b", text)],
        "known_error_codes": sorted(code for code in SAFE_CODES if code in lowered),
        "known_options_mentioned": sorted(option for option in SAFE_OPTIONS if option in text),
        "whitelisted_error_fragments": sorted(fragment for fragment in SAFE_FRAGMENTS if fragment in lowered),
        "known_paths_mentioned": sorted(path for path in SAFE_PATHS if re.search((r"(?<![\w/.-])" if path.startswith("/") else r"(?<![\w.-])") + re.escape(path) + r"(?![\w/.-])", text)),
        "error_fingerprint_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "inspected_error_lines": len([line for line in text.splitlines() if line.strip()]),
    }


def inspect_run(row) -> dict:
    def timestamp(value):
        try:
            return datetime.fromisoformat(value).isoformat() if isinstance(value, str) else None
        except (ValueError, TypeError):
            return None
    report = {
        "id": row["id"] if type(row["id"]) is int else None,
        "business_id": row["business_id"], "stage": row["stage"],
        "ok": row["ok"] if type(row["ok"]) is int and row["ok"] in (0, 1) else None,
        "started_at": timestamp(row["started_at"]), "finished_at": timestamp(row["finished_at"]),
    }
    if not re.fullmatch(r"[0-9a-f]{12}", str(row["business_id"])):
        raise ValueError("Identificador de negócio inválido; coleta bloqueada.")
    if row["stage"] not in {"research", "build", "operating"}:
        report["stage"] = "unknown"
    text = ""
    report.update(runner_ok=None, payload_outcome="unknown")
    try:
        result = json.loads(row["result"] or "{}")
        if isinstance(result, dict):
            report["runner_ok"] = result.get("ok") if type(result.get("ok")) is bool else None
            if isinstance(result.get("error"), str):
                text += result["error"] + "\n"
            payload = result.get("result")
            if isinstance(payload, dict):
                outcome = payload.get("outcome")
                report["payload_outcome"] = outcome if isinstance(outcome, str) and outcome in {"complete", "blocked"} else "unknown"
                payload_text = payload.get("summary") if isinstance(payload.get("summary"), str) else ""
                blockers = payload.get("blockers")
                if isinstance(blockers, list):
                    payload_text += "\n" + "\n".join(item for item in blockers if isinstance(item, str))
                report["payload_categories"] = classify(payload_text)["categories"]
                text += "\n" + payload_text
    except (ValueError, TypeError):
        report["result_json_valid"] = False
    raw = row["run_dir"]
    if not isinstance(raw, str):
        report.update(classify(text))
        report["logs_available"] = False
        return report
    directory = Path(raw)
    expected_parent = HOME_DIR / "workspaces" / row["business_id"] / ".runs"
    if directory.parent != expected_parent or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        raise ValueError("Diretório da execução fora do formato esperado; coleta bloqueada.")
    stderr, report["stderr_bytes"] = read_log(directory / "stderr.txt")
    stdout, report["stdout_bytes"] = read_log(directory / "stdout.jsonl")
    text += stderr
    counts = Counter()
    item_counts = Counter()
    commands = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if not isinstance(kind, str):
            continue
        if kind in {"thread.started", "turn.started", "turn.failed", "turn.completed", "error", "item.completed"}:
            counts[kind] += 1
        if kind in {"turn.failed", "error"}:
            error = event.get("error", event.get("message", ""))
            if isinstance(error, dict):
                error = error.get("message", "")
            if isinstance(error, str):
                text += "\n" + error
        item = event.get("item")
        if kind == "item.completed" and isinstance(item, dict):
            item_type = item.get("type")
            if isinstance(item_type, str) and item_type in {"command_execution", "agent_message", "reasoning", "file_change", "mcp_tool_call", "todo_list", "error"}:
                item_counts[item_type] += 1
                if item_type == "error" and isinstance(item.get("message"), str):
                    text += "\n" + item["message"]
                if item_type == "command_execution":
                    output = item.get("aggregated_output")
                    output = output if isinstance(output, str) else ""
                    text += "\n" + output
                    if len(commands) < 20:
                        code = item.get("exit_code")
                        command = item.get("command")
                        tools = classify(command)["known_paths_mentioned"] if isinstance(command, str) else []
                        commands.append({"exit_code": code if type(code) is int else None, "known_tools_in_command": tools, **classify(output)})
    report.update(classify(text))
    report["event_counts"] = dict(counts)
    report["item_counts"] = dict(item_counts)
    report["command_diagnostics"] = commands
    report["required_file_flags"] = {}
    for name in ("PLAN.md", "OWNER_ACTIONS.md"):
        try:
            info = (expected_parent.parent / name).lstat()
            regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            report["required_file_flags"][name] = {"regular": regular, "bytes": info.st_size if regular else None}
        except FileNotFoundError:
            report["required_file_flags"][name] = {"regular": False, "bytes": None}
    report["logs_available"] = True
    report["raw_messages_printed"] = False
    return report


def collect() -> dict:
    import pwd
    if os.geteuid() not in {0, pwd.getpwnam("fabrica").pw_uid}:
        raise ValueError("Execute somente como root ou fabrica.")
    database = regular_path(HOME_DIR / "state" / "fabrica.sqlite3")
    for suffix in ("-wal", "-journal"):
        journal = database.with_name(database.name + suffix)
        if journal.exists() and journal.stat().st_size:
            raise ValueError("Journal pendente; aguarde a execução terminar antes da coleta.")
    before = database.stat()
    uri = "file:" + quote(str(database), safe="/") + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        total = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        rows = connection.execute("SELECT id,business_id,stage,ok,started_at,finished_at,run_dir,result FROM runs WHERE id IN (SELECT MIN(id) FROM runs UNION SELECT MAX(id) FROM runs) ORDER BY id").fetchall()
    after = database.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError("Banco mudou durante a leitura; não foi emitido diagnóstico parcial.")
    return {"read_only": True, "total_runs": total, "runs": [inspect_run(row) for row in rows], "runtime_helpers_present": {str(path): path.is_file() for path in RUNTIME_HELPERS}, "credentials_read": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verificar", action="store_true", help="Mostrar plano estático; não ler arquivos nem consultar usuário")
    args = parser.parse_args(argv)
    if args.verificar:
        print(json.dumps({"read_only": True, "database": str(HOME_DIR / "state" / "fabrica.sqlite3"), "selection": "primeira e última execução", "output": "metadados e categorias fixas; sem mensagens ou logs brutos", "subprocesses": False, "network": False, "credentials_read": False}, ensure_ascii=False))
    else:
        print(json.dumps(collect(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError, sqlite3.Error) as error:
        message = str(error) if type(error) is ValueError else "Falha na leitura local; nenhum arquivo foi alterado."
        print(json.dumps({"error_type": type(error).__name__, "message": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
