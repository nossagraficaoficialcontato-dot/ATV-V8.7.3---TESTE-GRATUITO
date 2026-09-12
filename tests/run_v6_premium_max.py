import os, sys, tempfile, importlib.util, base64, io, zipfile, json
from pathlib import Path

os.environ['ATV_ENV']='local'
os.environ['ATV_COOKIE_SECURE']='0'
os.environ['ATV_ADMIN_PIN']='246810'
os.environ['ATV_ADMIN_KEY']='AD!!'
os.environ['ATV_DATA_DIR']=tempfile.mkdtemp(prefix='atv-v6-reg-')

app_path=Path(sys.argv[1]).resolve()
spec=importlib.util.spec_from_file_location('atvv6', app_path)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from fastapi.testclient import TestClient

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=')
PSD=b'8BPS'+b'\x00'*64
JPG=b'\xff\xd8\xff\xe0'+b'JFIF'+b'\x00'*64

checks=[]
def check(name, cond, detail=''):
    if not cond: raise AssertionError(f'{name}: {detail}')
    checks.append(name); print(f'✓ {name}')

def admin_login(c):
    r=c.post('/api/admin/auth/step1',json={'pin':'246810'}); assert r.status_code==200,r.text
    r=c.post('/api/admin/auth/step2',json={'challenge':r.json()['challenge'],'key':'AD!!'}); assert r.status_code==200,r.text
    return {'X-Admin-CSRF-Token':r.json()['csrfToken']}

def register(email, role='client', first='Teste'):
    c=TestClient(m.app)
    r=c.post('/api/auth/register',json={'firstName':first,'lastName':'V6','email':email,'password':'Senha@123','role':role})
    assert r.status_code==200,r.text
    acc=r.json()['account']; return c,acc,{'X-CSRF-Token':acc['csrfToken']}

def upload(c,h,filename,data,preview=PNG):
    r=c.post('/api/uploads/resource',headers=h,files={'file':(filename,data,'application/octet-stream'),'preview':('preview.png',preview,'image/png')},data={'metadata':'{}','previewMethod':'TEST'})
    assert r.status_code==200,r.text
    return r.json()

def add_parts(c,h,upload_id, parts):
    files=[('files',(name,data,'application/octet-stream')) for name,data in parts]
    r=c.post(f'/api/uploads/{upload_id}/files',headers=h,files=files)
    assert r.status_code==200,r.text
    return r.json()

def create_seller(c,h,title,tier,filename,data,category='OUTROS',parts=None):
    up=upload(c,h,filename,data)
    if parts: add_parts(c,h,up['uploadId'],parts)
    r=c.post('/api/seller/products',headers=h,json={'title':title,'category':category,'accessTier':tier,'description':'Recurso teste V6','tags':[],'uploadId':up['uploadId']})
    assert r.status_code==200,r.text
    return r.json()

def approve(admin,ah,pid):
    r=admin.post(f'/api/admin/products/{pid}/approve',headers=ah,json={}); assert r.status_code==200,r.text; return r.json()

def set_premium(c,h):
    r=c.post('/api/account/simulate-plan',headers=h,json={'plan':'PREMIUM','cycle':'MONTHLY'}); assert r.status_code==200,r.text
    return r.json()

def consume(c,h,pid,source):
    return c.post('/api/downloads/consume',headers=h,json={'productId':pid,'source':source})

admin=TestClient(m.app); AH=admin_login(admin)
# config defaults and public propagation
cfg=admin.get('/api/admin/settings').json()
check('01 FREE possui limite de 5 downloads/dia', cfg['freeDownloadsPerDay']==5, cfg['freeDownloadsPerDay'])
check('02 PREMIUM possui limite de 10 downloads/dia', cfg['premiumDownloadsPerDay']==10, cfg['premiumDownloadsPerDay'])
check('03 preços Premium oficiais preservados', (cfg['premiumMonthlyCents'],cfg['premiumAnnualCents'],cfg['premiumAnnualInstallments'],cfg['premiumAnnualInstallmentCents'])==(3490,30000,12,2500))

# Seller + formats independent of access/category
seller,sacc,SH=register('seller-v6@test.local','collaborator','Designer')
free_psd=create_seller(seller,SH,'PSD Free V6','FREE','free.psd',PSD,'PSD')
prem_psd=create_seller(seller,SH,'PSD Premium V6','PREMIUM','premium.psd',PSD,'PSD')
jpg_product=create_seller(seller,SH,'JPG Categoria V6','PREMIUM','foto.jpg',JPG,'JPG')
max_product=create_seller(seller,SH,'Kit Premium Max V6','PREMIUM_MAX','principal.psd',PSD,'MOCKUPS',parts=[('arte-final.png',PNG),('mockup.jpg',JPG),('elemento.psd',PSD)])
check('04 Colaborador consegue criar FREE', free_psd['accessTier']=='FREE' and free_psd['status']=='PENDING_REVIEW')
check('05 Colaborador consegue criar PREMIUM', prem_psd['accessTier']=='PREMIUM' and prem_psd['status']=='PENDING_REVIEW')
check('06 Colaborador consegue criar PREMIUM_MAX', max_product['accessTier']=='PREMIUM_MAX' and max_product['status']=='PENDING_REVIEW')
check('07 PSD pode existir em recurso FREE', free_psd['formats']==['PSD'])
check('08 PSD pode existir em recurso PREMIUM', prem_psd['formats']==['PSD'])
check('09 JPG original continua JPG mesmo com preview PNG', jpg_product['formats']==['JPG'], jpg_product['formats'])
check('10 PREMIUM_MAX aceita múltiplos arquivos', max_product.get('packageFileCount')==4, max_product.get('files'))
with m.db() as c:
    table_exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_files'").fetchone()
check('11 estrutura product_files criada automaticamente', bool(table_exists))
with m.db() as c:
    linked=c.execute('SELECT product_id,COUNT(*) n FROM product_files WHERE product_id=? GROUP BY product_id',(max_product['id'],)).fetchone()
check('12 product_files contém todas as partes do mesmo recurso', linked and linked['product_id']==max_product['id'] and linked['n']==4, dict(linked) if linked else '')

# Admin sees/approve/reject Max; standard approvals
admin_list=admin.get('/api/admin/products').json(); max_admin=next(x for x in admin_list if x['id']==max_product['id'])
check('13 Admin consegue visualizar PREMIUM_MAX e seus arquivos', max_admin['accessTier']=='PREMIUM_MAX' and max_admin['packageFileCount']==4)
approved_max=approve(admin,AH,max_product['id'])
check('14 Admin consegue aprovar PREMIUM_MAX', approved_max['status']=='PUBLISHED' and not approved_max['hidden'])
# reject another max
max_reject=create_seller(seller,SH,'Max Rejeitar V6','PREMIUM_MAX','base.png',PNG,'PNG',parts=[('extra.jpg',JPG)])
r=admin.post(f"/api/admin/products/{max_reject['id']}/action",headers=AH,json={'action':'REJECT','reason':'Teste de reprovação'}); assert r.status_code==200,r.text
check('15 Admin consegue reprovar PREMIUM_MAX', r.json()['status']=='REJECTED')
# approve standard
for prod in (free_psd,prem_psd,jpg_product): approve(admin,AH,prod['id'])
public_ids={x['id'] for x in admin.get('/api/products').json()}
check('16 recurso aprovado fica publicado normalmente', {free_psd['id'],prem_psd['id'],max_product['id']}.issubset(public_ids))

# category edit independent from format
r=admin.patch(f"/api/admin/products/{jpg_product['id']}",headers=AH,json={'category':'PNG','accessTier':'PREMIUM','status':'PUBLISHED','individualPriceCents':500,'featured':False,'hidden':False}); assert r.status_code==200,r.text
edited=r.json(); public_jpg=admin.get(f"/api/products/{jpg_product['id']}").json()
check('17 Admin altera categoria e mudança persiste publicamente', edited['category']=='PNG' and public_jpg['category']=='PNG')
check('18 alterar categoria não altera formato real do arquivo', public_jpg['formats']==['JPG'], public_jpg['formats'])

# Free access and 5/day quota — V7 conta somente quando o arquivo começa a ser entregue
free,free_acc,FH=register('free-v6@test.local','client','Free')
check('19 FREE consegue visualizar Premium', free.get(f"/api/products/{prem_psd['id']}").status_code==200)
check('20 FREE não baixa Premium gratuitamente', consume(free,FH,prem_psd['id'],'FREE').status_code==403)
check('21 FREE não baixa PREMIUM_MAX gratuitamente', consume(free,FH,max_product['id'],'FREE').status_code==403)
free_quota=[free_psd]
for i in range(4):
    q=create_seller(seller,SH,f'Free quota {i} V7','FREE',f'fq{i}.png',PNG,'OUTROS');approve(admin,AH,q['id']);free_quota.append(q)
for i,q in enumerate(free_quota):
    rr=consume(free,FH,q['id'],'FREE'); assert rr.status_code==200,(i,rr.text)
    dl=free.get(rr.json()['downloadUrl']); assert dl.status_code==200,(i,dl.text[:100] if hasattr(dl,'text') else '')
check('22 FREE consegue baixar recurso FREE', True)
q6=create_seller(seller,SH,'Free quota sexto V7','FREE','fq6.png',PNG,'OUTROS');approve(admin,AH,q6['id'])
check('23 FREE bloqueia o 6º download', consume(free,FH,q6['id'],'FREE').status_code==429)

# Premium quota and legacy single-file premium
premium,pacc,PH=register('premium-v6@test.local','client','Premium'); set_premium(premium,PH)
# FREE continua disponível para Premium e agora consome a cota Premium do assinante.
rr=consume(premium,PH,free_psd['id'],'PREMIUM'); assert rr.status_code==200; assert premium.get(rr.json()['downloadUrl']).status_code==200
check('24 PREMIUM consegue baixar FREE', True)
premium_quota=[prem_psd]
for i in range(8):
    q=create_seller(seller,SH,f'Premium quota {i} V7','PREMIUM',f'pq{i}.png',PNG,'OUTROS');approve(admin,AH,q['id']);premium_quota.append(q)
for i,q in enumerate(premium_quota):
    rr=consume(premium,PH,q['id'],'PREMIUM'); assert rr.status_code==200,(i,rr.text)
    dl=premium.get(rr.json()['downloadUrl']); assert dl.status_code==200,(i,dl.text[:100] if hasattr(dl,'text') else '')
check('25 PREMIUM consegue baixar PREMIUM', True)
p11=create_seller(seller,SH,'Premium quota onze V7','PREMIUM','pq11.png',PNG,'OUTROS');approve(admin,AH,p11['id'])
check('26 PREMIUM bloqueia o 11º download', consume(premium,PH,p11['id'],'PREMIUM').status_code==429)
# verify normal premium token returns original, not zip using fresh premium account
legacy,lacc,LH=register('legacy-v6@test.local','client','Legacy');set_premium(legacy,LH)
rr=consume(legacy,LH,prem_psd['id'],'PREMIUM'); assert rr.status_code==200,rr.text
file_resp=legacy.get(rr.json()['downloadUrl'])
check('27 recurso Premium antigo continua fluxo de arquivo único', file_resp.status_code==200 and file_resp.headers.get('content-type')!='application/zip')

# Max download one quota/event and zip all files
maxuser,macc,MH=register('maxuser-v6@test.local','client','Max');set_premium(maxuser,MH)
with m.db() as c:
    before=c.execute("SELECT COUNT(*) n FROM download_events WHERE user_id=? AND product_id=?",(macc['id'],max_product['id'])).fetchone()['n']
rr=consume(maxuser,MH,max_product['id'],'PREMIUM'); assert rr.status_code==200,rr.text
with m.db() as c:
    authorized_only=c.execute("SELECT COUNT(*) n FROM download_events WHERE user_id=? AND product_id=?",(macc['id'],max_product['id'])).fetchone()['n']
check('28 autorização de PREMIUM_MAX não consome antes da entrega', authorized_only==before)
z=maxuser.get(rr.json()['downloadUrl']); assert z.status_code==200,z.text[:100] if hasattr(z,'text') else ''
with m.db() as c:
    after=c.execute("SELECT COUNT(*) n FROM download_events WHERE user_id=? AND product_id=?",(macc['id'],max_product['id'])).fetchone()['n']
check('29 PREMIUM consegue acessar PREMIUM_MAX e consome somente 1 evento/cota', after-before==1)
with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
    names=zf.namelist()
check('30 download PREMIUM_MAX disponibiliza todos os arquivos em ZIP', z.headers.get('content-type')=='application/zip' and len(names)==4 and 'principal.psd' in names, names)

# Compensation prepared and owner attribution
# Configure Max 75 to prove editable; Premium remains 60
r=admin.patch('/api/admin/settings',headers=AH,json={'premiumCreatorCompensation':{'premiumCents':60,'premiumMaxCents':75,'premiumMaxMinCents':70,'premiumMaxMaxCents':80,'model':'PER_VALID_DOWNLOAD_PREPARED','paymentsEnabled':False}});assert r.status_code==200,r.text
# new downloads after config
compuser,cacc,CH=register('comp-v6@test.local','client','Comp');set_premium(compuser,CH)
r1=consume(compuser,CH,prem_psd['id'],'PREMIUM');assert r1.status_code==200;r2=consume(compuser,CH,max_product['id'],'PREMIUM');assert r2.status_code==200
assert compuser.get(r1.json()['downloadUrl']).status_code==200
assert compuser.get(r2.json()['downloadUrl']).status_code==200
with m.db() as c:
    latest={
      prem_psd['id']:c.execute("SELECT * FROM download_compensation_records WHERE product_id=? ORDER BY rowid DESC LIMIT 1",(prem_psd['id'],)).fetchone(),
      max_product['id']:c.execute("SELECT * FROM download_compensation_records WHERE product_id=? ORDER BY rowid DESC LIMIT 1",(max_product['id'],)).fetchone(),
    }
    wallet=c.execute('SELECT * FROM wallets WHERE user_id=?',(sacc['id'],)).fetchone()
check('31 download válido fica vinculado ao colaborador proprietário correto', latest[prem_psd['id']]['owner_id']==sacc['id'] and latest[max_product['id']]['owner_id']==sacc['id'])
check('32 Premium usa remuneração configurada R$ 0,60', latest[prem_psd['id']]['compensation_cents']==60)
check('33 Premium Max usa remuneração configurável', latest[max_product['id']]['compensation_cents']==75)
check('34 nenhum pagamento real é executado', latest[prem_psd['id']]['status']=='PREPARED' and latest[max_product['id']]['status']=='PREPARED' and admin.get('/api/admin/settings').json()['premiumCreatorCompensation']['paymentsEnabled'] is False)

# Founder/profile photo/site + public API (prior requested bugs)
r=admin.patch('/api/admin/founders',headers=AH,json={'enabled':True,'limit':10,'autoClose':True}); assert r.status_code==200,r.text
founder,facc,FOH=register('founder-v6@test.local','collaborator','Fundador')
photo='data:image/jpeg;base64,'+base64.b64encode(b'fake-photo').decode()
r=founder.patch('/api/account/profile',headers=FOH,json={'firstName':'Fundador','lastName':'VIP','email':'founder-v6@test.local','bio':'Designer fundador','site':'https://example.com','photo':photo,'profileComplete':True});assert r.status_code==200,r.text
pub=founder.get('/api/collaborators/'+facc['id']).json()
check('35 fundador recebe identificação vitalícia na conta', facc['premiumLifetime'] is True and facc['founderSlot']==1)
check('36 perfil público entrega foto/site reais e selo Fundador', pub['photo']==photo and pub['site']=='https://example.com' and pub['founder'] is True)

# Public plan configuration and showDaily control
r=admin.patch('/api/admin/settings',headers=AH,json={'freeDownloadsPerDay':5,'premiumDownloadsPerDay':10,'planContent':{'FREE':{'description':'Free teste','benefits':['Benefício Free'],'showDailyDownloads':False},'PREMIUM':{'description':'Premium teste','benefits':['Benefício Premium','✏️ Editor ATV — Em breve'],'showDailyDownloads':True}}});assert r.status_code==200,r.text
pcfg=admin.get('/api/config').json()
check('37 alterações Admin em planos refletem na configuração pública', pcfg['free']['downloadsPerDay']==5 and pcfg['premium']['downloadsPerDay']==10 and pcfg['planContent']['PREMIUM']['description']=='Premium teste')
check('38 controle de exibição de downloads diários persiste por plano', pcfg['planContent']['FREE']['showDailyDownloads'] is False and pcfg['planContent']['PREMIUM']['showDailyDownloads'] is True)

# static UI requirements
html=(app_path.parent/'frontend'/'index.html').read_text(encoding='utf-8')
check('39 UI possui Premium Max em Colaborador/Admin sem criar novo plano', 'PREMIUM_MAX' in html and 'Premium Max (pacote com vários arquivos)' in html)
check('40 UI possui preview grande de moderação', 'moderation-large-image' in html and 'moderation-preview-btn' in html)
check('41 UI possui mensagem Parabéns Colaborador Fundador', 'Parabéns, você agora é um Colaborador Fundador!' in html)
check('42 UI possui atualização automática Fundadores no Admin', '__founderAdminPoll' in html)
check('43 UI usa valores R$ com máscara no Admin', 'bindMoneyMask' in html and 'Premium mensal (R$)' in html)
check('44 UI permite categoria e tier autosalvos no Admin Assets', "querySelectorAll('.assetCategory,.assetTier')" in html)
check('45 UI esconde CTA Seja um colaborador para quem já é colaborador', 'collaboratorRegistered?' in html and 'Meu perfil de Colaborador' in html)
check('46 Editor ATV continua somente Em breve', '✏️ Editor ATV — Em breve' in html and '#/editor' not in html)

print('\nV6 SURGICAL REGRESSION PASSED')
print(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False))
