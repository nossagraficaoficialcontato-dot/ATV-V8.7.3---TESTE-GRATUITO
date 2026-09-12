import os, sys, tempfile, importlib.util, base64, json
from pathlib import Path

os.environ['ATV_ENV']='local'
os.environ['ATV_COOKIE_SECURE']='0'
os.environ['ATV_ADMIN_PIN']='246810'
os.environ['ATV_ADMIN_KEY']='AD!!'
os.environ['ATV_AI_FAKE_SAFE']='1'
os.environ['ATV_DATA_DIR']=tempfile.mkdtemp(prefix='atv-v7-reg-')

app_path=Path(sys.argv[1]).resolve()
spec=importlib.util.spec_from_file_location('atvv7',app_path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from fastapi.testclient import TestClient

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=')
JPG=b'\xff\xd8\xff\xe0JFIF'+b'\x00'*80
PSD=b'8BPS'+b'\x00'*80
checks=[]
def check(name,cond,detail=''):
    if not cond: raise AssertionError(f'{name}: {detail}')
    checks.append(name);print('✓',name)

def admin_login(c):
    r=c.post('/api/admin/auth/step1',json={'pin':'246810'});assert r.status_code==200,r.text
    r=c.post('/api/admin/auth/step2',json={'challenge':r.json()['challenge'],'key':'AD!!'});assert r.status_code==200,r.text
    return {'X-Admin-CSRF-Token':r.json()['csrfToken']}

def register(email,role='client',first='Teste'):
    c=TestClient(m.app)
    r=c.post('/api/auth/register',json={'firstName':first,'lastName':'V7','email':email,'password':'Senha@123','role':role})
    assert r.status_code==200,r.text
    a=r.json()['account'];return c,a,{'X-CSRF-Token':a['csrfToken']}

def upload(c,h,name,data,preview=PNG):
    r=c.post('/api/uploads/resource',headers=h,files={'file':(name,data,'application/octet-stream'),'preview':('preview.png',preview,'image/png')},data={'metadata':'{}','previewMethod':'TEST'})
    assert r.status_code==200,r.text;return r.json()

def create(c,h,title,tier='FREE',category='PNG',name='arte.png',data=PNG):
    up=upload(c,h,name,data)
    r=c.post('/api/seller/products',headers=h,json={'title':title,'category':category,'accessTier':tier,'description':'Teste V7','tags':[],'uploadId':up['uploadId']})
    assert r.status_code==200,r.text;return r.json()

def approve(admin,ah,pid):
    r=admin.post(f'/api/admin/products/{pid}/approve',headers=ah,json={});assert r.status_code==200,r.text;return r.json()

def set_premium(c,h):
    r=c.post('/api/account/simulate-plan',headers=h,json={'plan':'PREMIUM','cycle':'MONTHLY'});assert r.status_code==200,r.text

def authorize_and_get(c,h,pid,source):
    r=c.post('/api/downloads/consume',headers=h,json={'productId':pid,'source':source});assert r.status_code==200,r.text
    g=c.get(r.json()['downloadUrl']);assert g.status_code==200,g.text[:100] if hasattr(g,'text') else ''
    return r,g

admin=TestClient(m.app);AH=admin_login(admin)
check('01 Admin 2 etapas permanece funcionando',True)

# Founder campaign + VIP profile + future paid entitlements
r=admin.patch('/api/admin/founders',headers=AH,json={'enabled':True,'limit':10,'autoClose':True});assert r.status_code==200,r.text
founder,facc,FH=register('fundador-v7@test.local','collaborator','Fundador')
check('02 Primeiro elegível recebe Premium vitalício',facc['premiumLifetime'] is True)
# política futura: Fundadores herdam recursos pagos superiores sem criar um plano PRO agora.
with m.db() as c:
    future_access=m.founder_feature_access(c,facc['id'],'PRO')
check('03 Fundador VIP está preparado para ferramenta futura PRO',future_access is True)
photo='data:image/jpeg;base64,'+base64.b64encode(b'profile-photo-v7').decode()
r=founder.patch('/api/account/profile',headers=FH,json={'firstName':'Fundador','lastName':'Designer','email':'fundador-v7@test.local','bio':'Perfil fundador','site':'https://example.com','instagram':'https://instagram.com/atvteste','youtube':'https://youtube.com/@atvteste','photo':photo,'profileComplete':True});assert r.status_code==200,r.text
pub=founder.get('/api/collaborators/'+facc['id']).json()
check('04 Perfil público usa foto real configurada',pub['photo']==photo)
check('05 Perfil público expõe links configurados',pub['site'].startswith('https://example.com') and 'instagram.com' in pub['instagram'] and 'youtube.com' in pub['youtube'])
check('06 Perfil público expõe Fundador VIP sem depender de slot público',pub['founder'] is True and 'founderSlot' not in pub)

# Highlight hybrid/Admin control + profile photo
r=admin.patch('/api/admin/settings',headers=AH,json={'creatorHighlights':{'mode':'HYBRID','manualUserIds':[facc['id']],'max':5}});assert r.status_code==200,r.text
high=admin.get('/api/highlighted-collaborators').json()
check('07 Admin consegue fixar Colaborador nos destaques',high and high[0]['id']==facc['id'])
check('08 Destaque usa foto real quando perfil possui foto',high[0]['photo']==photo)
admin_h=admin.get('/api/admin/highlighted-collaborators').json()
check('09 Admin recebe candidatos e configuração dos Destaques',admin_h['config']['mode']=='HYBRID' and any(x['id']==facc['id'] for x in admin_h['candidates']))

# Download valid event + same product same day dedupe + ratings
asset=create(founder,FH,'PNG Avaliável V7','FREE','PNG','avaliar.png',PNG);approve(admin,AH,asset['id'])
buyer,bacc,BH=register('buyer-v7@test.local','client','Buyer')
pre=buyer.get(f"/api/reviews/me/{asset['id']}").json()
check('10 Usuário sem download ainda não pode avaliar',pre['eligible'] is False)
r=buyer.post('/api/downloads/consume',headers=BH,json={'productId':asset['id'],'source':'FREE'});assert r.status_code==200,r.text
with m.db() as c: before=c.execute('SELECT COUNT(*) n FROM download_events WHERE user_id=? AND product_id=?',(bacc['id'],asset['id'])).fetchone()['n']
check('11 Só autorizar download não cria download_event',before==0)
g=buyer.get(r.json()['downloadUrl']);assert g.status_code==200
with m.db() as c:
    one=c.execute('SELECT COUNT(*) n FROM download_events WHERE user_id=? AND product_id=?',(bacc['id'],asset['id'])).fetchone()['n']
check('12 Entrega iniciada registra exatamente um download válido',one==1)
r2=buyer.post('/api/downloads/consume',headers=BH,json={'productId':asset['id'],'source':'FREE'});assert r2.status_code==200,r2.text
check('13 Segundo download do mesmo recurso no mesmo dia é reconhecido',r2.json()['alreadyCountedToday'] is True)
assert buyer.get(r2.json()['downloadUrl']).status_code==200
with m.db() as c:
    after=c.execute('SELECT COUNT(*) n FROM download_events WHERE user_id=? AND product_id=?',(bacc['id'],asset['id'])).fetchone()['n']
check('14 Re-download no mesmo dia não desconta nova cota/evento',after==1)
me=buyer.get(f"/api/reviews/me/{asset['id']}").json();check('15 Após download usuário fica elegível para avaliação',me['eligible'] is True)
r=buyer.post('/api/reviews',headers=BH,json={'productId':asset['id'],'rating':4});assert r.status_code==200,r.text
check('16 Avaliação 4 estrelas é salva',r.json()['rating']==4 and r.json()['count']==1 and r.json()['average']==4.0)
r=buyer.post('/api/reviews',headers=BH,json={'productId':asset['id'],'rating':5});assert r.status_code==200,r.text
check('17 Uma avaliação por usuário/recurso pode ser atualizada',r.json()['rating']==5 and r.json()['count']==1 and r.json()['average']==5.0)
public_asset=buyer.get('/api/products/'+asset['id']).json();check('18 Média de avaliação aparece no recurso público',public_asset['rating']==5.0 and public_asset['ratingCount']==1)

# Admin remove bug: bought product preserves history but disappears from active Admin Assets
premium_asset=create(founder,FH,'Asset comprado remover V7','PREMIUM','JPG','compra.jpg',JPG);approve(admin,AH,premium_asset['id'])
# ensure individual purchase allowed/default and buyer checkout
r=buyer.patch('/api/account/library',headers=BH,json={'cart':[premium_asset['id']]});assert r.status_code==200,r.text
r=buyer.post('/api/checkout/simulate',headers=BH,json={'items':[premium_asset['id']]});assert r.status_code==200,r.text
r=admin.delete('/api/admin/products/'+premium_asset['id'],headers=AH);assert r.status_code==200,r.text
check('19 Remover asset vendido retorna arquivamento seguro',r.json()['mode']=='ARCHIVED')
active_ids={x['id'] for x in admin.get('/api/admin/products').json()}
all_ids={x['id'] for x in admin.get('/api/admin/products?includeRemoved=1').json()}
check('20 Asset removido some da lista ativa do Admin',premium_asset['id'] not in active_ids)
check('21 Histórico comercial do asset removido continua preservado internamente',premium_asset['id'] in all_ids)
with m.db() as c:
    row=c.execute('SELECT admin_removed,status,hidden FROM products WHERE id=?',(premium_asset['id'],)).fetchone()
check('22 Asset comercial removido fica marcado admin_removed e fora da vitrine',row and row['admin_removed']==1 and row['status']=='SUSPENDED' and row['hidden']==1)

# Support chat end-to-end
visitor=TestClient(m.app)
r=visitor.post('/api/support/tickets',json={'name':'Pessoa Teste','email':'pessoa@test.local','subject':'Preciso de ajuda','message':'Mensagem inicial'});assert r.status_code==201,r.text
ticket=r.json();check('23 Chat de suporte cria protocolo e token privado',ticket['protocol'].startswith('SUP-') and bool(ticket['accessToken']))
listing=admin.get('/api/admin/support').json();check('24 Atendimento aparece no Admin',any(t['id']==ticket['protocol'] for t in listing))
r=admin.post(f"/api/admin/support/{ticket['protocol']}/reply",headers=AH,json={'message':'Resposta individual ATV'});assert r.status_code==200,r.text
thread=visitor.get(f"/api/support/tickets/{ticket['protocol']}/messages?token={ticket['accessToken']}").json()
check('25 Usuário recebe resposta individual do Admin',any(x['sender']=='ADMIN' and x['message']=='Resposta individual ATV' for x in thread['messages']))
r=visitor.post(f"/api/support/tickets/{ticket['protocol']}/messages?token={ticket['accessToken']}",json={'message':'Obrigado, recebi.'});assert r.status_code==200,r.text
check('26 Usuário pode continuar a conversa',admin.get(f"/api/admin/support/{ticket['protocol']}").json()['messages'][-1]['message']=='Obrigado, recebi.')

# AI: uploads 1-10 human, 11th auto when enabled and provider safe (test adapter)
r=admin.patch('/api/admin/settings',headers=AH,json={'aiModeration':{'enabled':True,'mode':'AUTO_AFTER_TRUST','startAfterUploads':10,'provider':'OPENAI','model':'omni-moderation-latest'}});assert r.status_code==200,r.text
aiuser,aacc,AIH=register('ai-v7@test.local','collaborator','AI')
ai_products=[]
for i in range(1,12):
    p=create(aiuser,AIH,f'AI upload {i}','FREE','PNG',f'ai{i}.png',PNG);ai_products.append(p)
check('27 Uploads 1 a 10 continuam em revisão humana',all(x['status']=='PENDING_REVIEW' and not x['autoApproved'] for x in ai_products[:10]))
check('28 11º upload entra na triagem automática',ai_products[10]['aiEligible'] is True and ai_products[10]['submissionNumber']==11)
check('29 11º upload seguro pode ser aprovado automaticamente',ai_products[10]['status']=='PUBLISHED' and ai_products[10]['autoApproved'] is True)
check('30 Resultado da IA fica registrado no recurso',ai_products[10]['aiModeration']['decision']=='AUTO_APPROVE')
# provider unavailable -> human fallback, never blind approval
m.AI_FAKE_SAFE=False;m.AI_API_KEY=''
p12=create(aiuser,AIH,'AI upload 12 fallback','FREE','PNG','ai12.png',PNG)
check('31 Sem provedor disponível o fluxo volta para revisão humana',p12['aiEligible'] is True and p12['status']=='PENDING_REVIEW' and not p12['autoApproved'])
check('32 Fallback de IA registra motivo sem auto-reprovar',p12['aiModeration']['decision']=='HUMAN_FALLBACK')

# Static UX checks
html=(app_path.parent/'frontend'/'index.html').read_text(encoding='utf-8')
check('33 UI mostra mensagem individual de Colaborador Fundador','Parabéns, você agora é um Colaborador Fundador!' in html)
check('34 UI pública usa apenas selo Fundador VIP sem numeração','Fundador VIP #' not in html and '👑 Fundador VIP' in html)
check('35 UI possui ícones clicáveis de Site/Instagram/YouTube','creator-social-icon' in html and 'title="Site"' in html and 'title="Instagram"' in html and 'title="YouTube"' in html)
check('36 PNG transparente preserva alpha na prévia e usa fundo quadriculado/contain','atv-alpha-preview-v85' in html and 'object-fit:contain!important' in html and "outputType==='image/png'?c.toDataURL('image/png')" in html)
check('37 Download abre avaliação de 5 estrelas após evento válido','O que achou do arquivo?' in html and '[1,2,3,4,5]' in html and 'syncWhenRecorded' in html)
check('38 Média de avaliação é exibida nos cards/detalhe','ratingText(p)' in html and 'resource-rating-summary' in html)
check('39 Rodapé mantém somente IG como rede social','footer-instagram-only' in html and 'Instagram da ATV' in html)
check('40 Destaques possuem controle híbrido no Admin','creatorHighlightMode' in html and 'Híbrido (recomendado)' in html)
check('41 Destaques usam foto do perfil quando disponível','c.photo?`<img src="${c.photo}"' in html)
check('42 Botão Seja um colaborador muda para perfil em conta Colaborador','collaboratorRegistered?' in html and 'Meu perfil de Colaborador →' in html)
check('43 Admin possui preview grande para moderação','moderation-large-image' in html and 'moderation-preview-btn' in html)
check('44 Admin possui controle explícito da IA após 10 uploads','aiModerationEnabled' in html and 'IA após os 10 primeiros uploads' in html)
check('45 Chat flutuante está disponível também na área do Colaborador',"if(document.body.classList.contains('admin-ui'))return" in html and 'setTimeout(ensureSupportWidget,0)' in html)
check('46 Admin possui caixa individual de Atendimentos','#/admin/atendimentos' in html and 'adminSupportConversation' in html)
check('47 Fundadores no Admin atualizam automaticamente','__founderAdminPoll' in html)
check('48 Footer IG continua controlado pela configuração Admin','state.config?.general?.instagram' in html)

print('\nV7 STABILIZATION REGRESSION PASSED')
print(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False))
