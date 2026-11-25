#!/usr/bin/env python3
"""
Watcher en tiempo real de la actividad de Qwen Code (~/.qwen) + streaming en vivo desde Strata.
- Detecta automáticamente el proceso `qwen-code` (`cli-entry.js`) en ejecución y su `--session-id`.
- Localiza el archivo de sesión activo en `~/.qwen/projects/*/chats/*.jsonl`.
- Imprime en tiempo real:
  * 🧠 Bloques de Thinking (`thought: true`)
  * ✍️  Respuestas del asistente
  * 🛠️  Comandos y herramientas ejecutadas (`run_shell_command`, ediciones, búsquedas)
  * 📤 Resultados de herramientas y códigos de salida
  * ⚡ Streaming en vivo (`live.tail` y `tok/s`) desde Strata mientras el modelo piensa/genera
  * ⚙️  Subprocesos hijos en ejecución (ej. `dotnet test`, `check_all.sh`) mientras corre un tool call
"""

import argparse
import datetime
import glob
import json
import os
import sys
import time
import urllib.request

# Colores ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RED = "\033[91m"
CLEAR_LINE = "\033[2K\r"


def find_qwen_processes():
    """Encuentra procesos activos de qwen-code leyendo /proc."""
    procs = []
    for pid_dir in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(pid_dir))
            with open(f"{pid_dir}/cmdline", "rb") as f:
                raw = f.read()
            if not raw:
                continue
            args = [a.decode("utf-8", errors="replace") for a in raw.split(b"\x00") if a]
            cmd_str = " ".join(args)
            if "cli-entry.js" not in cmd_str and "qwen-code" not in cmd_str:
                continue
            # Ignorar este propio script o grep
            if "watch_qwen_activity" in cmd_str:
                continue
            session_id = None
            for i, arg in enumerate(args):
                if arg == "--session-id" and i + 1 < len(args):
                    session_id = args[i + 1]
                    break
                if arg.startswith("--session-id="):
                    session_id = arg.split("=", 1)[1]
                    break
            cwd = os.path.realpath(f"{pid_dir}/cwd")
            mtime = os.stat(pid_dir).st_mtime
            procs.append({
                "pid": pid,
                "session_id": session_id,
                "cwd": cwd,
                "mtime": mtime,
                "args": args,
            })
        except (OSError, ValueError):
            continue
    # Priorizar procesos con --session-id (como los lanzados por el arnés) y más recientes
    procs.sort(key=lambda p: (1 if p["session_id"] else 0, p["mtime"]), reverse=True)
    return procs


def find_child_commands(parent_pid):
    """Devuelve comandos hijos activos de un PID (para ver qué shell command está corriendo)."""
    children = []
    try:
        all_pids = [int(os.path.basename(p)) for p in glob.glob("/proc/[0-9]*") if os.path.basename(p).isdigit()]
        ppid_map = {}
        cmd_map = {}
        for pid in all_pids:
            try:
                with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as f:
                    stat_str = f.read()
                rparen = stat_str.rfind(")")
                fields = stat_str[rparen + 2:].split()
                ppid = int(fields[1])
                ppid_map.setdefault(ppid, []).append(pid)
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = [a.decode("utf-8", errors="replace") for a in f.read().split(b"\x00") if a]
                if args:
                    cmd_map[pid] = " ".join(args)
            except (OSError, ValueError, IndexError):
                continue

        # BFS desde parent_pid
        queue = list(ppid_map.get(parent_pid, []))
        visited = set(queue)
        while queue:
            curr = queue.pop(0)
            for nxt in ppid_map.get(curr, []):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
            cmd = cmd_map.get(curr, "")
            if cmd and not cmd.startswith("/bin/sh -c") and not cmd.startswith("bash -c"):
                children.append((curr, cmd))
    except Exception:
        pass
    return children


def resolve_chat_jsonl(qwen_dir, session_id=None):
    """Busca el archivo .jsonl de la sesión en ~/.qwen/projects/*/chats/."""
    pattern = os.path.join(os.path.expanduser(qwen_dir), "projects", "*", "chats", "*.jsonl")
    files = glob.glob(pattern)
    if not files:
        return None
    if session_id:
        for path in files:
            if os.path.basename(path) == f"{session_id}.jsonl":
                return path
    # Si no se especificó session_id o no apareció aún, devolver el más reciente por mtime
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return files[0]


def fmt_ts(iso_ts):
    if not iso_ts:
        return datetime.datetime.now().strftime("%H:%M:%S")
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return str(iso_ts)


def truncate_lines(text, max_lines=18, max_chars=1600):
    if not text:
        return ""
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n   {DIM}... [truncado, {len(text):,} chars totales]{RESET}"
    lines = text.splitlines()
    if len(lines) > max_lines:
        kept = lines[:max_lines // 2] + [f"   {DIM}... ({len(lines) - max_lines} líneas intermedias omitidas) ...{RESET}"] + lines[-max_lines // 2:]
        return "\n".join(kept)
    return "\n".join(lines)


def format_jsonl_event(raw_line, show_full_thinking=True):
    """Formatea una línea del archivo .jsonl de Qwen Code para imprimir en consola."""
    try:
        d = json.loads(raw_line)
    except Exception:
        return None

    ev_type = d.get("type")
    subtype = d.get("subtype")
    ts = fmt_ts(d.get("timestamp"))
    out = []

    if ev_type == "user":
        msg = d.get("message") or {}
        parts = msg.get("parts") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
        if texts:
            prompt_preview = truncate_lines("\n".join(texts), max_lines=10, max_chars=900)
            out.append(f"\n{BOLD}{BLUE}┌── 👤 [{ts}] PROMPT DE USUARIO / ARNÉS ───────────────────────────────────{RESET}")
            for line in prompt_preview.splitlines():
                out.append(f"{BLUE}│{RESET} {line}")

    elif ev_type == "assistant":
        msg = d.get("message") or {}
        parts = msg.get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("thought") is True and part.get("text"):
                thought_txt = part["text"].strip()
                if not show_full_thinking:
                    thought_txt = truncate_lines(thought_txt, max_lines=16, max_chars=1500)
                out.append(f"\n{BOLD}{MAGENTA}┌── 🧠 [{ts}] THINKING (Razonamiento del modelo) ──────────────────────────{RESET}")
                for line in thought_txt.splitlines():
                    out.append(f"{MAGENTA}│{RESET} {DIM}{line}{RESET}")
            elif part.get("text"):
                ans_txt = part["text"].strip()
                out.append(f"\n{BOLD}{GREEN}┌── ✍️  [{ts}] RESPUESTA DEL ASISTENTE ────────────────────────────────────{RESET}")
                for line in ans_txt.splitlines():
                    out.append(f"{GREEN}│{RESET} {line}")
            elif part.get("functionCall"):
                fc = part["functionCall"]
                fn_name = fc.get("name", "tool")
                args = fc.get("args") or {}
                desc = args.get("description") or ""
                out.append(
                    f"\n{BOLD}{YELLOW}┌── 🛠️  [{ts}] EJECUTANDO TOOL: {fn_name} "
                    f"{f'({desc})' if desc else ''} ──────────────{RESET}"
                )
                if "command" in args:
                    out.append(f"{YELLOW}│ Comando:{RESET} {BOLD}{args['command']}{RESET}")
                else:
                    args_str = json.dumps(args, ensure_ascii=False, indent=2)
                    for line in truncate_lines(args_str, max_lines=14, max_chars=1000).splitlines():
                        out.append(f"{YELLOW}│{RESET} {line}")

    elif ev_type == "tool_result":
        msg = d.get("message") or {}
        parts = msg.get("parts") or []
        for part in parts:
            fr = (part or {}).get("functionResponse") if isinstance(part, dict) else None
            if not fr:
                continue
            fn_name = fr.get("name", "tool")
            resp_obj = fr.get("response") or {}
            output_txt = resp_obj.get("output") or ""
            out.append(f"\n{BOLD}{CYAN}┌── 📤 [{ts}] RESULTADO DE TOOL ({fn_name}) ────────────────────────────────{RESET}")
            for line in truncate_lines(str(output_txt), max_lines=14, max_chars=1200).splitlines():
                out.append(f"{CYAN}│{RESET} {DIM}{line}{RESET}")

    elif ev_type == "system" and subtype == "ui_telemetry":
        ui_ev = ((d.get("systemPayload") or {}).get("uiEvent")) or {}
        ev_name = ui_ev.get("event.name")
        if ev_name == "qwen-code.api_response":
            in_tok = ui_ev.get("input_token_count", 0)
            cache_tok = ui_ev.get("cached_content_token_count", 0)
            think_tok = ui_ev.get("thoughts_token_count", 0)
            out_tok = ui_ev.get("output_token_count", 0)
            dur_s = (ui_ev.get("duration_ms") or 0) / 1000.0
            gen_ms = ui_ev.get("generation_ms") or 0
            tps = (out_tok / (gen_ms / 1000.0)) if gen_ms > 0 else 0.0
            out.append(
                f"{DIM}└── 📊 [{ts}] Turno LLM completado en {dur_s:.1f}s ({tps:.1f} tok/s) | "
                f"Contexto: {in_tok:,} (Caché: {cache_tok:,}) | Thinking: {think_tok:,} toks | Output: {out_tok:,} toks{RESET}"
            )

    return "\n".join(out) if out else None


def fetch_strata_live(metrics_url):
    try:
        req = urllib.request.Request(metrics_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("live") or {}
    except Exception:
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="Observa en tiempo real el proceso qwen-code activo, sus logs en ~/.qwen y el stream de Strata."
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=1.0,
        help="Segundos entre cada chequeo (default: 1.0)"
    )
    parser.add_argument(
        "-n", "--history",
        type=int,
        default=15,
        help="Cantidad de líneas finales del .jsonl a mostrar al arrancar (default: 15)"
    )
    parser.add_argument(
        "--session-id",
        type=str,
        default=None,
        help="Forzar un session-id específico (opcional; por defecto lo detecta del proceso qwen-code)"
    )
    parser.add_argument(
        "--qwen-dir",
        type=str,
        default="~/.qwen",
        help="Directorio base de Qwen Code (default: ~/.qwen)"
    )
    parser.add_argument(
        "--metrics-url",
        type=str,
        default="http://localhost:8084/metrics",
        help="URL de métricas de Strata para ver progreso en vivo entre turnos (default: http://localhost:8084/metrics)"
    )
    parser.add_argument(
        "--truncate-thinking",
        action="store_true",
        help="Truncar bloques de thinking muy largos en lugar de mostrarlos completos"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Mostrar el estado actual / historial reciente y salir"
    )
    args = parser.parse_args()

    procs = find_qwen_processes()
    active_proc = procs[0] if procs else None
    target_session = args.session_id or (active_proc["session_id"] if active_proc else None)
    jsonl_path = resolve_chat_jsonl(args.qwen_dir, target_session)

    print(f"{BOLD}{CYAN}===================================================================================={RESET}")
    print(f"{BOLD} 🔎 MONITOR EN VIVO DE ACTIVIDAD QWEN CODE + STRATA{RESET}")
    if active_proc:
        print(
            f" {GREEN}● Proceso activo:{RESET} PID {BOLD}{active_proc['pid']}{RESET} | "
            f"Session: {BOLD}{active_proc['session_id'] or 'auto'}{RESET}"
        )
        print(f" {DIM}  Directorio (cwd): {active_proc['cwd']}{RESET}")
    else:
        print(f" {YELLOW}○ Sin proceso qwen-code activo en este instante (mostrando última sesión){RESET}")

    if jsonl_path:
        print(f" {CYAN}📄 Archivo de sesión:{RESET} {jsonl_path}")
    else:
        print(f" {RED}[!] No se encontró ningún archivo .jsonl en {args.qwen_dir}{RESET}")
        sys.exit(1)
    print(f"{BOLD}{CYAN}===================================================================================={RESET}")

    file_pos = 0
    current_path = jsonl_path

    # Mostrar las últimas N líneas al iniciar
    try:
        with open(current_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            file_pos = f.tell()
        for raw_line in all_lines[-args.history:]:
            formatted = format_jsonl_event(raw_line, show_full_thinking=not args.truncate_thinking)
            if formatted:
                print(formatted)
    except OSError as e:
        print(f"{RED}[!] Error leyendo {current_path}: {e}{RESET}")

    if args.once:
        if active_proc:
            children = find_child_commands(active_proc["pid"])
            if children:
                print(f"\n{BOLD}{YELLOW}⚙️  Subprocesos en ejecución ahora mismo (PID {active_proc['pid']}):{RESET}")
                for cpid, ccmd in children[-3:]:
                    print(f"   [PID {cpid}] {ccmd[:140]}")
        return

    print(f"\n{DIM}--- Siguiendo actividad en tiempo real (Refresco: {args.interval}s | Ctrl+C para salir) ---{RESET}\n")

    try:
        while True:
            # 1. Revisar si cambió el proceso qwen-code o si arrancó una nueva sesión
            procs = find_qwen_processes()
            active_proc = procs[0] if procs else None
            if not args.session_id and active_proc and active_proc.get("session_id"):
                new_path = resolve_chat_jsonl(args.qwen_dir, active_proc["session_id"])
                if new_path and new_path != current_path:
                    sys.stdout.write(CLEAR_LINE)
                    print(f"\n{BOLD}{CYAN}>> Cambiando a nueva sesión detectada: {new_path}{RESET}")
                    current_path = new_path
                    file_pos = 0

            # 2. Leer líneas nuevas agregadas al .jsonl
            if current_path and os.path.exists(current_path):
                try:
                    with open(current_path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(file_pos)
                        new_lines = f.readlines()
                        file_pos = f.tell()
                    for raw_line in new_lines:
                        formatted = format_jsonl_event(raw_line, show_full_thinking=not args.truncate_thinking)
                        if formatted:
                            sys.stdout.write(CLEAR_LINE)
                            print(formatted, flush=True)
                except OSError:
                    pass

            # 3. Mostrar barra de estado en vivo (qué está haciendo en este segundo)
            live = fetch_strata_live(args.metrics_url)
            state = live.get("state") or "idle"
            phase = live.get("phase") or state

            if state == "reading":
                p_read = live.get("prompt_read") or 0
                p_tot = live.get("prompt_total") or live.get("prompt_tokens") or 0
                prefill_tps = live.get("prefill_tok_s_mean") or 0.0
                el = live.get("elapsed_s") or 0.0
                status_line = (
                    f"{YELLOW}⏳ [EN VIVO - STRATA]{RESET} 📖 Leyendo prompt: "
                    f"{p_read:,}/{p_tot:,} toks ({prefill_tps:,.1f} tok/s) [{el:.1f}s]"
                )
                sys.stdout.write(CLEAR_LINE + status_line[:150])
                sys.stdout.flush()
            elif state != "idle":
                gen = live.get("generated") or 0
                tps = live.get("tok_s") or live.get("tok_s_mean") or 0.0
                el = live.get("elapsed_s") or 0.0
                phase_icon = "🧠 Pensando" if phase == "thinking" else (
                    "✍️ Respondiendo" if phase == "answering" else f"🛠️ {phase}"
                )
                status_line = (
                    f"{MAGENTA}⚡ [EN VIVO - STRATA]{RESET} {phase_icon}: "
                    f"{gen:,} tokens generados a {BOLD}{GREEN}{tps:.1f} tok/s{RESET} [{el:.1f}s]"
                )
                sys.stdout.write(CLEAR_LINE + status_line[:150])
                sys.stdout.flush()
            elif active_proc:
                children = find_child_commands(active_proc["pid"])
                if children:
                    cpid, ccmd = children[-1]
                    short_cmd = ccmd if len(ccmd) <= 85 else "..." + ccmd[-82:]
                    status_line = (
                        f"{CYAN}⚙️  [EJECUTANDO COMANDO HIJO PID {cpid}]{RESET} {short_cmd}"
                    )
                    sys.stdout.write(CLEAR_LINE + status_line)
                    sys.stdout.flush()
                else:
                    sys.stdout.write(
                        CLEAR_LINE + f"{DIM}💤 [PID {active_proc['pid']}] Esperando próximo evento...{RESET}"
                    )
                    sys.stdout.flush()

            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write(CLEAR_LINE)
        print(f"\n{DIM}Watcher finalizado.{RESET}")


if __name__ == "__main__":
    main()
