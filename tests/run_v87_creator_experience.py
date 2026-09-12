from pathlib import Path
import os,sys,tempfile,importlib.util,base64,json
os.environ['ATV_ENV']='local';os.environ['ATV_COOKIE_SECURE']='0';os.environ['ATV_ADMIN_PIN']='246810';os.environ['ATV_ADMIN_KEY']='AD!!';os.environ['ATV_DATA_DIR']=tempfile.mkdtemp(prefix='atv-v87-')
spec=importlib.util.spec_from_file_location('atvv87',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from fastapi.testclient import TestClient
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=')
JPG=bytes.fromhex('FFD8FFE000104A46494600010100000100010000FFD9')
def ck(n,v,d=None):
 print(('✓' if v else '✗'),n)
 if not v: raise AssertionError(f'{n}: {d}')

def register(role,email):
 c=TestClient(m.app);r=c.post('/api/auth/register',json={'firstName':'Teste','lastName':'V87','email':email,'password':'Senha@123','role':role});assert r.status_code==200,r.text;return c,r.json()['account'],{'X-CSRF-Token':r.json()['account']['csrfToken']}

pub=TestClient(m.app);h=pub.get('/api/health').json();ck('01 build V8.7',h['build']=='V8.7',h)
config=pub.get('/api/config').json();ck('02 CANVA em categorias','CANVA' in config['resourceTypes'],config['resourceTypes'])

seller,sa,SH=register('collaborator','seller87@test.local')
# imagem + preview para Canva
up=seller.post('/api/uploads/resource',headers=SH,files={'file':('capa.jpg',JPG,'image/jpeg'),'preview':('preview.png',PNG,'image/png')},data={'metadata':'{}','previewMethod':'CANVA_TEST'});assert up.status_code==200,up.text
r=seller.post('/api/seller/products',headers=SH,json={'title':'Template Canva V87','category':'CANVA','accessTier':'PREMIUM','description':'Template','tags':[],'uploadId':up.json()['uploadId'],'canvaUrl':'https://www.canva.com/design/TESTE-V87'});ck('03 cria Canva com link protegido',r.status_code==200,r.text);canva=r.json();ck('04 produto privado informa CANVA',canva['primaryFormat']=='CANVA' and canva['deliveryMode']=='CANVA',canva)
# link não vaza publicamente
admin=TestClient(m.app);r1=admin.post('/api/admin/auth/step1',json={'pin':'246810'});r2=admin.post('/api/admin/auth/step2',json={'challenge':r1.json()['challenge'],'key':'AD!!'});AH={'X-Admin-CSRF-Token':r2.json()['csrfToken']};assert admin.post(f"/api/admin/products/{canva['id']}/approve",headers=AH,json={}).status_code==200
pv=pub.get(f"/api/products/{canva['id']}").json();ck('05 link Canva não vaza no catálogo','canvaUrl' not in pv and 'external_url' not in pv,pv)

client,ca,CH=register('client','client87@test.local')
r=client.post(f"/api/products/{canva['id']}/external-access",headers=CH,json={});ck('06 Free sem direito não abre Canva',r.status_code==403,r.text)
# premium e coleções
r=client.post('/api/account/simulate-plan',headers=CH,json={'plan':'PREMIUM','cycle':'MONTHLY'});assert r.status_code==200
r=client.post(f"/api/products/{canva['id']}/external-access",headers=CH,json={});ck('07 Premium recebe link Canva',r.status_code==200 and r.json()['url'].startswith('https://www.canva.com/'),r.text)
co=client.post('/api/account/collections',headers=CH,json={'name':'Referências'});ck('08 Premium cria pasta',co.status_code==200,co.text);cid=co.json()['id']
ck('09 adiciona asset à coleção',client.post(f'/api/account/collections/{cid}/items',headers=CH,json={'productId':canva['id']}).status_code==200)
cols=client.get('/api/account/collections').json();ck('10 coleção persiste',cols['collections'][0]['count']==1,cols)
ck('11 renomeia coleção',client.patch(f'/api/account/collections/{cid}',headers=CH,json={'name':'Clientes'}).status_code==200)
# cancelamento
r=client.post('/api/account/cancel-subscription',headers=CH,json={});ck('12 cancelar Premium funciona em homologação',r.status_code==200 and not r.json()['premiumActive'],r.text)
r=client.post('/api/account/collections',headers=CH,json={'name':'Bloqueada'});ck('13 Free não cria coleção',r.status_code==403,r.text)

# compra gera notificação para colaborador
buyer,ba,BH=register('client','buyer87@test.local')
# produto premium avulso comum
up=seller.post('/api/uploads/resource',headers=SH,files={'file':('arte.jpg',JPG,'image/jpeg'),'preview':('p.png',PNG,'image/png')},data={'metadata':'{}','previewMethod':'TEST'});assert up.status_code==200
r=seller.post('/api/seller/products',headers=SH,json={'title':'Arte Venda V87','category':'JPG','accessTier':'PREMIUM','description':'x','tags':[],'uploadId':up.json()['uploadId']});p=r.json();admin.post(f"/api/admin/products/{p['id']}/approve",headers=AH,json={})
# ativar compra individual via setting legado já pode estar false; usa admin settings allowIndividual true
settings=admin.get('/api/admin/settings').json();settings['priceRules']['allowIndividual']=True;admin.patch('/api/admin/settings',headers=AH,json=settings)
admin.patch(f"/api/admin/products/{p['id']}",headers=AH,json={'category':'JPG','accessTier':'PREMIUM','status':'PUBLISHED','individualPriceCents':500,'featured':False,'hidden':False})
r=buyer.post('/api/checkout/simulate',headers=BH,json={'items':[p['id']]});ck('14 compra simulada aprovada',r.status_code==200,r.text)
notes=seller.get('/api/notifications').json();ck('15 venda notifica colaborador',any(str(x['type']).startswith('ASSET_SALE:') for x in notes['items']),notes)
# re-download não cria venda adicional
before=seller.get('/api/seller/summary').json()['sales'];buyer.post('/api/downloads/consume',headers=BH,json={'productId':p['id'],'source':'PURCHASE'});after=seller.get('/api/seller/summary').json()['sales'];ck('16 acesso não duplica venda',before==after,(before,after))

# Admin profile/support
r=admin.get(f"/api/admin/collaborators/{sa['id']}");ck('17 Admin acessa perfil do colaborador',r.status_code==200 and r.json()['email']=='seller87@test.local',r.text)
# ticket aberto
r=buyer.post('/api/support/tickets',json={'name':'Buyer','email':'buyer87@test.local','subject':'Teste V87','message':'Ajuda'});assert r.status_code in (200,201)
ss=admin.get('/api/admin/support/summary').json();ck('18 Admin recebe alerta de atendimento',ss['open']>=1,ss)

# Segurança/edição Canva adicionais
catalog=pub.get('/api/products').json();public_canva=next(x for x in catalog if x['id']==canva['id'])
ck('25 catálogo público não expõe URL Canva','canvaUrl' not in public_canva and 'external_url' not in public_canva,public_canva)
private=seller.get(f"/api/seller/products/{canva['id']}").json();ck('26 owner recebe URL Canva para editar',private.get('canvaUrl','').startswith('https://www.canva.com/'),private)
r=seller.patch(f"/api/seller/products/{canva['id']}",headers=SH,json={'canvaUrl':'https://www.canva.com/design/EDITADO-V87'});ck('27 editar somente link Canva é persistido',r.status_code==200,r.text)
private=seller.get(f"/api/seller/products/{canva['id']}").json();ck('28 link Canva editado persiste',private.get('canvaUrl','').endswith('EDITADO-V87'),private)
# Admin também consegue criar Canva sem NameError e o link não vaza publicamente.
upadm=admin.post('/api/admin/uploads/resource',headers=AH,files={'file':('admin-canva.jpg',JPG,'image/jpeg'),'preview':('admin-prev.png',PNG,'image/png')},data={'metadata':'{}','previewMethod':'TEST'})
ck('29 upload Admin Canva',upadm.status_code==200,upadm.text)
r=admin.post('/api/admin/products',headers=AH,json={'title':'Canva Admin V87','category':'CANVA','accessTier':'PREMIUM','uploadId':upadm.json()['uploadId'],'canvaUrl':'https://www.canva.com/design/ADMIN-V87'})
ck('30 Admin cria Canva',r.status_code==201,r.text)

html=Path(sys.argv[2]).read_text(encoding='utf-8')
for name,needle in [
('19 A real no CTA','collab-mini-logo'),('20 carteira compacta','wallet-v87-balance'),('21 chat compacto','max-width:78%'),('22 coleções Premium','collection-modal-v87'),('23 cadastro atualizado','Cadastrar como Cliente'),('24 botão cancelar','cancelPremiumBtn'),('31 admin perfil asset','data-admin-collab'),('32 admin atendimento badge','admin-support-badge-v87'),('33 Canva UI','newCanvaUrl')]:ck(name,needle in html)
print(json.dumps({'PASS':True,'checks':33},ensure_ascii=False))
