from pathlib import Path
import os,sys,tempfile,importlib.util,base64,json
os.environ['ATV_ENV']='local';os.environ['ATV_COOKIE_SECURE']='0';os.environ['ATV_ADMIN_PIN']='246810';os.environ['ATV_ADMIN_KEY']='AD!!';os.environ['ATV_DATA_DIR']=tempfile.mkdtemp(prefix='atv-v873-')
spec=importlib.util.spec_from_file_location('atvv873',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from fastapi.testclient import TestClient
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=')
JPG=bytes.fromhex('FFD8FFE000104A46494600010100000100010000FFD9')
def ck(n,c,d=None):
 print(('✓' if c else '✗'),n)
 if not c: raise AssertionError(f'{n}: {d}')

def reg(email):
 c=TestClient(m.app);r=c.post('/api/auth/register',json={'firstName':'Colab','lastName':'873','email':email,'password':'Senha@123','role':'collaborator'});assert r.status_code==200,r.text
 a=r.json()['account'];return c,a,{'X-CSRF-Token':a['csrfToken']}

def make(c,H,title):
 u=c.post('/api/uploads/resource',headers=H,files={'file':('arte.jpg',JPG,'image/jpeg'),'preview':('preview.png',PNG,'image/png')},data={'metadata':'{}','previewMethod':'V873'})
 assert u.status_code==200,u.text
 r=c.post('/api/seller/products',headers=H,json={'title':title,'category':'JPG','accessTier':'PREMIUM','description':'teste','tags':[],'uploadId':u.json()['uploadId']})
 assert r.status_code==200,r.text
 return r.json()

pub=TestClient(m.app)
h=pub.get('/api/health').json();ck('01 build V8.7.3',h['build']=='V8.7.3',h);ck('02 API 1.7.10',h['apiVersion']=='1.7.10',h)

c1,a1,H1=reg('seller873a@test.local');c2,a2,H2=reg('seller873b@test.local')
p1=make(c1,H1,'Sem histórico 873')
# other collaborator cannot delete
r=c2.delete(f"/api/seller/products/{p1['id']}",headers=H2);ck('03 outro colaborador não exclui',r.status_code==404,r.text)
# owner hard deletes no-history item
r=c1.delete(f"/api/seller/products/{p1['id']}",headers=H1);ck('04 owner exclui próprio asset',r.status_code==200 and r.json()['mode']=='DELETED',r.text)
ck('05 asset excluído some da lista',all(x['id']!=p1['id'] for x in c1.get('/api/seller/products').json()))
with m.db() as db: ck('06 hard delete remove registro',db.execute('SELECT 1 FROM products WHERE id=?',(p1['id'],)).fetchone() is None)

p2=make(c1,H1,'Com histórico 873')
with m.db() as db:
 row=db.execute('SELECT * FROM users WHERE id=?',(a1['id'],)).fetchone()
 # create a valid historical download event; schema columns copied from production table
 cols=[x['name'] for x in db.execute('PRAGMA table_info(download_events)').fetchall()]
 data={'id':'evt-v873','user_id':a1['id'],'product_id':p2['id'],'source':'PREMIUM','day':'2026-08-20','created_at':m.iso_now()}
 use={k:v for k,v in data.items() if k in cols}
 db.execute(f"INSERT INTO download_events({','.join(use)}) VALUES({','.join('?' for _ in use)})",tuple(use.values()))
r=c1.delete(f"/api/seller/products/{p2['id']}",headers=H1);ck('07 asset com histórico é arquivado',r.status_code==200 and r.json()['mode']=='ARCHIVED',r.text)
ck('08 arquivado some do painel ativo',all(x['id']!=p2['id'] for x in c1.get('/api/seller/products').json()))
with m.db() as db:
 row=db.execute('SELECT status,hidden,admin_removed FROM products WHERE id=?',(p2['id'],)).fetchone();ck('09 histórico preserva produto oculto',row and row['status']=='SUSPENDED' and row['hidden']==1 and row['admin_removed']==1,dict(row) if row else None)
 ck('10 evento histórico preservado',db.execute('SELECT 1 FROM download_events WHERE product_id=?',(p2['id'],)).fetchone() is not None)

src=Path(sys.argv[2]).read_text(encoding='utf-8')
ck('11 botão Excluir existe no painel','deleteSellerAssetV873' in src and '>Excluir</button>' in src)
ck('12 botão chama DELETE do seller',"method:'DELETE'" in src and '/api/seller/products/${encodeURIComponent(p.id)}' in src)
ck('13 confirmação explica preservação','preservará o histórico financeiro' in src)
ck('14 aviso anual exato','Em breve no cartão. Se quiser assinar por PIX, fale com o suporte.' in src)
ck('15 primeira tela aprovada preservada','atv-auth-choice-v872' in src)
print(json.dumps({'PASS':True,'checks':15},ensure_ascii=False))
