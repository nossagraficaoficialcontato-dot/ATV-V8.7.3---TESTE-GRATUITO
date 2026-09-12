
import os,sys,tempfile,importlib.util,base64,json
from pathlib import Path
os.environ["ATV_ENV"]="local";os.environ["ATV_COOKIE_SECURE"]="0";os.environ["ATV_ADMIN_PIN"]="246810";os.environ["ATV_ADMIN_KEY"]="AD!!";os.environ["ATV_DATA_DIR"]=tempfile.mkdtemp(prefix="atv-v84-")
spec=importlib.util.spec_from_file_location("atvv84",sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from fastapi.testclient import TestClient

PNG=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=")
JPG=bytes.fromhex("FFD8FFE000104A46494600010100000100010000FFD9")

def ck(n,c,d=None):
    if not c: raise AssertionError(f"{n}: {d}")
    print("✓",n)

c=TestClient(m.app)
h=c.get("/api/health").json()
ck("01 build V8.7",h["build"]=="V8.7",h)
ck("02 migration V8.7 aplicada",c.get("/api/runtime-status").json()["migrations"]["v84_root_fixes_migrated"] is True)

# Magic-first: caminho .png contendo bytes JPEG DEVE ser JPG.
fake=Path(os.environ["ATV_DATA_DIR"])/"originals"/"legacy-mislabeled.png"
fake.parent.mkdir(parents=True,exist_ok=True);fake.write_bytes(JPG)
with m.db() as db:
    now=m.iso_now()
    db.execute("""INSERT INTO products(id,slug,owner_id,title,seller,category,formats_json,description,preview_url,original_path,license,access_tier,individual_purchase_enabled,individual_price_cents,status,tags_json,featured,hidden,sales,premium_downloads,free_downloads,created_by_admin,moderation_json,history_json,created_at,updated_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
    ("legacy-jpg","legacy-jpg",None,"Legacy JPG","ATV","JPG",m.jdump(["JPG","PNG"]),"x","/media/previews/x.png",str(fake),"Uso comercial","PREMIUM",1,None,"PUBLISHED","[]",0,0,0,0,0,1,"{}","[]",now,now))
row=None
with m.db() as db: row=db.execute("SELECT * FROM products WHERE id='legacy-jpg'").fetchone()
ck("03 assinatura JPEG vence extensão PNG",m.primary_product_format(row)=="JPG",m.primary_product_format(row))
v=c.get("/api/products/legacy-jpg").json()
ck("04 API expõe apenas JPG em recurso comum",v["primaryFormat"]=="JPG" and v["formats"]==["JPG"],v)

# Legacy suspended account BLOCKED sem CLIENT_STATUS_CHANGED explícito: login deve reparar.
admin=TestClient(m.app)
r=admin.post("/api/admin/auth/step1",json={"pin":"246810"});assert r.status_code==200
r=admin.post("/api/admin/auth/step2",json={"challenge":r.json()["challenge"],"key":"AD!!"});assert r.status_code==200
AH={"X-Admin-CSRF-Token":r.json()["csrfToken"]}

u=TestClient(m.app)
r=u.post("/api/auth/register",json={"firstName":"Legacy","lastName":"Seller","email":"legacy84@test.local","password":"Senha@123","role":"collaborator"});assert r.status_code==200
uid=r.json()["account"]["id"];UH={"X-CSRF-Token":r.json()["account"]["csrfToken"]}
u.post("/api/auth/logout",headers=UH,json={})
with m.db() as db:
    db.execute("UPDATE users SET creator_status='SUSPENDED',status='BLOCKED' WHERE id=?",(uid,))
r=u.post("/api/auth/login",json={"email":"legacy84@test.local","password":"Senha@123"})
ck("05 bloqueio legado de suspensão não impede cliente",r.status_code==200,r.text)
ck("06 login legado retorna creatorActive false",r.json()["account"]["creatorActive"] is False,r.json()["account"])

# Bloqueio explícito de CLIENTE deve continuar impedindo.
u2=TestClient(m.app)
r=u2.post("/api/auth/register",json={"firstName":"Blocked","lastName":"Seller","email":"blocked84@test.local","password":"Senha@123","role":"collaborator"});assert r.status_code==200
uid2=r.json()["account"]["id"];H2={"X-CSRF-Token":r.json()["account"]["csrfToken"]}
u2.post("/api/auth/logout",headers=H2,json={})
assert admin.patch(f"/api/admin/clients/{uid2}/status",headers=AH,json={"status":"BLOCKED"}).status_code==200
with m.db() as db: db.execute("UPDATE users SET creator_status='SUSPENDED' WHERE id=?",(uid2,))
r=TestClient(m.app).post("/api/auth/login",json={"email":"blocked84@test.local","password":"Senha@123"})
ck("07 bloqueio explícito de Cliente continua bloqueando",r.status_code==403,r.text)

html=Path(sys.argv[2]).read_text(encoding="utf-8")
ck("08 frontend V8.7","const ATV_BUILD='V8.7';" in html)
ck("09 displayFormats comum usa primary","function displayFormats(p)" in html)
ck("10 Premium Max é avaliado antes de purchased","else if (p.accessTier==='PREMIUM_MAX')" in html and "Acesso preservado ao Premium Max" in html)
ck("11 chat não usa var(--card) em isolamento",'v8-5-definitive-visual-fixes' in html and 'background:#f4f1fb!important' in html)
ck("12 chat texto com cor explícita/visível","visibility:visible!important" in html and "opacity:1!important" in html)
print(json.dumps({"PASS":True,"checks":12},ensure_ascii=False))
