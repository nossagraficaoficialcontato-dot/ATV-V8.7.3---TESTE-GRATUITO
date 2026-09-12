import io, json, os, time, uuid
import requests
import base64

BASE=os.getenv('ATV_BASE_URL','http://127.0.0.1:8000')

def check(cond,msg):
    if not cond: raise AssertionError(msg)
    print('✓',msg)

def png_bytes(color=None):
    # PNG 1x1 válido; a cor não é relevante para o teste de storage/magic bytes.
    return base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')

def register(role,email):
    s=requests.Session();r=s.post(BASE+'/api/auth/register',json={'firstName':'Homolog','lastName':role.title(),'email':email,'password':'SenhaForte123','role':role});check(r.status_code==200,f'registro real {role}');data=r.json();return s,data['account']['csrfToken']

# Public
r=requests.get(BASE+'/api/health');check(r.status_code==200 and r.json()['ok'],'health backend')
r=requests.get(BASE+'/api/products');check(r.status_code==200 and len(r.json())>=6,'vitrine pública sem login')
check(requests.get(BASE+'/api/account/library').status_code==401,'biblioteca protegida sem login')

# Client real session
email=f'cliente-{uuid.uuid4().hex[:6]}@example.com'; client,csrf=register('client',email)
r=client.get(BASE+'/api/account');check(r.json()['id'] and r.json()['email']==email,'sessão persistida por cookie HttpOnly')
# csrf enforcement
check(client.patch(BASE+'/api/account/library',json={'favorites':['p1']}).status_code==403,'CSRF bloqueia escrita sem token')
h={'X-CSRF-Token':csrf}
r=client.patch(BASE+'/api/account/library',json={'favorites':['p1'],'collection':['p2'],'following':['Studio Criativo'],'cart':['p1']},headers=h);check(r.status_code==200 and r.json()['favorites']==['p1'],'favoritos/coleção/carrinho centralizados')
# new session same user sees same library
client2=requests.Session();r=client2.post(BASE+'/api/auth/login',json={'email':email,'password':'SenhaForte123'});check(r.status_code==200,'login real em segunda sessão');csrf2=r.json()['account']['csrfToken'];lib=client2.get(BASE+'/api/account/library').json();check('p1' in lib['favorites'] and 'p1' in lib['cart'],'dados de biblioteca aparecem em outra sessão/dispositivo')
# Checkout homologation + entitlement
r=client2.post(BASE+'/api/checkout/simulate',json={'items':['p1']},headers={'X-CSRF-Token':csrf2});check(r.status_code==200 and r.json()['order']['status']=='APPROVED_TEST','checkout de homologação server-side')
check(client2.post(BASE+'/api/checkout/simulate',json={'items':['p1']},headers={'X-CSRF-Token':csrf2}).status_code==400,'compra duplicada bloqueada')
# Purchased download token + actual file
r=client2.post(BASE+'/api/downloads/consume',json={'productId':'p1','source':'PURCHASE'},headers={'X-CSRF-Token':csrf2});check(r.status_code==200 and r.json().get('downloadUrl'),'download autorizado gera link temporário')
durl=r.json()['downloadUrl'];dr=client2.get(BASE+durl);check(dr.status_code==200 and dr.content.startswith(b'8BPS'),'arquivo privado baixado após autorização')
check(client2.get(BASE+durl).status_code==200,'link temporário autenticado aceita retry/range do navegador')

# Plan simulation server-side
r=client2.post(BASE+'/api/account/simulate-plan',json={'plan':'PREMIUM','cycle':'MONTHLY'},headers={'X-CSRF-Token':csrf2});check(r.status_code==200 and r.json()['premiumActive'],'Premium de homologação ativado no servidor')
# Same account can gain collaborator role without duplicate user
r=client2.post(BASE+'/api/account/activate-collaborator',json={},headers={'X-CSRF-Token':csrf2});check(r.status_code==200 and 'client' in r.json()['roles'] and 'collaborator' in r.json()['roles'],'mesma conta ativa Cliente + Colaborador')

# Collaborator upload and publication
collab_email=f'colab-{uuid.uuid4().hex[:6]}@example.com'; col,ccsrf=register('collaborator',collab_email)
img=png_bytes();files={'file':('arte-real.png',img,'image/png'),'preview':('preview.png',img,'image/png')};data={'metadata':json.dumps({'test':True}),'previewMethod':'SMOKE'}
r=col.post(BASE+'/api/uploads/resource',files=files,data=data,headers={'X-CSRF-Token':ccsrf});check(r.status_code==200 and r.json().get('uploadId'),'upload original real em storage privado');upload_id=r.json()['uploadId']
r=col.post(BASE+'/api/seller/products',json={'title':'Asset Homologação '+uuid.uuid4().hex[:4],'category':'PNG','accessTier':'PREMIUM','description':'Asset criado no teste automatizado','uploadId':upload_id},headers={'X-CSRF-Token':ccsrf});check(r.status_code==200 and r.json()['id'],'colaborador cria asset na base central');seller_product=r.json();check(seller_product['status']=='PENDING_REVIEW','asset do colaborador entra na fila de revisão');check(not any(p['id']==seller_product['id'] for p in requests.get(BASE+'/api/products').json()),'asset pendente ainda não aparece na vitrine pública')

# Admin 2-step real, credentials not needed in frontend
admin=requests.Session();check(admin.get(BASE+'/api/admin/summary').status_code==401,'Admin protegido sem sessão')
check(admin.post(BASE+'/api/admin/auth/step1',json={'pin':'000000'}).status_code==401,'PIN incorreto rejeitado')
r=admin.post(BASE+'/api/admin/auth/step1',json={'pin':'246810'});check(r.status_code==200 and r.json().get('challenge'),'Admin etapa 1 validada');challenge=r.json()['challenge']
check(admin.post(BASE+'/api/admin/auth/step2',json={'challenge':challenge,'key':'AAAA'}).status_code==400,'formato da etapa 2 validado')
# Need new challenge because prior format error does not consume challenge; same challenge works
r=admin.post(BASE+'/api/admin/auth/step2',json={'challenge':challenge,'key':'AD!!'});check(r.status_code==200 and r.json().get('csrfToken'),'Admin etapa 2 validada');acsrf=r.json()['csrfToken']
check(admin.get(BASE+'/api/admin/summary').status_code==200,'Painel Admin liberado após 2 etapas')
# Human approval publishes collaborator asset
r=admin.post(BASE+f"/api/admin/products/{seller_product['id']}/action",json={'action':'APPROVE','reason':'Aprovado na homologação automatizada'},headers={'X-Admin-CSRF-Token':acsrf});check(r.status_code==200 and r.json()['status']=='PUBLISHED','Admin aprova asset pendente')
check(any(p['id']==seller_product['id'] for p in requests.get(BASE+'/api/products').json()),'asset aprovado passa a aparecer na vitrine pública')
check(admin.patch(BASE+'/api/admin/settings',json={'content':{'heroTitle':'TESTE SEM CSRF'}}).status_code==403,'Admin CSRF bloqueia alteração sem token')
# Settings change reflects public config
r=admin.patch(BASE+'/api/admin/settings',json={'content':{'heroTitle':'HOMOLOGAÇÃO ATV ATIVA'}},headers={'X-Admin-CSRF-Token':acsrf});check(r.status_code==200,'Admin altera configuração central')
check(requests.get(BASE+'/api/config').json()['content']['heroTitle']=='HOMOLOGAÇÃO ATV ATIVA','alteração Admin reflete na API pública')
# Admin add real asset
img2=png_bytes((30,180,100));files={'file':('admin-real.png',img2,'image/png'),'preview':('preview.png',img2,'image/png')};r=admin.post(BASE+'/api/admin/uploads/resource',files=files,data={'metadata':'{}','previewMethod':'ADMIN_SMOKE'},headers={'X-Admin-CSRF-Token':acsrf});check(r.status_code==200,'Admin envia arquivo real');aid=r.json()['uploadId']
r=admin.post(BASE+'/api/admin/products',json={'title':'Admin Asset '+uuid.uuid4().hex[:4],'seller':'ATV Admin','category':'PNG','accessTier':'PREMIUM','status':'PUBLISHED','individualPriceCents':500,'description':'Teste Admin','tags':['homologacao'],'uploadId':aid,'featured':True},headers={'X-Admin-CSRF-Token':acsrf});check(r.status_code==201,'Admin adiciona asset real');admin_product=r.json();check(any(p['id']==admin_product['id'] for p in requests.get(BASE+'/api/products').json()),'asset adicionado no Admin aparece publicamente')
# Remove no-sale admin product
r=admin.delete(BASE+'/api/admin/products/'+admin_product['id'],headers={'X-Admin-CSRF-Token':acsrf});check(r.status_code==200 and r.json()['mode']=='DELETED','Admin remove asset sem histórico definitivamente')
check(not any(p['id']==admin_product['id'] for p in requests.get(BASE+'/api/products').json()),'asset removido desaparece da vitrine')
# Remove sold p1 must archive, not erase history
r=admin.delete(BASE+'/api/admin/products/p1',headers={'X-Admin-CSRF-Token':acsrf});check(r.status_code==200 and r.json()['mode']=='ARCHIVED','asset com venda é arquivado, preservando histórico')
check(not any(p['id']=='p1' for p in requests.get(BASE+'/api/products').json()),'asset arquivado sai da vitrine')
# Audit log
logs=admin.get(BASE+'/api/admin/audit').json();check(any(x['action']=='ADMIN_LOGIN_2FA_SUCCESS' for x in logs),'login admin registrado em auditoria');check(any(x['action'] in ('ASSET_DELETED_BY_ADMIN','ASSET_REMOVED_FROM_STOREFRONT') for x in logs),'remoção de asset registrada no log')

# Block client invalidates live session
clients=admin.get(BASE+'/api/admin/clients').json();u=next(x for x in clients if x['email']==email)
r=admin.patch(BASE+f"/api/admin/clients/{u['id']}/status",json={'status':'BLOCKED'},headers={'X-Admin-CSRF-Token':acsrf});check(r.status_code==200,'Admin bloqueia cliente')
check(client2.get(BASE+'/api/account').json()['id'] is None,'sessão de cliente bloqueado deixa de autenticar')

print('\nALL HOMOLOGATION SMOKE TESTS PASSED')
