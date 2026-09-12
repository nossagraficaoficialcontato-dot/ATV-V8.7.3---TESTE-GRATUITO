from __future__ import annotations
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

BUILD = "V8.7.3"
HOST = "127.0.0.1"
PORT = int(os.getenv("ATV_PORT", "8000"))
NO_BROWSER = os.getenv("ATV_NO_BROWSER", "0") == "1"


def get_health(timeout=1.2):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health?ts={time.time()}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def port_in_use():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((HOST, PORT)) == 0
    finally:
        s.close()


def wait_and_open():
    for _ in range(120):
        h = get_health()
        if h:
            if h.get("build") != BUILD:
                print(f"\n[ERRO] A porta {PORT} respondeu com {h.get('build') or 'uma versão desconhecida'}, não {BUILD}.")
                return
            print(f"\n[OK] Servidor confirmado: {h.get('build')} / API {h.get('apiVersion')}")
            print("[OK] A V8.7.3 correta está ativa. Navegador liberado.")
            if not NO_BROWSER:
                webbrowser.open(f"http://localhost:{PORT}/?build={BUILD}#/home")
            return
        time.sleep(0.25)
    print("\n[ERRO] O servidor não ficou pronto a tempo.")


def main():
    if port_in_use():
        h = get_health()
        print("\n============================================================")
        print("  ATV NÃO INICIADA — PORTA JÁ ESTÁ EM USO")
        print("============================================================")
        if h:
            print(f"\nVersão que já está rodando: {h.get('build') or 'anterior/sem identificação'}")
            print(f"Versão que você tentou abrir: {BUILD}")
            print("\nFeche a janela da ATV anterior com CTRL+C e execute a V8.7.3 novamente.")
        else:
            print(f"\nA porta {PORT} está sendo usada por outro programa.")
        if sys.stdin.isatty():
            input("\nPressione ENTER para fechar...")
        return 3

    threading.Thread(target=wait_and_open, daemon=True).start()
    import uvicorn
    print(f"[OK] Iniciando {BUILD} na porta {PORT}...")
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
