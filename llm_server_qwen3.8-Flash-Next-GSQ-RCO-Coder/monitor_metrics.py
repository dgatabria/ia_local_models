#!/usr/bin/env python3
"""
Monitor en tiempo real de actividad, velocidad (t/s) y hardware para Strata (Qwen3.8-Coder).
Consulta http://localhost:8084/metrics en un loop infinito limpiando la consola.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

PHASE_TRANSLATIONS = {
    "reading the prompt": "📖 Procesando el prompt (Prefill)",
    "thinking": "🧠 Pensando (Modo Thinking / Reasoning)",
    "answering": "✍️  Escribiendo respuesta",
    "calling a tool": "🛠️  Ejecutando llamada a herramienta (Tool Call)",
    "tool call complete": "✅ Llamada a herramienta completada",
}

STATE_TRANSLATIONS = {
    "idle": "💤 Inactivo (Esperando requests)",
    "reading": "📖 Leyendo prompt",
    "generating": "⚡ Generando tokens",
}

# Colores ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
RED = "\033[91m"


def fmt_num(n):
    if n is None:
        return "-"
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


def progress_bar(current, total, width=28):
    if not current or not total or total <= 0:
        return ""
    ratio = min(max(float(current) / float(total), 0.0), 1.0)
    filled = int(round(width * ratio))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {ratio * 100:5.1f}%"


def clear_screen():
    # Secuencia ANSI para limpiar pantalla y llevar cursor al inicio sin parpadeo
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def fetch_metrics(url, timeout=3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def render_dashboard(data, url, interval):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    eng = data.get("engine") or {}
    live = data.get("live") or {}
    hw = data.get("hardware") or {}
    hw_static = data.get("hardware_static") or {}
    totals = data.get("totals") or {}
    requests = data.get("requests") or []

    model_name = eng.get("model", "desconocido")
    ctx_max = eng.get("context") or eng.get("max_context") or 0
    kv_type = eng.get("kv", "-")
    drafting = eng.get("drafting", "-")

    state_raw = live.get("state") or "idle"
    phase_raw = live.get("phase")
    queued = live.get("queued") or 0

    if state_raw == "idle":
        activity_color = DIM
        activity_label = STATE_TRANSLATIONS["idle"]
    elif phase_raw in PHASE_TRANSLATIONS:
        activity_color = YELLOW if phase_raw == "reading the prompt" else (
            MAGENTA if phase_raw == "thinking" else GREEN
        )
        activity_label = PHASE_TRANSLATIONS[phase_raw]
    else:
        activity_color = CYAN
        activity_label = phase_raw or STATE_TRANSLATIONS.get(state_raw, state_raw)

    lines = []
    lines.append(f"{BOLD}{CYAN}============================================================================={RESET}")
    lines.append(
        f"{BOLD} 🚀 MONITOR STRATA LLM {RESET} | {BOLD}{model_name}{RESET} "
        f"(Ctx: {fmt_num(ctx_max)} | KV: {kv_type} | Draft: {drafting})"
    )
    lines.append(
        f" {DIM}URL: {url} | Refresco: {interval}s | Hora: {now_str} (Ctrl+C para salir){RESET}"
    )
    lines.append(f"{BOLD}{CYAN}============================================================================={RESET}")

    # --- 1. ACTIVIDAD EN VIVO ---
    lines.append(f"\n{BOLD}📌 ACTIVIDAD ACTUAL:{RESET}")
    queue_str = f"  {YELLOW}(En cola: {queued}){RESET}" if queued > 0 else ""
    lines.append(f"   Estado:         {BOLD}{activity_color}{activity_label}{RESET}{queue_str}")

    tok_s = live.get("tok_s")
    tok_s_mean = live.get("tok_s_mean")
    prefill_tok_s = live.get("prefill_tok_s_mean")
    elapsed_s = live.get("elapsed_s")

    if state_raw == "reading":
        p_read = live.get("prompt_read") or 0
        p_total = live.get("prompt_total") or live.get("prompt_tokens") or 0
        bar = progress_bar(p_read, p_total)
        speed_str = f"{BOLD}{YELLOW}{fmt_num(prefill_tok_s)} tok/s{RESET} (lectura de prompt)"
        lines.append(f"   Velocidad:      {speed_str}")
        lines.append(f"   Prompt leído:   {fmt_num(p_read)} / {fmt_num(p_total)} tokens  {bar}")
        if elapsed_s is not None:
            lines.append(f"   Tiempo activo:  {elapsed_s:.1f} s")
    elif state_raw != "idle":
        gen = live.get("generated") or 0
        max_t = live.get("max_tokens") or 0
        p_tot = live.get("prompt_total") or live.get("prompt_tokens") or 0
        inst_str = f"{ fmt_num(tok_s) } tok/s" if tok_s is not None else "calculando..."
        mean_str = f"{ fmt_num(tok_s_mean) } tok/s" if tok_s_mean is not None else "-"
        lines.append(
            f"   Velocidad:      {BOLD}{GREEN}{inst_str}{RESET} (instantánea) | "
            f"{BOLD}{mean_str}{RESET} (promedio turno)"
        )
        if prefill_tok_s:
            lines.append(f"   Vel. Prefill:   {fmt_num(prefill_tok_s)} tok/s (Prompt: {fmt_num(p_tot)} tokens)")
        bar = progress_bar(gen, max_t) if max_t else ""
        lines.append(f"   Generados:      {BOLD}{fmt_num(gen)}{RESET} / {fmt_num(max_t)} tokens  {bar}")
        if elapsed_s is not None:
            lines.append(f"   Tiempo activo:  {elapsed_s:.1f} s")
    else:
        # Si está idle, mostrar la velocidad del último request como referencia rápida
        if requests:
            last = requests[0]
            last_dec = last.get("decode_tok_s")
            last_p_ms = last.get("prompt_ms") or 0
            last_p_read = last.get("prompt_read") or 0
            last_prefill_tps = (last_p_read / (last_p_ms / 1000.0)) if last_p_ms > 0 else 0
            lines.append(
                f"   Última vel.:    {GREEN}{fmt_num(last_dec)} tok/s{RESET} (generación) | "
                f"{YELLOW}{fmt_num(last_prefill_tps)} tok/s{RESET} (lectura prompt)"
            )
        else:
            lines.append("   Velocidad:      0.0 tok/s")

    # --- 2. HARDWARE EN VIVO ---
    gpu_name = hw_static.get("gpu_name") or "GPU"
    gpu_util = hw.get("gpu_util", 0)
    vram_used_gib = (hw.get("gpu_mem_used") or 0) / (1024 ** 3)
    vram_tot_gib = (hw.get("gpu_mem_total") or 0) / (1024 ** 3)
    vram_free_mib = eng.get("vram_free_mib", 0)
    gpu_temp = hw.get("gpu_temp", 0)
    gpu_pwr = hw.get("gpu_power") or 0.0
    gpu_pwr_lim = hw.get("gpu_power_limit") or 0.0
    pcie_rx = hw.get("gpu_pcie_rx_mb") or 0.0
    pcie_tx = hw.get("gpu_pcie_tx_mb") or 0.0
    disk_r = hw.get("disk_read_mb") or 0.0
    cpu_pct = hw.get("cpu") or 0.0
    ram_used_gib = (hw.get("ram_used") or 0) / (1024 ** 3)
    ram_tot_gib = (hw.get("ram_total") or 0) / (1024 ** 3)

    lines.append(f"\n{BOLD}🖥️  HARDWARE Y MEMORIA ({gpu_name}):{RESET}")
    lines.append(
        f"   GPU:            Uso {BOLD}{gpu_util:>3}%{RESET} | Temp {gpu_temp}°C | "
        f"Potencia {gpu_pwr:5.1f}W / {gpu_pwr_lim:.0f}W"
    )
    lines.append(
        f"   VRAM:           {vram_used_gib:5.2f} / {vram_tot_gib:5.2f} GiB "
        f"(Libre motor: {vram_free_mib} MiB | Caché expertos: {eng.get('expert_cache_mib', 0)} MiB)"
    )
    lines.append(
        f"   RAM Sistema:    {ram_used_gib:5.2f} / {ram_tot_gib:5.2f} GiB | CPU: {cpu_pct:4.1f}%"
    )
    lines.append(
        f"   Tráfico I/O:    Disco Lectura: {BOLD}{disk_r:6.1f} MB/s{RESET} | "
        f"PCIe RX: {pcie_rx:5.1f} MB/s | TX: {pcie_tx:5.1f} MB/s"
    )

    # --- 3. ÚLTIMOS REQUESTS COMPLETADOS ---
    lines.append(f"\n{BOLD}📊 ÚLTIMOS TURNOS COMPLETADOS:{RESET}")
    if not requests:
        lines.append("   (Sin requests completados aún)")
    else:
        for i, r in enumerate(requests[:3], start=1):
            r_id = r.get("id", "-")
            dur = r.get("duration_s", 0.0)
            p_tot = r.get("prompt_total") or r.get("prompt_tokens") or 0
            p_reused = r.get("reused") or 0
            p_read = r.get("prompt_read") or 0
            p_ms = r.get("prompt_ms") or 0.0
            prefill_tps = (p_read / (p_ms / 1000.0)) if p_ms > 0 else 0.0
            out_tok = r.get("output_tokens") or 0
            dec_tps = r.get("decode_tok_s")
            hit_rate = (r.get("hit_rate") or 0.0) * 100.0
            d_acc = r.get("drafts_accepted") or 0
            d_off = r.get("drafts_offered") or 0
            d_pct = (d_acc / d_off * 100.0) if d_off > 0 else 0.0
            file_mb = r.get("file_mb") or 0.0

            lines.append(
                f"   {BOLD}#{i} [{r_id}]{RESET} ({dur:.1f}s, fin={r.get('finish', '-')}) -> "
                f"Decode: {BOLD}{GREEN}{fmt_num(dec_tps)} tok/s{RESET} ({fmt_num(out_tok)} toks) | "
                f"Prefill: {YELLOW}{fmt_num(prefill_tps)} tok/s{RESET}"
            )
            lines.append(
                f"      Contexto: {fmt_num(p_tot)} toks (Caché KV: {fmt_num(p_reused)} | Nuevos: {fmt_num(p_read)}) | "
                f"VRAM Hit: {hit_rate:.1f}% | MTP Drafts: {d_acc}/{d_off} ({d_pct:.0f}%)"
            )

    # --- 4. TOTALES DE LA SESIÓN ---
    tot_req = totals.get("requests", 0)
    tot_out = totals.get("output_tokens", 0)
    tot_dec_ms = totals.get("decode_ms") or 0.0
    tot_avg_tps = (tot_out / (tot_dec_ms / 1000.0)) if tot_dec_ms > 0 else 0.0
    lines.append(f"\n{BOLD}📈 ACUMULADO SERVIDOR:{RESET}")
    lines.append(
        f"   Requests: {fmt_num(tot_req)} | Tokens generados: {fmt_num(tot_out)} | "
        f"Velocidad media global: {BOLD}{fmt_num(tot_avg_tps)} tok/s{RESET}"
    )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor en consola de velocidad (t/s) y actividad para Strata."
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=2.0,
        help="Segundos entre cada actualización (default: 2.0)"
    )
    parser.add_argument(
        "-u", "--url",
        type=str,
        default="http://localhost:8084/metrics",
        help="URL del endpoint /metrics de Strata (default: http://localhost:8084/metrics)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Imprimir una sola vez y salir (útil para pruebas)"
    )
    args = parser.parse_args()

    try:
        while True:
            try:
                data = fetch_metrics(args.url)
                output = render_dashboard(data, args.url, args.interval)
            except urllib.error.URLError as e:
                output = (
                    f"{RED}{BOLD}[!] No se pudo conectar a {args.url}:{RESET} {e}\n"
                    f"Reintentando en {args.interval}s..."
                )
            except Exception as e:
                output = f"{RED}{BOLD}[!] Error procesando métricas:{RESET} {e}"

            if not args.once:
                clear_screen()
            print(output, flush=True)

            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}Monitor finalizado.{RESET}")


if __name__ == "__main__":
    main()
