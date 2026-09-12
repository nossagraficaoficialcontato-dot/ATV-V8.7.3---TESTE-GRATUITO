import os,sys,tempfile,importlib.util,base64,json
from pathlib import Path
os.environ['ATV_ENV']='local';os.environ['ATV_COOKIE_SECURE']='0';os.environ['ATV_ADMIN_PIN']='246810';os.environ['ATV_ADMIN_KEY']='AD!!';os.environ['ATV_DATA_DIR']=tempfile.mkdtemp(prefix='atv-v86-')
spec=importlib.util.spec_from_file_location('atvv86',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from fastapi.testclient import TestClient
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=')
JPG=bytes.fromhex('FFD8FFE000104A46494600010100000100010000FFD9')
PSD=b'8BPS'+b'\x00\x01'+b'\x00'*20

def ck(n,c,d=None):
    if not c: raise AssertionError(f'{n}: {d}')
    print('✓',n)

admin=TestClient(m.app)
r=admin.post('/api/admin/auth/step1',json={'pin':'246810'});assert r.status_code==200,r.text
r=admin.post('/api/admin/auth/step2',json={'challenge':r.json()['challenge'],'key':'AD!!'});assert r.status_code==200,r.text
AH={'X-Admin-CSRF-Token':r.json()['csrfToken']}

seller=TestClient(m.app)
r=seller.post('/api/auth/register',json={'firstName':'Seller','lastName':'V86','email':'seller86@test.local','password':'Senha@123','role':'collaborator'});assert r.status_code==200,r.text
SH={'X-CSRF-Token':r.json()['account']['csrfToken']};sid=r.json()['account']['id']

def create(title,tier,name,data,extra=None):
    mime='image/jpeg' if name.endswith('.jpg') else 'image/png' if name.endswith('.png') else 'application/octet-stream'
    up=seller.post('/api/uploads/resource',headers=SH,files={'file':(name,data,mime),'preview':('preview.png',PNG,'image/png')},data={'metadata':'{}','previewMethod':'TEST'})
    assert up.status_code==200,up.text
    uid=up.json()['uploadId']
    if extra:
        rr=seller.post(f'/api/uploads/{uid}/files',headers=SH,files=[('files',(n,d,ct)) for n,d,ct in extra]);assert rr.status_code==200,rr.text
    rr=seller.post('/api/seller/products',headers=SH,json={'title':title,'category':'OUTROS','accessTier':tier,'description':'teste','tags':[],'uploadId':uid});assert rr.status_code==200,rr.text
    p=rr.json(); rr=admin.post(f"/api/admin/products/{p['id']}/approve",headers=AH,json={});assert rr.status_code==200,rr.text
    return rr.json()

# 1) Suspender some do painel + notificação.
p_suspend=create('Asset Incompatível V86','PREMIUM','incompativel.psd',PSD)
r=admin.patch(f"/api/admin/products/{p_suspend['id']}",headers=AH,json={'status':'SUSPENDED','hidden':True})
assert r.status_code==200,r.text
lst=seller.get('/api/seller/products').json()
ck('01 asset suspenso não aparece em Meus Recursos',all(x['id']!=p_suspend['id'] for x in lst),lst)
sumry=seller.get('/api/seller/summary').json()
ck('02 asset suspenso não aparece nos produtos do resumo',all(x['id']!=p_suspend['id'] for x in sumry['products']),sumry['products'])
notif=seller.get('/api/notifications').json()
ck('03 suspensão gera notificação',notif['unreadCount']>=1,notif)
n=next(x for x in notif['items'] if x['productId']==p_suspend['id'])
ck('04 notificação informa Incompatibilidade','Incompatibilidade' in n['title'] and 'chat' in n['message'].lower(),n)
ck('05 notificação oferece abrir chat',n['action']=='OPEN_CHAT',n)
r=seller.post('/api/notifications/read',headers=SH,json={'ids':[n['id']]});assert r.status_code==200,r.text
ck('06 notificação pode ser marcada como lida',seller.get('/api/notifications').json()['unreadCount']==0)

# 2) Remover asset hard delete também notifica e some.
p_delete=create('Asset Removido V86','FREE','removido.jpg',JPG)
r=admin.delete(f"/api/admin/products/{p_delete['id']}",headers=AH);assert r.status_code==200,r.text
ck('07 remover asset some do painel do colaborador',all(x['id']!=p_delete['id'] for x in seller.get('/api/seller/products').json()))
n2=seller.get('/api/notifications').json()
ck('08 remoção gera notificação persistente',any(x['productId']==p_delete['id'] and 'Incompatibilidade' in x['message'] for x in n2['items']),n2)

# 3) Downloads Premium em todos os formatos + path legado + retry do token.
p_psd=create('Premium PSD V86','PREMIUM','arquivo.psd',PSD)
p_png=create('Premium PNG V86','PREMIUM','arquivo.png',PNG)
p_jpg=create('Premium JPG V86','PREMIUM','arquivo.jpg',JPG)
p_max=create('Premium Max V86','PREMIUM_MAX','principal.psd',PSD,[('interno.png',PNG,'image/png')])

# Simula caminhos absolutos antigos mantendo os arquivos atuais em data/originals.
with m.db() as c:
    for pid in (p_psd['id'],p_png['id'],p_jpg['id']):
        row=c.execute('SELECT original_path FROM products WHERE id=?',(pid,)).fetchone();c.execute('UPDATE products SET original_path=? WHERE id=?',(str(Path('C:/VERSAO-ANTIGA/data/originals')/Path(row['original_path']).name),pid))
    for row in c.execute('SELECT id,original_path FROM product_files WHERE product_id=?',(p_max['id'],)).fetchall():
        c.execute('UPDATE product_files SET original_path=? WHERE id=?',(str(Path('C:/VERSAO-ANTIGA/data/originals')/Path(row['original_path']).name),row['id']))

premium=TestClient(m.app)
r=premium.post('/api/auth/register',json={'firstName':'Premium','lastName':'V86','email':'premium86@test.local','password':'Senha@123','role':'client'});assert r.status_code==200,r.text
PH={'X-CSRF-Token':r.json()['account']['csrfToken']}
assert premium.post('/api/account/simulate-plan',headers=PH,json={'plan':'PREMIUM','cycle':'MONTHLY'}).status_code==200

def consume_get(pid):
    rr=premium.post('/api/downloads/consume',headers=PH,json={'productId':pid,'source':'PREMIUM'});assert rr.status_code==200,rr.text
    url=rr.json()['downloadUrl'];g=premium.get(url);assert g.status_code==200,(pid,g.status_code,g.text[:200] if hasattr(g,'text') else '')
    return url,g

u_psd,g=consume_get(p_psd['id']);ck('09 download Premium PSD funciona',g.status_code==200)
ck('10 mesmo token pode atender retry/range do navegador',premium.get(u_psd).status_code==200)
_,g=consume_get(p_png['id']);ck('11 download Premium PNG funciona',g.status_code==200)
_,g=consume_get(p_jpg['id']);ck('12 download Premium JPG funciona',g.status_code==200)
_,g=consume_get(p_max['id']);ck('13 download Premium Max ZIP funciona',g.status_code==200 and g.headers.get('content-type','').startswith('application/zip'),g.headers)
acc=premium.get('/api/account').json()
ck('14 quatro recursos diferentes contam 4/10 Premium',acc['quotas']['premium']['used']==4,acc['quotas'])
ck('15 retry do mesmo token não duplica cota',acc['quotas']['premium']['used']==4,acc['quotas'])

# 4) Frontend: chat preserva rascunho, caixa não estoura, Max entra na Home Premium.
html=Path(sys.argv[2]).read_text(encoding='utf-8')
ck('16 build V8.7',"const ATV_BUILD='V8.7';" in html)
ck('17 chat preserva rascunho durante polling','existingDraft=fromPoll' in html and "$('#supportReplyText').value=existingDraft" in html)
ck('18 polling não rerenderiza conversa sem mudança','signature!==state.supportLastSignature' in html)
ck('19 textarea do chat usa grid minmax e min-width zero','grid-template-columns:minmax(0,1fr) auto' in html and 'min-width:0!important' in html)
ck('20 Home Premium reserva Premium Max','state.account?.premiumActive&&premiumMax.length' in html and '[premiumMax[0],...premiumOnly' in html)
ck('21 painel possui aviso de incompatibilidade','sellerModerationNotice()' in html and 'Abrir chat' in html)
ck('22 sino usa notificações reais','/api/notifications' in html and 'notificationCounter' in html)
print(json.dumps({'PASS':True,'checks':22},ensure_ascii=False))
