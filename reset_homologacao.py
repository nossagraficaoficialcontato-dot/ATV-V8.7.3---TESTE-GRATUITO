from pathlib import Path
import os, shutil
ROOT=Path(__file__).resolve().parent
DATA=Path(os.getenv('ATV_DATA_DIR',str(ROOT/'data')))
for name in ['atv_homologacao.sqlite3','atv_homologacao.sqlite3-shm','atv_homologacao.sqlite3-wal']:
    p=DATA/name
    if p.exists(): p.unlink()
for folder in ['previews','originals']:
    p=DATA/folder
    if p.exists():
        for f in p.iterdir():
            if f.is_file(): f.unlink()
print('Base de homologação limpa. Ela será recriada e semeada na próxima inicialização.')
