
import os, sys, tempfile, importlib.util, json, base64
os.environ["ATV_ENV"]="local"
os.environ["ATV_COOKIE_SECURE"]="0"
os.environ["ATV_ADMIN_PIN"]="246810"
os.environ["ATV_ADMIN_KEY"]="AD!!"
os.environ["ATV_DATA_DIR"]=tempfile.mkdtemp(prefix="atv-v5-")

spec=importlib.util.spec_from_file_location("atvv5",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
from fastapi.testclient import TestClient
from pathlib import Path
c=TestClient(m.app)

def admin_login():
    r=c.post("/api/admin/auth/step1",json={"pin":"246810"});assert r.status_code==200,r.text
    r=c.post("/api/admin/auth/step2",json={"challenge":r.json()["challenge"],"key":"AD!!"});assert r.status_code==200,r.text
    return {"X-Admin-CSRF-Token":r.json()["csrfToken"]}

AH=admin_login()

# 1. Admin 2FA and founder campaign
r=c.patch("/api/admin/founders",headers=AH,json={"enabled":True,"limit":10,"autoClose":True});assert r.status_code==200,r.text
assert r.json()["campaign"]["enabled"] is True

# 2. First 10 direct collaborators receive lifetime premium, 11th does not.
founders=[]
for i in range(1,12):
    s=TestClient(m.app)
    rr=s.post("/api/auth/register",json={"firstName":f"Designer{i}","lastName":"Teste","email":f"d{i}@teste.local","password":"Senha@123","role":"collaborator"})
    assert rr.status_code==200,rr.text
    acc=rr.json()["account"]
    if i<=10:
        assert acc["premiumLifetime"] is True,(i,acc)
        assert acc["premiumCycle"]=="LIFETIME",(i,acc)
        assert acc["founderSlot"]==i,(i,acc)
        founders.append(acc["id"])
    else:
        assert acc.get("premiumLifetime") is False,(i,acc)
        assert "collaborator" in acc.get("roles",[]),(i,acc)

campaign=c.get("/api/admin/founders").json()["campaign"]
assert campaign["claimed"]==10,campaign
assert campaign["full"] is True,campaign
assert campaign["enabled"] is False,campaign
assert campaign["remaining"]==0,campaign

# 3. Lifetime cannot be downgraded.
s=TestClient(m.app)
rr=s.post("/api/auth/login",json={"email":"d1@teste.local","password":"Senha@123"});assert rr.status_code==200
csrf=rr.json()["account"]["csrfToken"]
rr=s.post("/api/account/simulate-plan",headers={"X-CSRF-Token":csrf},json={"plan":"FREE","cycle":"MONTHLY"})
assert rr.status_code==409,rr.text

# 4. Existing client -> collaborator after campaign full stays collaborator but no lifetime.
s2=TestClient(m.app)
rr=s2.post("/api/auth/register",json={"firstName":"Cliente","lastName":"Onze","email":"cliente11@teste.local","password":"Senha@123","role":"client"});assert rr.status_code==200
csrf2=rr.json()["account"]["csrfToken"]
rr=s2.post("/api/account/activate-collaborator",headers={"X-CSRF-Token":csrf2},json={});assert rr.status_code==200,rr.text
assert "collaborator" in rr.json()["roles"]
assert rr.json()["premiumLifetime"] is False

# 5. Create a fresh collaborator with campaign paused for asset workflow.
seller=TestClient(m.app)
rr=seller.post("/api/auth/register",json={"firstName":"Arte","lastName":"PNG","email":"artepng@teste.local","password":"Senha@123","role":"collaborator"});assert rr.status_code==200,rr.text
scsrf=rr.json()["account"]["csrfToken"]
png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=")
files={"file":("arte.png",png,"image/png"),"preview":("preview.png",png,"image/png")}
rr=seller.post("/api/uploads/resource",headers={"X-CSRF-Token":scsrf},files=files,data={"metadata":"{}","previewMethod":"TEST"})
assert rr.status_code==200,rr.text
up=rr.json()
rr=seller.post("/api/seller/products",headers={"X-CSRF-Token":scsrf},json={"title":"PNG Homologação V5","category":"PNG","accessTier":"PREMIUM","description":"Teste completo","tags":["png"],"uploadId":up["uploadId"]})
assert rr.status_code==200,rr.text
p=rr.json()
assert p["status"]=="PENDING_REVIEW",p
assert p["category"]=="PNG",p
assert p["id"] not in [x["id"] for x in c.get("/api/products").json()]

# 6. Owner can see and open pending preview by both id and slug.
own=seller.get("/api/seller/products").json()
assert p["id"] in [x["id"] for x in own],own
for key in (p["id"],p["slug"]):
    rr=seller.get("/api/seller/products/"+key);assert rr.status_code==200,rr.text
    assert rr.json()["status"]=="PENDING_REVIEW"

# 7. Dedicated approval must persist PUBLISHED + visible.
rr=c.post(f"/api/admin/products/{p['id']}/approve",headers=AH,json={})
assert rr.status_code==200,rr.text
approved=rr.json()
assert approved["status"]=="PUBLISHED" and approved["hidden"] is False,approved
verify=c.get(f"/api/admin/products/{p['id']}/detail").json()
assert verify["status"]=="PUBLISHED" and verify["hidden"] is False,verify
pub=c.get("/api/products").json()
hit=[x for x in pub if x["id"]==p["id"]]
assert hit and hit[0]["category"]=="PNG",pub
assert c.get("/api/products/"+p["slug"]).status_code==200

# 8. Status transitions persist.
rr=c.patch(f"/api/admin/products/{p['id']}",headers=AH,json={"status":"IN_REVIEW","category":"PNG","hidden":False,"featured":False,"individualPriceCents":500})
assert rr.status_code==200 and rr.json()["status"]=="IN_REVIEW" and rr.json()["hidden"] is True,rr.text
rr=c.post(f"/api/admin/products/{p['id']}/approve",headers=AH,json={})
assert rr.status_code==200 and rr.json()["status"]=="PUBLISHED" and rr.json()["hidden"] is False,rr.text
rr=c.patch(f"/api/admin/products/{p['id']}",headers=AH,json={"status":"SUSPENDED","category":"PNG","hidden":False,"featured":False,"individualPriceCents":500})
assert rr.status_code==200 and rr.json()["status"]=="SUSPENDED" and rr.json()["hidden"] is True,rr.text
rr=c.post(f"/api/admin/products/{p['id']}/approve",headers=AH,json={})
assert rr.status_code==200 and rr.json()["status"]=="PUBLISHED"

# 9. Preview replace
newpng=png
rr=c.post(f"/api/admin/products/{p['id']}/preview",headers=AH,files={"preview":("novo.png",newpng,"image/png")},data={"previewMethod":"REGRESSION"})
assert rr.status_code==200,rr.text
assert rr.json()["image"].startswith("/media/previews/"),rr.json()

# 10. Checkout + entitlement + download authorization.
buyer=TestClient(m.app)
rr=buyer.post("/api/auth/register",json={"firstName":"Buyer","lastName":"Teste","email":"buyer@teste.local","password":"Senha@123","role":"client"});assert rr.status_code==200
bcsrf=rr.json()["account"]["csrfToken"]
rr=buyer.patch("/api/account/library",headers={"X-CSRF-Token":bcsrf},json={"cart":[p["id"]]});assert rr.status_code==200,rr.text
rr=buyer.post("/api/checkout/simulate",headers={"X-CSRF-Token":bcsrf},json={"items":[p["id"]]});assert rr.status_code==200,rr.text
lib=buyer.get("/api/downloads");assert lib.status_code==200,lib.text
assert any(x["product"]["id"]==p["id"] for x in lib.json()["purchased"])
rr=buyer.post("/api/downloads/consume",headers={"X-CSRF-Token":bcsrf},json={"productId":p["id"],"source":"PURCHASE"});assert rr.status_code==200,rr.text
assert rr.json().get("downloadUrl")

# 11. Support + reports
rr=buyer.post("/api/support/tickets",json={"name":"Buyer","email":"buyer@teste.local","subject":"Teste","message":"Mensagem de homologação"});assert rr.status_code in (200,201),rr.text
rr=buyer.post("/api/reports",json={"name":"Buyer","email":"buyer@teste.local","resource":"Teste","reason":"Outro","description":"Teste","evidence":None});assert rr.status_code in (200,201),rr.text

# 12. Admin block invalidates account session.
clients=c.get("/api/admin/clients").json()
bid=next(x["id"] for x in clients if x["email"]=="buyer@teste.local")
rr=c.patch(f"/api/admin/clients/{bid}/status",headers=AH,json={"status":"BLOCKED"});assert rr.status_code==200,rr.text
assert buyer.get("/api/account").json()["id"] is None

print(json.dumps({
 "PASS":True,
 "admin_2fa":True,
 "founder_first_10":True,
 "founder_11_blocked_from_benefit":True,
 "lifetime_protected":True,
 "pending_owner_preview":True,
 "approval_persisted":True,
 "public_png_after_approval":True,
 "status_transitions":True,
 "preview_replace":True,
 "checkout_download":True,
 "support_report":True,
 "account_block":True
},ensure_ascii=False))
