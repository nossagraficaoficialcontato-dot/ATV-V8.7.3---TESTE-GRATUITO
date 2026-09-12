from __future__ import annotations
import json, urllib.request
BASE='http://127.0.0.1:8000'
try:
    with urllib.request.urlopen(BASE+'/api/health?verify=v873', timeout=10) as r:
        h=json.loads(r.read().decode('utf-8'))
    print(f"[OK] Servidor respondeu. Build: {h.get('build')} | API: {h.get('apiVersion')}")
    if h.get('build')!='V8.7.3' or h.get('apiVersion')!='1.7.10':
        raise SystemExit('[ERRO] Esperado V8.7.3 / API 1.7.10.')
    print('\nRESULTADO FINAL: V8.7.3 ATIVA.')
except Exception as e:
    print('[ERRO] Falha ao confirmar V8.7.3:', e)
    raise SystemExit(1)
