from pathlib import Path
import os,sys,tempfile,importlib.util,base64,re,json

os.environ['ATV_ENV']='local';os.environ['ATV_COOKIE_SECURE']='0'
os.environ['ATV_ADMIN_PIN']='246810';os.environ['ATV_ADMIN_KEY']='AD!!'
os.environ['ATV_DATA_DIR']=tempfile.mkdtemp(prefix='atv-v871-')

spec=importlib.util.spec_from_file_location('atvv871',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from fastapi.testclient import TestClient

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=')
JPG=bytes.fromhex('FFD8FFE000104A46494600010100000100010000FFD9')
PSD=b'8BPS'+b'\x00\x01'+b'\x00'*40

def ck(name,cond,detail=None):
    print(('✓' if cond else '✗'),name)
    if not cond: raise AssertionError(f'{name}: {detail}')

def reg(role,email):
    c=TestClient(m.app)
    r=c.post('/api/auth/register',json={'firstName':'Teste','lastName':'871','email':email,'password':'Senha@123','role':role})
    assert r.status_code==200,r.text
    acc=r.json()['account'];return c,acc,{'X-CSRF-Token':acc['csrfToken']}

def admin_login():
    c=TestClient(m.app);r=c.post('/api/admin/auth/step1',json={'pin':'246810'});assert r.status_code==200
    r=c.post('/api/admin/auth/step2',json={'challenge':r.json()['challenge'],'key':'AD!!'});assert r.status_code==200
    return c,{'X-Admin-CSRF-Token':r.json()['csrfToken']}

def make_product(c,H,title,cat='JPG',tier='PREMIUM',name='file.jpg',data=JPG,canva=None):
    mime='image/jpeg' if name.lower().endswith(('.jpg','.jpeg')) else 'image/png' if name.lower().endswith('.png') else 'application/octet-stream'
    up=c.post('/api/uploads/resource',headers=H,files={'file':(name,data,mime),'preview':('preview.png',PNG,'image/png')},data={'metadata':'{}','previewMethod':'V871'})
    assert up.status_code==200,up.text
    payload={'title':title,'category':cat,'accessTier':tier,'description':'teste','tags':[],'uploadId':up.json()['uploadId']}
    if canva:payload['canvaUrl']=canva
    r=c.post('/api/seller/products',headers=H,json=payload);assert r.status_code==200,r.text
    return r.json()

pub=TestClient(m.app);h=pub.get('/api/health').json()
ck('01 build V8.7.1',h.get('build')=='V8.7.1',h)
ck('02 API 1.7.8',h.get('apiVersion')=='1.7.8',h)
rt=pub.get('/api/runtime-status').json()
ck('03 migration V8.7.1',rt['migrations'].get('v871_ui_wiring_migrated') is True,rt)

admin,AH=admin_login()
seller,sa,SH=reg('collaborator','seller871@test.local')
client,ca,CH=reg('client','premium871@test.local')

canva=make_product(seller,SH,'Canva 871','CANVA','PREMIUM','canva.jpg',JPG,'https://www.canva.com/design/ATV871')
assert admin.post(f"/api/admin/products/{canva['id']}/approve",headers=AH,json={}).status_code==200
pv=pub.get(f"/api/products/{canva['id']}").json()
ck('04 Canva URL protegida','canvaUrl' not in pv and 'external_url' not in pv,pv)
ck('05 Free não abre Canva',client.post(f"/api/products/{canva['id']}/external-access",headers=CH,json={}).status_code==403)
assert client.post('/api/account/simulate-plan',headers=CH,json={'plan':'PREMIUM','cycle':'MONTHLY'}).status_code==200
r=client.post(f"/api/products/{canva['id']}/external-access",headers=CH,json={})
ck('06 Premium abre Canva',r.status_code==200 and r.json().get('url')=='https://www.canva.com/design/ATV871',r.text)

r=client.post('/api/account/collections',headers=CH,json={'name':'Campanhas'});assert r.status_code==200;r_id=r.json()['id']
ck('07 coleção adiciona item',client.post(f'/api/account/collections/{r_id}/items',headers=CH,json={'productId':canva['id']}).status_code==200)
ck('08 coleção renomeia',client.patch(f'/api/account/collections/{r_id}',headers=CH,json={'name':'Clientes 2026'}).status_code==200)
cols=client.get('/api/account/collections').json()
ck('09 coleção persiste',cols['collections'][0]['name']=='Clientes 2026' and canva['id'] in cols['collections'][0]['productIds'],cols)
ck('10 coleção remove item',client.delete(f'/api/account/collections/{r_id}/items/{canva["id"]}',headers=CH).status_code==200)
ck('11 coleção exclui pasta',client.delete(f'/api/account/collections/{r_id}',headers=CH).status_code==200)

r=client.post('/api/account/cancel-subscription',headers=CH,json={})
ck('12 cancelar Premium volta Free',r.status_code==200 and not r.json().get('premiumActive'),r.text)
ck('13 Free não cria coleção',client.post('/api/account/collections',headers=CH,json={'name':'Bloqueada'}).status_code==403)
# primeiro colaborador no banco novo é Fundador, portanto seller não pode cancelar Premium vitalício
ck('14 Fundador protegido',seller.post('/api/account/cancel-subscription',headers=SH,json={}).status_code==409)

asset=make_product(seller,SH,'Venda 871')
assert admin.post(f"/api/admin/products/{asset['id']}/approve",headers=AH,json={}).status_code==200
settings=admin.get('/api/admin/settings').json();settings['priceRules']['allowIndividual']=True
assert admin.patch('/api/admin/settings',headers=AH,json=settings).status_code==200
assert admin.patch(f"/api/admin/products/{asset['id']}",headers=AH,json={'category':'JPG','accessTier':'PREMIUM','status':'PUBLISHED','individualPriceCents':500,'featured':False,'hidden':False}).status_code==200
buyer,ba,BH=reg('client','buyer871@test.local')
r=buyer.post('/api/checkout/simulate',headers=BH,json={'items':[asset['id']]})
ck('15 compra simulada',r.status_code==200,r.text)
notes=seller.get('/api/notifications').json();sale=next((x for x in notes['items'] if str(x['type']).startswith('ASSET_SALE:')),None)
ck('16 venda notifica Colaborador',sale is not None,notes)
ck('17 notificação venda abre Carteira',sale and sale['action']=='OPEN_WALLET',sale)

r=buyer.post('/api/downloads/consume',headers=BH,json={'productId':asset['id'],'source':'PURCHASE'});assert r.status_code==200,r.text
d=buyer.get(r.json()['downloadUrl'])
ck('18 JPG comprado é entregue',d.status_code==200 and len(d.content)>0,d.status_code)
notes=seller.get('/api/notifications').json()
ck('19 download real notifica Colaborador',any(str(x['type']).startswith('ASSET_DOWNLOAD:') for x in notes['items']),notes)

# regressão do download Premium PSD
psd=make_product(seller,SH,'PSD Premium 871','PSD','PREMIUM','arte.psd',PSD)
assert admin.post(f"/api/admin/products/{psd['id']}/approve",headers=AH,json={}).status_code==200
assert client.post('/api/account/simulate-plan',headers=CH,json={'plan':'PREMIUM','cycle':'MONTHLY'}).status_code==200
r=client.post('/api/downloads/consume',headers=CH,json={'productId':psd['id'],'source':'PREMIUM'})
ck('20 autoriza PSD Premium',r.status_code==200,r.text)
d=client.get(r.json()['downloadUrl'])
ck('21 entrega PSD Premium',d.status_code==200 and d.content.startswith(b'8BPS'),d.status_code)

r=admin.get(f"/api/admin/collaborators/{sa['id']}")
ck('22 Admin recebe contexto do Colaborador',r.status_code==200 and r.json()['email']=='seller871@test.local',r.text)
ticket=buyer.post('/api/support/tickets',json={'name':'Buyer','email':'buyer871@test.local','subject':'Chamado 871','message':'Ajuda'})
assert ticket.status_code in (200,201)
summary=admin.get('/api/admin/support/summary').json()
ck('23 Admin detecta chamado aberto',summary['open']>=1,summary)

rem=make_product(seller,SH,'Remover 871');assert admin.post(f"/api/admin/products/{rem['id']}/approve",headers=AH,json={}).status_code==200
rr=admin.delete(f"/api/admin/products/{rem['id']}",headers=AH)
ck('24 remoção Admin funciona',rr.status_code==200,rr.text)
seller_list=seller.get('/api/seller/products').json()
ck('25 removido some de Meus Recursos',all(x['id']!=rem['id'] for x in seller_list),seller_list)
notes=seller.get('/api/notifications').json()
ck('26 remoção gera incompatibilidade',any(x['type']=='ASSET_INCOMPATIBILITY' for x in notes['items']),notes)

src=Path(sys.argv[2]).read_text(encoding='utf-8')
def has(p):return re.search(p,src,re.S) is not None
ck('27 Admin Canva tem campo no modal',has(r'openAdminAddAssetModal\(.*?id="adminCanvaBoxV871".*?id="adminNewCanvaUrl"'))
ck('28 Admin Canva envia canvaUrl no POST',has(r"const adminCanvaUrl=.*?api\('/api/admin/products'.*?canvaUrl:adminCanvaUrl"))
ck('29 Admin Canva restringe upload a imagem',has(r"adminAssetFile.*?accept=canva\?'image/png,image/jpeg"))
ck('30 rota Notificações do Seller está ligada',"if (path === '#/seller/notificacoes') return sellerNotificationsV871();" in src)
ck('31 sidebar Seller abre Notificações', "['Notificações','#/seller/notificacoes']" in src and 'seller-notification-link' in src)
ck('32 ações de notificação têm Carteira/Vendas/chat',"'OPEN_WALLET'" in src and "'OPEN_SALES'" in src and "'OPEN_CHAT'" in src)
ck('33 cancelar em Minha Conta chama endpoint',has(r'cancelPremiumAccountBtn.*?onclick=.*?cancelPremiumSubscriptionV871'))
ck('34 cancelar em Planos usa mesma função',has(r'cancelPremiumBtn.*?onclick=.*?cancelPremiumSubscriptionV871'))
ck('35 Atendimento Admin faz polling real',has(r'ensureAdminSupportPollingV871\(\).*?setInterval\(pollAdminSupportV871,5000\)'))
ck('36 confirmação de remoção mostra Colaborador','Colaborador/origem: ${seller}' in src)
ck('37 Asset Admin tem botão explícito Ver colaborador','admin-collab-open-v871' in src and 'Ver colaborador' in src)
ck('38 cards têm ação de Coleção ligada', "document.querySelectorAll('[data-collection-card]').forEach" in src and 'openCollectionModal(b.dataset.collectionCard)' in src)
ck('39 chat final usa altura natural e wrap',has(r'\.atv-ticket-bubble-v85\{[^}]*max-width:72%!important;[^}]*height:auto!important;[^}]*max-height:none!important') and has(r'\.atv-ticket-text-v85\{[^}]*overflow-wrap:anywhere!important'))
ck('40 CTA usa imagem A da ATV','collab-mini-logo' in src and 'ARTIVA_A_LOGO' in src)
ck('41 Carteira compacta é renderizada','wallet-v87-balance' in src and 'wallet-v87-origins' in src)
ck('42 handshake frontend V8.7.1',"const ATV_BUILD='V8.7.1';" in src)
print(json.dumps({'PASS':True,'checks':42},ensure_ascii=False))
