"""
agent_collecteur.py - Collecteur Universel de Liens Agricoles
VERSION SECURISEE - Aucune cle hardcodee
Render + GitHub safe
"""
import os, re, json, hashlib, datetime, io, time
import sqlite3, jwt, requests
from urllib.parse import urlparse
from functools import wraps
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from bs4 import BeautifulSoup
from groq import Groq
from langchain_huggingface import HuggingFaceEmbeddings
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── CONFIG ── 100% variables d'environnement ──────────────────
GROQ_API_KEY   = os.getenv("GROQ_API_KEY")
QDRANT_URL     = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
JWT_SECRET     = os.getenv("JWT_SECRET",     "change_moi_en_production")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change_moi_en_production")
DB_PATH        = os.getenv("DB_PATH",        "collecteur.db")
COLLECTION     = "collecteur_liens"
MODEL          = "llama-3.1-8b-instant"
EMBED_DIM      = 384
PORT           = int(os.getenv("PORT", 5005))
# ─────────────────────────────────────────────────────────────

missing = [v for v,val in [("GROQ_API_KEY",GROQ_API_KEY),("QDRANT_URL",QDRANT_URL),("QDRANT_API_KEY",QDRANT_API_KEY)] if not val]
if missing:
    raise ValueError(f"Variables d'environnement manquantes: {missing}\nVerifier .env (local) ou variables Render (production)")

app = Flask(__name__)
CORS(app)
groq_client = Groq(api_key=GROQ_API_KEY)

def generate_token():
    return jwt.encode({"admin":True,"exp":datetime.datetime.utcnow()+datetime.timedelta(hours=24),"iat":datetime.datetime.utcnow()}, JWT_SECRET, algorithm="HS256")

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization","").replace("Bearer ","") or request.args.get("token","")
        if not token: return jsonify({"error":"Token requis"}), 401
        try: jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.ExpiredSignatureError: return jsonify({"error":"Token expire"}), 401
        except jwt.InvalidTokenError: return jsonify({"error":"Token invalide"}), 401
        return f(*args, **kwargs)
    return decorated

print("Connexion Qdrant Cloud...")
try:
    qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    if COLLECTION not in [c.name for c in qdrant.get_collections().collections]:
        qdrant.create_collection(collection_name=COLLECTION, vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE))
        print(f"Collection '{COLLECTION}' creee")
    else:
        print(f"Collection '{COLLECTION}' chargee")
except Exception as e:
    print(f"Qdrant error: {e}")
    qdrant = None

print("Chargement embeddings...")
emb = HuggingFaceEmbeddings(model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", model_kwargs={"device":"cpu"})
print("Embeddings prets")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.cursor().execute('''CREATE TABLE IF NOT EXISTS liens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, url_hash TEXT UNIQUE,
        titre TEXT, contenu TEXT, source_type TEXT, plateforme TEXT, envoyeur TEXT,
        institution TEXT, commentaire TEXT, date_collecte TIMESTAMP, date_publication TEXT,
        statut TEXT, raison_rejet TEXT, mots_cles TEXT, langue TEXT, nb_mots INTEGER, indexe_qdrant INTEGER DEFAULT 0)''')
    conn.commit(); conn.close()
init_db()

AGRI_KEYWORDS = ["agricult","agro","farm","crop","culture","recolte","harvest","cacao","cafe","coffee","mais","corn","maize","riz","rice","manioc","cassava","plantain","banane","coton","cotton","palmier","palm","hevea","rubber","sorgho","mil","millet","tomate","oignon","arachide","groundnut","haricot","bean","elevage","livestock","bovin","cattle","poulet","poultry","porc","pig","chevre","goat","mouton","sheep","peche","fish","veterinaire","veterinary","animal","apiculture","beekeeping","tracteur","tractor","engrais","fertilizer","pesticide","irrigation","semence","seed","mecanisation","mechanization","microfinance","subvention","cooperative","stockage","storage","silo","transformation","emballage","marche agricole","agricultural market","filiere","value chain","export","irad","minader","fao","ifad","agri","rural","paysan","farmer","smallholder","cameroun","cameroon","afrique","africa","sol","soil","fertilite","fertility","secheresse","drought","climat","climate","agroforesterie","agroforestry","foret","forest","bois","timber","innovation","plante","plant","logistique","securite alimentaire","food security","nutrition","changement climatique","climate change","pisciculture","aquaculture"]
TRUSTED = ["fao.org","worldbank.org","ifad.org","cgiar.org","cirad.fr","ird.fr","semanticscholar.org","openalex.org","researchgate.net","sciencedirect.com","springer.com","wiley.com","mdpi.com","nature.com","ncbi.nlm.nih.gov"]
BLOCKED = ["porn","adult","casino","bet","gambling","hack","crack","pirate","torrent","warez"]
BLOCKED_SOCIAL = ["facebook.com","instagram.com","tiktok.com","fb.com","twitter.com","x.com"]

def detect_platform(url):
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "youtube"
    if "linkedin.com" in u: return "linkedin"
    if "twitter.com" in u or "x.com" in u: return "twitter"
    if "facebook.com" in u or "fb.com" in u: return "facebook"
    if "instagram.com" in u: return "instagram"
    if "tiktok.com" in u: return "tiktok"
    if "reddit.com" in u: return "reddit"
    if ".pdf" in u: return "pdf"
    return "web"

def extract_content(url, platform, commentaire=""):
    result = {"titre":"","contenu":"","langue":"fr","date_pub":""}
    headers = {"User-Agent":"Mozilla/5.0 Chrome/120.0.0.0","Accept-Language":"fr-FR,fr;q=0.9,en;q=0.8"}
    if platform == "youtube":
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
            vid = re.search(r'(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})', url)
            if vid:
                t = YouTubeTranscriptApi.get_transcript(vid.group(1), languages=['fr','en'])
                result["contenu"] = " ".join([x['text'] for x in t])[:3000]
                result["titre"] = f"YouTube: {url}"
                return result
        except: pass
        try:
            soup = BeautifulSoup(requests.get(url,headers=headers,timeout=8).text,'html.parser')
            og = soup.find('meta',property='og:title')
            result["titre"] = og['content'] if og else url
            result["contenu"] = result["titre"] + " " + commentaire
        except: pass
        return result
    if platform == "pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(requests.get(url,headers=headers,timeout=15,stream=True).content))
            result["titre"] = url.split("/")[-1]
            result["contenu"] = " ".join([p.extract_text() or "" for p in reader.pages[:10]])[:3000]
        except: pass
        return result
    domain = urlparse(url).netloc.lower()
    if any(s in domain for s in BLOCKED_SOCIAL):
        try:
            soup = BeautifulSoup(requests.get(url,headers=headers,timeout=8).text,'html.parser')
            og_t = soup.find('meta',property='og:title')
            og_d = soup.find('meta',property='og:description')
            result["titre"] = og_t['content'] if og_t else url
            result["contenu"] = og_d['content'] if og_d else ""
        except: pass
        result["contenu"] = (result["contenu"]+" "+commentaire).strip()
        if not result["titre"]: result["titre"] = url
        return result
    try:
        resp = requests.get(url,headers=headers,timeout=10)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text,'html.parser')
        og_t = soup.find('meta',property='og:title')
        t_tag = soup.find('title')
        result["titre"] = og_t['content'] if og_t else (t_tag.text if t_tag else url)
        for tag in soup(['script','style','nav','footer','header','aside']): tag.decompose()
        main = soup.find('article') or soup.find('main') or soup.find('div',class_=re.compile(r'content|article|post|body'))
        paragraphs = main.find_all('p') if main else soup.find_all('p')
        result["contenu"] = " ".join([p.get_text() for p in paragraphs if len(p.get_text())>30])[:3000]
        dm = soup.find('meta',property='article:published_time')
        if dm: result["date_pub"] = dm.get('content','')
        lang = soup.find('html')
        if lang and lang.get('lang'): result["langue"] = lang.get('lang','fr')[:2]
    except: result["contenu"] = commentaire
    return result

def is_agricultural(url, titre, contenu, commentaire=""):
    domain = urlparse(url).netloc.lower()
    if any(b in domain for b in BLOCKED): return False, "Source bloquee"
    if any(t in domain for t in TRUSTED): return True, "Source de confiance"
    if any(s in domain for s in BLOCKED_SOCIAL):
        check = (commentaire+" "+titre).lower()
        kw = [k for k in AGRI_KEYWORDS if k in check]
        return True, (f"Mots-cles: {', '.join(kw[:3])}" if kw else "Reseau social - contenu presume agricole")
    text = (titre+" "+contenu[:500]+" "+commentaire).lower()
    kw = [k for k in AGRI_KEYWORDS if k in text]
    if len(kw) >= 2: return True, f"Mots-cles: {', '.join(kw[:3])}"
    if len((contenu+commentaire).strip()) < 30: return False, "Contenu insuffisant"
    try:
        r = groq_client.chat.completions.create(model=MODEL,messages=[{"role":"user","content":f"Ce contenu est-il lie a l'agriculture africaine ? Titre: {titre[:200]} Contenu: {contenu[:300]} {commentaire}\nReponds UNIQUEMENT: OUI ou NON"}],temperature=0.0,max_tokens=5)
        ans = r.choices[0].message.content.strip().upper()
        return ("OUI" in ans, "LLM: agricole" if "OUI" in ans else "LLM: hors agriculture")
    except:
        return True, "Accepte par defaut"

def index_to_qdrant(url, titre, contenu, platform, mots_cles):
    if not qdrant: return False
    try:
        qdrant.upsert(collection_name=COLLECTION,points=[PointStruct(id=abs(hash(url))%(2**63),vector=emb.embed_query(f"TITRE: {titre}\n\nCONTENU: {contenu}"),payload={"url":url[:200],"titre":titre[:100],"plateforme":platform,"mots_cles":mots_cles[:200],"date":datetime.datetime.now().isoformat()})])
        return True
    except Exception as e:
        print(f"Qdrant error: {e}"); return False

def search_qdrant(question, k=5):
    if not qdrant: return []
    try:
        results = qdrant.query_points(collection_name=COLLECTION,query=emb.embed_query(question),limit=k,with_payload=True).points
        return [{"url":r.payload.get("url",""),"titre":r.payload.get("titre",""),"score":round(r.score,3)} for r in results]
    except: return []

HTML = """<!DOCTYPE html><html lang="fr"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Collecteur</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Geist+Mono:wght@300;400;500&family=Geist:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}:root{--g:#1DB954;--gd:#17A547;--gdim:rgba(29,185,84,0.12);--gbor:rgba(29,185,84,0.22);--bg:#061A0E;--bg2:#0A2214;--bg3:#0D2A18;--bg4:#102E1C;--bor:rgba(29,185,84,0.1);--bor2:rgba(29,185,84,0.2);--t:#FFFFFF;--t2:rgba(255,255,255,0.65);--t3:rgba(255,255,255,0.3);--t4:rgba(255,255,255,0.12)}html,body{height:100%;overflow:hidden}body{background:var(--bg);color:var(--t);font-family:'Geist',sans-serif;display:flex;flex-direction:column}body::before{content:'';position:fixed;top:-15%;left:50%;transform:translateX(-50%);width:700px;height:350px;background:radial-gradient(ellipse,rgba(29,185,84,0.07) 0%,transparent 70%);pointer-events:none}header{display:flex;align-items:center;justify-content:space-between;padding:0 28px;height:56px;border-bottom:1px solid var(--bor);background:rgba(6,26,14,0.97);backdrop-filter:blur(12px);flex-shrink:0;z-index:100;position:relative}.logo{display:flex;align-items:center;gap:11px}.logo-mark{width:30px;height:30px;background:linear-gradient(135deg,var(--g),var(--gd));border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:15px;box-shadow:0 0 14px rgba(29,185,84,0.28)}.logo-name{font-family:'Instrument Serif',serif;font-size:1.1rem}.logo-sub{font-family:'Geist Mono',monospace;font-size:.55rem;color:var(--g);letter-spacing:.2em;text-transform:uppercase;margin-top:1px}.hdr-right{display:flex;align-items:center;gap:10px}.pulse{width:7px;height:7px;border-radius:50%;background:var(--g);box-shadow:0 0 8px var(--g);animation:pulse 2s infinite}@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.8)}}.stxt{font-family:'Geist Mono',monospace;font-size:.6rem;color:var(--t3);letter-spacing:.1em}.nav{display:none;align-items:center;gap:2px;background:var(--bg2);border:1px solid var(--bor2);border-radius:7px;padding:3px}.nb{font-family:'Geist Mono',monospace;font-size:.6rem;padding:4px 11px;border-radius:5px;border:none;background:transparent;color:var(--t3);cursor:pointer;transition:all .15s;letter-spacing:.07em;text-transform:uppercase}.nb:hover{color:var(--t2)}.nb.on{background:var(--g);color:#000;font-weight:600}.view{flex:1;display:none;flex-direction:column;min-height:0;animation:fu .3s ease}.view.show{display:flex}@keyframes fu{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}#v-landing{align-items:center;justify-content:center;gap:36px;padding:32px}.hero{text-align:center}.eyebrow{font-family:'Geist Mono',monospace;font-size:.62rem;color:var(--g);letter-spacing:.25em;text-transform:uppercase;margin-bottom:12px}.h1{font-family:'Instrument Serif',serif;font-size:clamp(1.8rem,4vw,2.8rem);line-height:1.15;margin-bottom:10px}.h1 em{color:var(--g);font-style:italic}.sub{font-size:.83rem;color:var(--t3);line-height:1.65;max-width:460px;margin:0 auto}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;width:100%;max-width:820px}.card{background:var(--bg2);border:1px solid var(--bor2);border-radius:14px;padding:22px 18px;cursor:pointer;transition:all .2s;position:relative;overflow:hidden;text-align:left}.card::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,var(--gdim),transparent);opacity:0;transition:opacity .2s}.card:hover{border-color:var(--g);transform:translateY(-3px);box-shadow:0 12px 36px rgba(29,185,84,0.13)}.card:hover::before{opacity:1}.card-ico{font-size:1.7rem;margin-bottom:10px;display:block}.card-ttl{font-family:'Instrument Serif',serif;font-size:1.1rem;margin-bottom:4px}.card-dsc{font-size:.73rem;color:var(--t3);line-height:1.5}.card-arr{position:absolute;bottom:14px;right:14px;width:24px;height:24px;border-radius:50%;background:var(--gdim);border:1px solid var(--gbor);display:flex;align-items:center;justify-content:center;font-size:.75rem;color:var(--g);transition:all .2s}.card:hover .card-arr{background:var(--g);color:#000}.topbar{display:flex;align-items:center;justify-content:space-between;padding:9px 22px;background:var(--bg2);border-bottom:1px solid var(--bor);flex-shrink:0}.back{font-family:'Geist Mono',monospace;font-size:.62rem;color:var(--t3);cursor:pointer;border:1px solid var(--bor);background:transparent;padding:5px 11px;border-radius:6px;transition:all .15s}.back:hover{border-color:var(--g);color:var(--g)}.tb-title{font-family:'Geist Mono',monospace;font-size:.62rem;color:var(--g);letter-spacing:.14em;text-transform:uppercase}.form-card{background:var(--bg2);border:1px solid var(--bor2);border-radius:14px;padding:24px;max-width:640px;width:100%;margin:0 auto}.form-title{font-family:'Instrument Serif',serif;font-size:1.3rem;margin-bottom:4px}.form-sub{font-size:.76rem;color:var(--t3);margin-bottom:20px;line-height:1.5}.field-lbl{font-family:'Geist Mono',monospace;font-size:.6rem;color:var(--t3);letter-spacing:.12em;text-transform:uppercase;margin-bottom:5px}.field-inp{width:100%;background:var(--bg3);border:1px solid var(--bor2);border-radius:8px;padding:10px 12px;color:var(--t);font-family:'Geist',sans-serif;font-size:.875rem;outline:none;margin-bottom:14px;transition:border-color .15s}.field-inp:focus{border-color:rgba(29,185,84,.45)}.field-inp::placeholder{color:var(--t4)}.submit-btn{width:100%;padding:12px;background:var(--g);border:none;border-radius:8px;color:#000;font-family:'Geist Mono',monospace;font-size:.72rem;font-weight:600;cursor:pointer;transition:background .15s;letter-spacing:.08em}.submit-btn:hover{background:var(--gd)}.submit-btn:disabled{background:var(--bg4);color:var(--t3);cursor:not-allowed}.result-card{max-width:640px;width:100%;margin:0 auto;border-radius:12px;padding:16px;border:1px solid var(--bor2);background:var(--bg2)}.result-ok{border-color:rgba(34,197,94,0.4)!important}.result-ko{border-color:rgba(239,68,68,0.4)!important}.result-title{font-family:'Geist Mono',monospace;font-size:.65rem;letter-spacing:.1em;margin-bottom:8px}.badge{display:inline-flex;align-items:center;gap:4px;font-family:'Geist Mono',monospace;font-size:.58rem;padding:2px 8px;border-radius:4px;margin:2px}.badge-ok{background:rgba(34,197,94,0.1);color:#4ade80;border:1px solid rgba(34,197,94,0.2)}.badge-ko{background:rgba(239,68,68,0.1);color:#f87171;border:1px solid rgba(239,68,68,0.2)}.badge-info{background:rgba(59,130,246,0.1);color:#60a5fa;border:1px solid rgba(59,130,246,0.2)}.progress-wrap{background:var(--bg3);border-radius:4px;height:4px;width:100%;margin-top:8px;overflow:hidden}.progress-bar{height:100%;background:var(--g);border-radius:4px;transition:width .3s;width:0%}.liens-filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}.filter-btn{font-family:'Geist Mono',monospace;font-size:.6rem;padding:4px 11px;border-radius:20px;border:1px solid var(--bor2);background:transparent;color:var(--t3);cursor:pointer;transition:all .15s}.filter-btn:hover,.filter-btn.on{border-color:var(--g);color:var(--g);background:var(--gdim)}.lien-item{background:var(--bg2);border:1px solid var(--bor);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:6px}.lien-titre{font-size:.84rem;color:var(--t);font-weight:500}.lien-url{font-size:.7rem;color:var(--t3);word-break:break-all}.lien-meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stat-card{background:var(--bg2);border:1px solid var(--bor2);border-radius:10px;padding:16px}.stat-val{font-family:'Instrument Serif',serif;font-size:1.9rem;color:var(--g);margin-bottom:4px}.stat-lbl{font-family:'Geist Mono',monospace;font-size:.58rem;color:var(--t3);letter-spacing:.12em;text-transform:uppercase}.platform-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.platform-card{background:var(--bg2);border:1px solid var(--bor);border-radius:8px;padding:12px;text-align:center}.platform-ico{font-size:1.5rem;margin-bottom:6px}.platform-name{font-family:'Geist Mono',monospace;font-size:.6rem;color:var(--t3)}.platform-count{font-size:1.1rem;color:var(--g);font-weight:500}.admin-card{background:var(--bg2);border:1px solid rgba(251,191,36,0.3);border-radius:14px;padding:24px;max-width:400px;width:100%;margin:0 auto}.admin-title{font-family:'Instrument Serif',serif;font-size:1.3rem;color:#fbbf24;margin-bottom:16px}.admin-inp{width:100%;background:var(--bg3);border:1px solid rgba(251,191,36,0.3);border-radius:8px;padding:10px 12px;color:var(--t);font-family:'Geist',sans-serif;font-size:.875rem;outline:none;margin-bottom:12px}.admin-btn{width:100%;padding:11px;background:#fbbf24;border:none;border-radius:8px;color:#000;font-family:'Geist Mono',monospace;font-size:.72rem;font-weight:600;cursor:pointer}.admin-section{background:var(--bg2);border:1px solid rgba(251,191,36,0.2);border-radius:10px;padding:16px;margin-bottom:12px}.admin-section-title{font-family:'Geist Mono',monospace;font-size:.62rem;color:#fbbf24;letter-spacing:.15em;text-transform:uppercase;margin-bottom:10px}#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(14px);background:var(--bg2);border:1px solid var(--gbor);color:var(--t2);font-family:'Geist Mono',monospace;font-size:.62rem;padding:7px 16px;border-radius:20px;opacity:0;transition:all .25s;z-index:1000;pointer-events:none}#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}</style></head><body>
<header><div class="logo"><div class="logo-mark">&#128279;</div><div><div class="logo-name">Agent Collecteur</div><div class="logo-sub">Liens Agricoles &middot; Universal</div></div></div><div class="hdr-right"><div class="nav" id="nav"><button class="nb" onclick="go('landing')">Menu</button><button class="nb" onclick="go('collect')">Collecter</button><button class="nb" onclick="go('liens');loadLiens()">Liens</button><button class="nb" onclick="go('stats');loadStats()">Stats</button><button class="nb" onclick="go('admin')" style="color:#fbbf24">Admin</button></div><div class="pulse"></div><div class="stxt" id="stxt">EN LIGNE</div></div></header>
<div class="view show" id="v-landing"><div class="hero"><div class="eyebrow">Agent Collecteur &middot; Agriculture</div><div class="h1">Collecte <em>universelle</em><br>de liens agricoles</div><div class="sub">Partagez n'importe quel lien - YouTube, Facebook, Twitter, PDF, articles. L'agent extrait, filtre et indexe dans Qdrant Cloud.</div></div><div class="cards"><div class="card" onclick="go('collect')"><span class="card-ico">&#128279;</span><div class="card-ttl">Soumettre un lien</div><div class="card-dsc">Partagez n'importe quelle URL publique</div><div class="card-arr">&rarr;</div></div><div class="card" onclick="go('liens');loadLiens()"><span class="card-ico">&#128203;</span><div class="card-ttl">Voir les liens</div><div class="card-dsc">Parcourez les liens collectes</div><div class="card-arr">&rarr;</div></div><div class="card" onclick="go('stats');loadStats()"><span class="card-ico">&#128202;</span><div class="card-ttl">Statistiques</div><div class="card-dsc">Sources, plateformes, taux d'acceptation</div><div class="card-arr">&rarr;</div></div></div></div>
<div class="view" id="v-collect"><div class="topbar"><button class="back" onclick="go('landing')">&larr; Retour</button><div class="tb-title">&#128279; Soumettre un lien</div><div></div></div><div style="padding:24px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:16px;align-items:center"><div class="form-card"><div class="form-title">Partager un lien agricole</div><div class="form-sub">YouTube, Facebook, Twitter, LinkedIn, PDF, articles... Ajoutez un commentaire pour les reseaux sociaux prives.</div><div class="field-lbl">URL du lien *</div><input class="field-inp" id="url-inp" type="url" placeholder="https://..."><div class="field-lbl">Commentaire (recommande pour reseaux sociaux)</div><input class="field-inp" id="comm-inp" type="text" placeholder="Ex: Video sur la culture du cacao au Nord Cameroun..."><div class="field-lbl">Votre nom (optionnel)</div><input class="field-inp" id="nom-inp" type="text" placeholder="Ex: Jean Dupont"><div class="field-lbl">Institution (optionnel)</div><input class="field-inp" id="inst-inp" type="text" placeholder="Ex: IRAD, MINADER..."><button class="submit-btn" id="submit-btn" onclick="submitLink()">Soumettre et analyser</button><div class="progress-wrap" id="progress-wrap" style="display:none"><div class="progress-bar" id="progress-bar"></div></div></div><div id="result-zone"></div></div></div>
<div class="view" id="v-liens"><div class="topbar"><button class="back" onclick="go('landing')">&larr; Retour</button><div class="tb-title">&#128203; Liens collectes</div><button class="back" onclick="loadLiens()">&#8635;</button></div><div style="padding:16px 20px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:10px"><div class="liens-filters"><button class="filter-btn on" onclick="filterLiens('tous',this)">Tous</button><button class="filter-btn" onclick="filterLiens('accepte',this)">Acceptes</button><button class="filter-btn" onclick="filterLiens('rejete',this)">Rejetes</button><button class="filter-btn" onclick="filterLiens('youtube',this)">YouTube</button><button class="filter-btn" onclick="filterLiens('facebook',this)">Facebook</button><button class="filter-btn" onclick="filterLiens('web',this)">Web</button><button class="filter-btn" onclick="filterLiens('pdf',this)">PDF</button></div><div id="liens-list" style="display:flex;flex-direction:column;gap:8px"></div></div></div>
<div class="view" id="v-stats"><div class="topbar"><button class="back" onclick="go('landing')">&larr; Retour</button><div class="tb-title">&#128202; Statistiques</div><button class="back" onclick="loadStats()">&#8635;</button></div><div style="padding:20px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:14px"><div class="stats-grid"><div class="stat-card"><div class="stat-val" id="s-total">-</div><div class="stat-lbl">Total liens</div></div><div class="stat-card"><div class="stat-val" id="s-accept">-</div><div class="stat-lbl">Acceptes</div></div><div class="stat-card"><div class="stat-val" id="s-reject">-</div><div class="stat-lbl">Rejetes</div></div><div class="stat-card"><div class="stat-val" id="s-qdrant">-</div><div class="stat-lbl">Indexes Qdrant</div></div></div><div style="background:var(--bg2);border:1px solid var(--bor);border-radius:10px;padding:16px"><div style="font-family:'Geist Mono',monospace;font-size:.62rem;color:var(--g);letter-spacing:.15em;text-transform:uppercase;margin-bottom:12px">Par plateforme</div><div class="platform-grid" id="platform-grid"></div></div></div></div>
<div class="view" id="v-admin"><div class="topbar"><button class="back" onclick="go('landing')">&larr; Retour</button><div class="tb-title" style="color:#fbbf24">&#128272; Administration</div><div></div></div><div style="padding:24px;overflow-y:auto;flex:1;min-height:0;display:flex;flex-direction:column;gap:16px;align-items:center"><div class="admin-card" id="admin-login"><div class="admin-title">&#128272; Connexion Admin</div><input class="admin-inp" id="admin-pwd" type="password" placeholder="Mot de passe admin"><button class="admin-btn" onclick="adminLogin()">Se connecter</button></div><div id="admin-dashboard" style="display:none;width:100%;max-width:860px;flex-direction:column;gap:12px"><div class="admin-section"><div class="admin-section-title">&#128279; Gestion des liens</div><div id="admin-liens-list" style="display:flex;flex-direction:column;gap:6px;max-height:300px;overflow-y:auto"></div><button class="admin-btn" style="margin-top:10px" onclick="loadAdminLiens()">&#8635; Charger tous les liens</button></div><div class="admin-section"><div class="admin-section-title">&#128202; Qdrant Cloud</div><div id="admin-qdrant" style="font-family:'Geist Mono',monospace;font-size:.7rem;color:var(--t2)">Chargement...</div><button class="admin-btn" style="margin-top:10px;background:var(--bg3);color:var(--t2);border:1px solid rgba(251,191,36,0.3)" onclick="loadQdrantStats()">&#8635; Stats Qdrant</button></div><div class="admin-section"><div class="admin-section-title">&#128465; Actions</div><button class="admin-btn" style="background:#ef4444" onclick="if(confirm('Supprimer TOUS les liens rejetes ?'))deleteRejected()">Supprimer liens rejetes</button></div></div></div></div>
<div id="toast"></div>
<script>
let allLiens=[],adminToken=localStorage.getItem('admin_token')||'';
const PI={youtube:'&#9654;',linkedin:'&#128188;',twitter:'&#128038;',facebook:'&#128104;',instagram:'&#128247;',tiktok:'&#127925;',reddit:'&#128992;',pdf:'&#128196;',web:'&#127760;',whatsapp:'&#128172;',telegram:'&#9992;',researchgate:'&#128300;'};
function go(v){document.querySelectorAll('.view').forEach(e=>{e.classList.remove('show');e.style.flex='0'});const el=document.getElementById('v-'+v);el.classList.add('show');el.style.flex='1';document.getElementById('nav').style.display=v==='landing'?'none':'flex';document.querySelectorAll('.nb').forEach(b=>{const m={collect:'Collecter',liens:'Liens',stats:'Stats',landing:'Menu',admin:'Admin'};b.classList.toggle('on',b.textContent===(m[v]||''));});if(v==='admin'&&adminToken)showAdminDashboard();}
function toast(msg,d=2500){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),d);}
function setProgress(p){document.getElementById('progress-wrap').style.display='block';document.getElementById('progress-bar').style.width=p+'%';}
async function submitLink(){const url=document.getElementById('url-inp').value.trim(),comm=document.getElementById('comm-inp').value.trim(),nom=document.getElementById('nom-inp').value.trim(),inst=document.getElementById('inst-inp').value.trim();if(!url){toast('URL requis');return;}const btn=document.getElementById('submit-btn');btn.disabled=true;btn.textContent='Analyse en cours...';document.getElementById('stxt').textContent='ANALYSE...';setProgress(30);try{setProgress(60);const res=await fetch('/collect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,commentaire:comm,envoyeur:nom,institution:inst})});setProgress(90);const data=await res.json();setProgress(100);const ok=data.statut==='accepte',ico=PI[data.plateforme]||'&#127760;';document.getElementById('result-zone').innerHTML=`<div class="result-card ${ok?'result-ok':'result-ko'}"><div class="result-title" style="color:${ok?'#4ade80':'#f87171'}">${ok?'LIEN ACCEPTE':'LIEN REJETE'}</div><div style="font-size:.88rem;font-weight:500;margin-bottom:6px">${data.titre||url}</div><div style="font-size:.72rem;color:var(--t3);word-break:break-all;margin-bottom:8px">${url}</div><div style="display:flex;flex-wrap:wrap;gap:4px"><span class="badge badge-info">${ico} ${data.plateforme||'web'}</span>${ok?'<span class="badge badge-ok">Qdrant</span>':''}${data.nb_mots?`<span class="badge badge-info">${data.nb_mots} mots</span>`:''}<span class="badge ${ok?'badge-ok':'badge-ko'}">${data.raison||''}</span></div>${data.extrait?`<div style="margin-top:10px;font-size:.74rem;color:var(--t3);padding:8px;background:var(--bg3);border-radius:6px;line-height:1.5">${data.extrait}</div>`:''}</div>`;document.getElementById('url-inp').value='';document.getElementById('comm-inp').value='';document.getElementById('stxt').textContent='EN LIGNE';}catch(err){toast('Erreur: '+err.message);document.getElementById('stxt').textContent='ERREUR';}btn.disabled=false;btn.textContent='Soumettre et analyser';setTimeout(()=>{document.getElementById('progress-wrap').style.display='none';document.getElementById('progress-bar').style.width='0%';},1000);}
async function loadLiens(){try{allLiens=await(await fetch('/liens')).json();renderLiens(allLiens);}catch(e){toast('Erreur');}}
function filterLiens(f,btn){document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('on'));btn.classList.add('on');renderLiens(f==='tous'?allLiens:allLiens.filter(l=>f==='accepte'||f==='rejete'?l.statut===f:l.plateforme===f));}
function renderLiens(liens){const list=document.getElementById('liens-list');if(!liens.length){list.innerHTML='<div style="color:var(--t3);font-size:.78rem;text-align:center;padding:20px">Aucun lien</div>';return;}list.innerHTML=liens.map(l=>{const ico=PI[l.plateforme]||'&#127760;',ok=l.statut==='accepte';return`<div class="lien-item"><div class="lien-titre">${l.titre||l.url}</div><div class="lien-url">${l.url}</div><div class="lien-meta"><span class="badge badge-info">${ico} ${l.plateforme||'web'}</span><span class="badge ${ok?'badge-ok':'badge-ko'}">${ok?'OK':'KO'} ${l.statut}</span>${l.nb_mots?`<span class="badge badge-info">${l.nb_mots} mots</span>`:''} ${l.envoyeur?`<span class="badge badge-info">${l.envoyeur}</span>`:''}</div>${l.raison_rejet&&!ok?`<div style="font-size:.7rem;color:#f87171">${l.raison_rejet}</div>`:''}</div>`}).join('');}
async function loadStats(){try{const d=await(await fetch('/stats')).json();document.getElementById('s-total').textContent=d.total||0;document.getElementById('s-accept').textContent=d.acceptes||0;document.getElementById('s-reject').textContent=d.rejetes||0;document.getElementById('s-qdrant').textContent=d.indexes_qdrant||0;const pg=document.getElementById('platform-grid'),icons={"youtube":"&#9654;","web":"&#127760;","pdf":"&#128196;","twitter":"&#128038;","linkedin":"&#128188;","facebook":"&#128104;","reddit":"&#128992;","autres":"&#128279;"};pg.innerHTML=Object.entries(d.par_plateforme||{}).map(([p,c])=>`<div class="platform-card"><div class="platform-ico">${icons[p]||'&#128279;'}</div><div class="platform-count">${c}</div><div class="platform-name">${p}</div></div>`).join('');}catch(e){toast('Erreur stats');}}
async function adminLogin(){const pwd=document.getElementById('admin-pwd').value;try{const d=await(await fetch('/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})})).json();if(d.token){adminToken=d.token;localStorage.setItem('admin_token',adminToken);toast('Connexion admin reussie');showAdminDashboard();}else{toast('Mot de passe incorrect');}}catch(e){toast('Erreur connexion');}}
function showAdminDashboard(){document.getElementById('admin-login').style.display='none';const dash=document.getElementById('admin-dashboard');dash.style.display='flex';loadAdminLiens();loadQdrantStats();}
async function loadAdminLiens(){try{const liens=await(await fetch('/admin/liens',{headers:{'Authorization':'Bearer '+adminToken}})).json();document.getElementById('admin-liens-list').innerHTML=liens.map(l=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:var(--bg3);border-radius:6px;font-size:.72rem"><span style="color:var(--t2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${l.titre||l.url}</span><span class="badge ${l.statut==='accepte'?'badge-ok':'badge-ko'}" style="flex-shrink:0;margin-left:8px">${l.statut}</span><button onclick="deleteLink(${l.id})" style="background:transparent;border:none;color:#f87171;cursor:pointer;margin-left:6px;font-size:.7rem">X</button></div>`).join('');}catch(e){toast('Non autorise');}}
async function loadQdrantStats(){try{const d=await(await fetch('/admin/qdrant',{headers:{'Authorization':'Bearer '+adminToken}})).json();document.getElementById('admin-qdrant').innerHTML=`Collection: ${d.collection} | Points: ${d.points_count} | Status: ${d.status}`;}catch(e){document.getElementById('admin-qdrant').textContent='Erreur Qdrant';}}
async function deleteLink(id){if(!confirm('Supprimer ?'))return;await fetch(`/admin/liens/${id}`,{method:'DELETE',headers:{'Authorization':'Bearer '+adminToken}});toast('Supprime');loadAdminLiens();}
async function deleteRejected(){await fetch('/admin/liens/rejected',{method:'DELETE',headers:{'Authorization':'Bearer '+adminToken}});toast('Liens rejetes supprimes');loadAdminLiens();loadStats();}
document.getElementById('url-inp').addEventListener('keydown',function(e){if(e.key==='Enter')submitLink();});
</script></body></html>"""

@app.route("/")
def index(): return render_template_string(HTML)

@app.route("/health")
def health(): return jsonify({"status":"ok","agent":"collecteur","port":PORT,"qdrant":qdrant is not None})

@app.route("/collect", methods=["POST"])
def collect():
    data=request.get_json()
    url=data.get("url","").strip()
    if not url: return jsonify({"error":"URL requis"}), 400
    envoyeur=data.get("envoyeur","anonyme"); institution=data.get("institution",""); commentaire=data.get("commentaire","")
    url_hash=hashlib.md5(url.encode()).hexdigest()
    conn=sqlite3.connect(DB_PATH); c=conn.cursor()
    existing=c.execute("SELECT id,statut,titre FROM liens WHERE url_hash=?",(url_hash,)).fetchone()
    if existing: conn.close(); return jsonify({"statut":existing[1],"titre":existing[2],"plateforme":detect_platform(url),"raison":"Doublon detecte","nb_mots":0})
    platform=detect_platform(url); extracted=extract_content(url,platform,commentaire)
    titre=extracted.get("titre","")[:300]; contenu=extracted.get("contenu","")
    langue=extracted.get("langue","fr"); date_pub=extracted.get("date_pub",""); nb_mots=len(contenu.split())
    is_agri,raison=is_agricultural(url,titre,contenu,commentaire)
    statut="accepte" if is_agri else "rejete"
    text_check=(titre+" "+contenu[:500]+" "+commentaire).lower()
    kw_found=[k for k in AGRI_KEYWORDS if k in text_check]; mots_cles=", ".join(kw_found[:10])
    try:
        c.execute("INSERT INTO liens (url,url_hash,titre,contenu,source_type,plateforme,envoyeur,institution,commentaire,date_collecte,date_publication,statut,raison_rejet,mots_cles,langue,nb_mots,indexe_qdrant) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(url,url_hash,titre,contenu[:5000],"manuel",platform,envoyeur,institution,commentaire,datetime.datetime.now(),date_pub,statut,raison if not is_agri else "",mots_cles,langue,nb_mots,0))
        conn.commit(); lien_id=c.lastrowid
    except Exception as e:
        conn.close(); return jsonify({"error":str(e)}), 500
    indexed=False
    if is_agri:
        indexed=index_to_qdrant(url,titre,contenu or commentaire,platform,mots_cles)
        if indexed: c.execute("UPDATE liens SET indexe_qdrant=1 WHERE id=?",(lien_id,)); conn.commit()
    conn.close()
    return jsonify({"statut":statut,"titre":titre,"plateforme":platform,"raison":raison,"nb_mots":nb_mots,"indexe":indexed,"extrait":(contenu or commentaire)[:200]+"..." if len(contenu or commentaire)>200 else (contenu or commentaire)})

@app.route("/liens")
def get_liens():
    conn=sqlite3.connect(DB_PATH); rows=conn.cursor().execute("SELECT id,url,titre,plateforme,envoyeur,statut,raison_rejet,nb_mots,date_collecte FROM liens ORDER BY date_collecte DESC LIMIT 200").fetchall(); conn.close()
    return jsonify([{"id":r[0],"url":r[1],"titre":r[2],"plateforme":r[3],"envoyeur":r[4],"statut":r[5],"raison_rejet":r[6],"nb_mots":r[7],"date_collecte":r[8]} for r in rows])

@app.route("/stats")
def stats():
    conn=sqlite3.connect(DB_PATH); c=conn.cursor()
    total=c.execute("SELECT COUNT(*) FROM liens").fetchone()[0]; acceptes=c.execute("SELECT COUNT(*) FROM liens WHERE statut='accepte'").fetchone()[0]; rejetes=c.execute("SELECT COUNT(*) FROM liens WHERE statut='rejete'").fetchone()[0]; indexed=c.execute("SELECT COUNT(*) FROM liens WHERE indexe_qdrant=1").fetchone()[0]; platforms=c.execute("SELECT plateforme,COUNT(*) FROM liens GROUP BY plateforme").fetchall(); conn.close()
    qdrant_count=0
    if qdrant:
        try: qdrant_count=qdrant.get_collection(COLLECTION).points_count
        except: pass
    return jsonify({"total":total,"acceptes":acceptes,"rejetes":rejetes,"indexes_qdrant":indexed,"qdrant_points":qdrant_count,"taux_acceptation":f"{round(acceptes/total*100,1)}%" if total else "0%","par_plateforme":{p:n for p,n in platforms}})

@app.route("/search", methods=["POST"])
def search():
    q=request.get_json().get("question","")
    if not q: return jsonify({"error":"Question vide"}), 400
    return jsonify(search_qdrant(q))

@app.route("/admin/login", methods=["POST"])
def admin_login():
    pwd=request.get_json().get("password","")
    if pwd==ADMIN_PASSWORD: return jsonify({"token":generate_token()})
    return jsonify({"error":"Mot de passe incorrect"}), 401

@app.route("/admin/liens")
@require_admin
def admin_liens():
    conn=sqlite3.connect(DB_PATH); rows=conn.cursor().execute("SELECT id,url,titre,plateforme,envoyeur,institution,commentaire,statut,raison_rejet,nb_mots,date_collecte,indexe_qdrant FROM liens ORDER BY date_collecte DESC").fetchall(); conn.close()
    return jsonify([{"id":r[0],"url":r[1],"titre":r[2],"plateforme":r[3],"envoyeur":r[4],"institution":r[5],"commentaire":r[6],"statut":r[7],"raison_rejet":r[8],"nb_mots":r[9],"date_collecte":r[10],"indexe_qdrant":r[11]} for r in rows])

@app.route("/admin/liens/<int:lid>", methods=["DELETE"])
@require_admin
def admin_delete_lien(lid):
    conn=sqlite3.connect(DB_PATH); conn.cursor().execute("DELETE FROM liens WHERE id=?",(lid,)); conn.commit(); conn.close(); return jsonify({"deleted":lid})

@app.route("/admin/liens/rejected", methods=["DELETE"])
@require_admin
def admin_delete_rejected():
    conn=sqlite3.connect(DB_PATH); conn.cursor().execute("DELETE FROM liens WHERE statut='rejete'"); conn.commit(); conn.close(); return jsonify({"status":"ok"})

@app.route("/admin/qdrant")
@require_admin
def admin_qdrant():
    if not qdrant: return jsonify({"error":"Qdrant non disponible"}), 503
    try:
        info=qdrant.get_collection(COLLECTION)
        return jsonify({"collection":COLLECTION,"points_count":info.points_count,"status":str(info.status),"vector_size":EMBED_DIM})
    except Exception as e: return jsonify({"error":str(e)}), 500

if __name__ == "__main__":
    print("\n"+"="*55)
    print("  Agent Collecteur - Liens Agricoles Universels")
    print(f"  http://localhost:{PORT}")
    print("  Qdrant Cloud + JWT Admin + SQLite")
    print("="*55+"\n")
    app.run(debug=False, port=PORT)