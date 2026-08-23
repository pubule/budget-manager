#!/usr/bin/env python3
"""
Bilancio - consolida ed etichetta le transazioni bancarie.

Un solo comando:
    python bilancio.py

Legge tutti gli export (CSV/Excel) presenti nella cartella, li unifica in un
unico file e assegna una categoria a ogni riga usando, in ordine:

  1. lo storico gia' categorizzato a mano in MoneyWiz (backup/*.zip)
  2. le regole scritte a mano in regole.csv
  3. un modello LLM locale via Ollama, solo per cio' che resta

Le risposte del modello finiscono in cache: ogni descrizione nuova viene
chiesta una volta sola.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

try:
    import dashboard
except ImportError:            # dashboard.py e' opzionale
    dashboard = None

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen3:8b"

# Soglia di somiglianza sotto la quale non ci si fida del match sullo storico.
JACCARD_MIN = 0.60

# Distacco minimo in log-verosimiglianza fra la prima e la seconda categoria
# perche' il voto per token venga accettato. Misurato sullo storico: a 1.0 il
# livello copre meno righe ma ne sbaglia molte meno (75% contro 61% a 0).
MARGIN_MIN = 1.0

# Smoothing di Laplace del voto per token.
ALPHA = 0.2

# Valore da mettere in categorie_merge.csv per le categorie che non vanno
# imparate dallo storico: i cassetti dei rifiuti tipo "Casa > Altro", dove
# finiva di tutto. Le righe restano, ma le categorizzano le regole.
IGNORE = "IGNORA"

# Categoria speciale: spostare soldi fra conti propri o verso familiari non e'
# una spesa. Le regole che puntano qui vengono valutate PRIMA dello storico,
# perche' capita di aver etichettato a mano un giroconto come spesa vera.
TRANSFER = "Giroconto"

# Parole che ogni banca infila nelle causali e che non identificano il merchant.
STOPWORDS = {
    "pagamento", "pagamenti", "pag", "pos", "carta", "cart", "operazione",
    "op", "data", "del", "il", "la", "lo", "le", "i", "gli", "un", "una",
    "di", "da", "a", "al", "alla", "in", "su", "per", "con", "e", "ed",
    "bonifico", "disposto", "favore", "ordinante", "beneficiario", "causale",
    "addebito", "accredito", "acquisto", "prelievo", "prel", "atm", "sepa",
    "dd", "cbill", "rid", "mav", "rav", "commissione", "commissioni",
    "importo", "eur", "euro", "srl", "spa", "snc", "sas", "s.r.l", "s.p.a",
    "italia", "italy", "ita", "num", "numero", "rif", "riferimento", "ore",
    "vs", "ns", "c/o", "nr", "id", "cod", "codice", "trn", "seq",
    # Boilerplate che le banche mettono in testa a ogni causale: senza queste
    # il nome del negozio diventa "Vostro Bogoni" o "Diretto Rapporti".
    "disposizione", "disposizioni", "istantaneo", "istantanea", "fattura",
    "fatture", "vostro", "vostra", "nostro", "nostra", "diretto", "diretta",
    "rapporti", "rapporto", "incasso", "incassi", "domiciliazioni",
    "domiciliazione", "utenze", "rata", "rate", "mastercard", "visa",
    "maestro", "pagobancomat", "bancomat", "contactless", "esercente",
    "terminale", "autorizzazione", "valuta", "saldo", "movimento", "conto",
    "banca", "filiale", "servizio", "servizi", "comm", "carte", "estero",
    "cliente", "titolare", "ricevuta", "quietanza", "scadenza",
    "interni", "sportello", "carico", "sdd", "cbi", "giroconto", "versamento",
    "prelevamento", "canone", "spese", "spesa", "mese", "mensile", "annuale",
}

# Codici tecnici e IBAN: vanno via sia per privacy sia perche' sono rumore.
TECHNICAL_PATTERNS = [
    # IBAN: 2 lettere paese, 2 di controllo, poi 11-30 alfanumerici. Volutamente
    # largo, gli IBAN esteri non hanno tutti la stessa lunghezza.
    r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
    r"\b\d{2}[A-Z]{5}\d{8}[A-Z]{4}\d{10}\b",       # 02INTER20250502HSRT1382833170
    r"\b\d{12,}\b",                                 # identificativi lunghi
    r"\b[A-Z0-9]{8,}-[A-Z0-9-]{8,}\b",             # UUID e simili
    r"[*x]{3,}[\s.-]*\d{4}\b",                      # **** 1234, xxxx-5678
    r"\bCRO[\s:]*\d+\b",                            # riferimento bonifico
]

DATE_FORMATS = [
    "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d",
    "%d.%m.%Y", "%m/%d/%Y", "%d-%m-%y", "%d/%m/%y",
]


# --------------------------------------------------------------------------
# normalizzazione
# --------------------------------------------------------------------------

def normalize(text):
    """Minuscolo, senza accenti, senza punteggiatura, spazi singoli."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^a-z0-9&/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def anonymize(description):
    """Toglie IBAN e codici tecnici, lascia intatto il nome del merchant.

    Volutamente NON rimuove i token alfanumerici come Q8, H&M, A4: sono
    esattamente il segnale che serve per categorizzare.
    """
    if description is None or (isinstance(description, float) and pd.isna(description)):
        return ""
    desc = str(description).strip()
    for pattern in TECHNICAL_PATTERNS:
        desc = re.sub(pattern, " ", desc, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", desc).strip()


def tokens(text):
    """Token significativi di una descrizione, senza stopword bancarie."""
    return {
        t for t in normalize(text).split()
        if len(t) > 2 and t not in STOPWORDS and not t.isdigit()
    }


def parse_date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    raw = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    numbers = re.findall(r"\d+", raw)
    if len(numbers) >= 3:
        try:
            day, month, year = numbers[:3]
            if len(year) == 2:
                year = ("20" if int(year) < 50 else "19") + year
            return f"{year}-{int(month):02d}-{int(day):02d}"
        except (ValueError, IndexError):
            pass
    return raw


def parse_amount(value):
    """Importo in float. Gestisce 1.234,56 e 1,234.56 senza confonderli."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raw = re.sub(r"[^\d,.\-+]", "", str(value).strip())
    if "," in raw and "." in raw:
        # L'ultimo separatore che compare e' quello decimale.
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        decimals = raw.split(",")[-1]
        raw = raw.replace(",", "." if len(decimals) <= 2 else "")
    elif raw.count(".") > 1 or re.search(r"\d\.\d{3}$", raw):
        # "1.234" e "1.234.567": in un export italiano il punto separa le
        # migliaia. Un importo con tre decimali non esiste.
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# storico MoneyWiz
# --------------------------------------------------------------------------

def extract_moneywiz_db(folder, workdir):
    """Estrae il sqlite dal backup MoneyWiz piu' recente. None se non c'e'."""
    zips = sorted(folder.glob("**/iMoneyWiz-Data-Backup-*.zip"))
    if not zips:
        return None
    newest = zips[-1]
    workdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(newest) as archive:
        names = set(archive.namelist())
        # Il -wal contiene le scritture non ancora consolidate: senza di lui
        # si perdono le transazioni piu' recenti.
        for name in ("ipadMoneyWiz.sqlite", "ipadMoneyWiz.sqlite-wal",
                     "ipadMoneyWiz.sqlite-shm"):
            if name in names:
                archive.extract(name, workdir)
    db = workdir / "ipadMoneyWiz.sqlite"
    print(f"  storico letto da {newest.name}")
    return db if db.exists() else None


def load_history(db_path, merge_map):
    """(descrizione normalizzata -> categoria) dallo storico gia' etichettato.

    Se la stessa descrizione compare con categorie diverse vince quella usata
    piu' spesso: e' l'unico criterio deterministico e riproducibile.
    """
    conn = sqlite3.connect(db_path)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    query = """
        SELECT t.ZDESC2, cat.ZNAME2, par.ZNAME2
        FROM ZSYNCOBJECT t
        JOIN ZCATEGORYASSIGMENT ca ON ca.ZTRANSACTION = t.Z_PK
        JOIN ZSYNCOBJECT cat ON cat.Z_PK = ca.ZCATEGORY AND cat.Z_ENT = 19
        LEFT JOIN ZSYNCOBJECT par ON par.Z_PK = cat.ZPARENTCATEGORY
        WHERE t.Z_ENT IN (37, 47) AND t.ZDESC2 IS NOT NULL AND t.ZDESC2 != ''
    """
    votes = defaultdict(Counter)
    ignored = 0
    for desc, name, parent in conn.execute(query):
        category = canonical_category(name, parent, merge_map)
        if category == IGNORE:
            # Categoria marcata come cassetto dei rifiuti: non va imparata,
            # altrimenti insegna al motore a buttarci dentro roba nuova.
            ignored += 1
            continue
        key = normalize(anonymize(desc))
        if key and category:
            votes[key][category] += 1
    conn.close()
    if ignored:
        print(f"  {ignored} righe ignorate (categorie marcate IGNORA)")
    return {key: counts.most_common(1)[0][0] for key, counts in votes.items()}


def canonical_category(name, parent, merge_map):
    """Nome categoria ripulito e passato per la mappa di merge dell'utente."""
    def clean(part):
        part = unicodedata.normalize("NFC", str(part)).replace("\xa0", " ")
        part = re.sub(r"\s*\(Duplica\)\s*$", "", part)
        return re.sub(r"\s+", " ", part).strip()

    full = f"{clean(parent)} > {clean(name)}" if parent else clean(name)
    return merge_map.get(full, full)


def load_merge_map(path):
    """categorie_merge.csv: colonna 1 attuale, colonna 3 finale."""
    if not path.exists():
        return {}
    mapping = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            current = (row.get("categoria_attuale") or "").strip()
            final = (row.get("categoria_finale") or "").strip()
            if current and final and current != final:
                mapping[current] = final
    if mapping:
        print(f"  {len(mapping)} merge di categoria applicati da categorie_merge.csv")
    return mapping


def transaction_id(row, seen):
    """Identificativo stabile di una transazione, dai suoi valori ORIGINALI.

    Deve restare lo stesso quando la pipeline rigira sullo stesso export,
    altrimenti ogni correzione manuale si stacca dalla sua riga. Per questo
    non entra nel calcolo nulla che l'utente possa modificare dopo.

    Il suffisso distingue i duplicati esatti: due caffe' uguali nello stesso
    giorno sono due transazioni diverse, non la stessa contata due volte.
    """
    raw = "|".join((str(row.get("Conto", "")), str(row.get("Data", "")),
                    f"{row.get('Importo', 0):.2f}",
                    str(row.get("Descrizione", ""))))
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    seen[digest] += 1
    return f"{digest}#{seen[digest]}"


def assign_ids(rows):
    """Assegna l'ID a ogni riga. Va fatto PRIMA di applicare le correzioni."""
    seen = Counter()
    for row in rows:
        row["ID"] = transaction_id(row, seen)
    return rows


def load_corrections(path):
    """correzioni.csv: id -> {campo: valore}, per importi e date sbagliati.

    Separato da override.csv perche' cambia i fatti della transazione, non la
    sua interpretazione. Applicato dopo l'assegnazione degli ID, cosi' anche
    correggendo l'importo la riga resta la stessa.
    """
    if not path.exists():
        return {}
    fixes = defaultdict(dict)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            key = (row.get("id") or "").strip()
            field = (row.get("campo") or "").strip().capitalize()
            value = (row.get("valore") or "").strip()
            if key and field in ("Importo", "Data", "Descrizione") and value:
                fixes[key][field] = value
    if fixes:
        print(f"  {len(fixes)} transazioni corrette da correzioni.csv")
    return dict(fixes)


def apply_corrections(rows, fixes):
    """Sovrascrive i campi corretti a mano. L'ID non cambia mai."""
    if not fixes:
        return rows
    for row in rows:
        for field, value in fixes.get(row["ID"], {}).items():
            row[field] = (parse_amount(value) if field == "Importo"
                          else parse_date(value) if field == "Data" else value)
    return rows


def load_overrides(path):
    """override.csv: categoria decisa a mano per una singola transazione.

    Accetta due modi di identificare la riga: l'ID (quello che scrive
    l'interfaccia) oppure la coppia data+importo, che resta comoda per
    aggiungere una riga a mano senza dover cercare un ID.
    Le righe con categoria vuota sono promemoria ancora da compilare.
    """
    if not path.exists():
        return {}
    overrides, pending = {}, 0
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            key = (row.get("id") or "").strip()
            date = (row.get("data") or "").strip()
            category = (row.get("categoria") or "").strip()
            if not key and not date:
                continue
            if not category:
                pending += 1
                continue
            overrides[key or (date, round(parse_amount(row.get("importo")), 2))] = category
    if overrides:
        print(f"  {len(overrides)} override manuali da override.csv")
    if pending:
        print(f"  {pending} righe di override.csv ancora senza categoria")
    return overrides


def override_for(row, overrides):
    """L'ID vince sulla coppia data+importo, che puo' colpire piu' righe."""
    return (overrides.get(row["ID"])
            or overrides.get((row["Data"], round(row["Importo"], 2))))


def load_merchants(path):
    """merchant.csv: regex -> nome canonico del negozio."""
    if not path.exists():
        return []
    merchants = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            pattern = (row.get("pattern") or "").strip()
            name = (row.get("merchant") or "").strip()
            if not pattern or not name:
                continue
            try:
                merchants.append((re.compile(pattern, re.IGNORECASE), name))
            except re.error as exc:
                print(f"  merchant ignorato, regex non valida {pattern!r}: {exc}")
    if merchants:
        print(f"  {len(merchants)} merchant canonici da merchant.csv")
    return merchants


def canonical_merchant(description, merchants):
    """Nome del negozio, unificando le grafie diverse dello stesso posto.

    Senza questo passo lo stesso supermercato conta come decine di negozi
    distinti (Iper Rossetto compare con 56 grafie) e sparisce da ogni
    classifica di spesa.
    """
    for pattern, name in merchants:
        if pattern.search(description):
            return name
    # Ripiego: le prime due parole significative. Non unifica le grafie, ma
    # rende visibile un negozio nuovo che vale la pena aggiungere al file.
    significant = [
        word for word in normalize(description).split()
        if len(word) > 2 and word not in STOPWORDS and not word.isdigit()
    ]
    return " ".join(significant[:2]).title() if significant else ""


def load_natura(path):
    """natura.csv: categoria -> natura (quanto e' comprimibile quella spesa)."""
    if not path.exists():
        return {}
    natura = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            category = (row.get("categoria") or "").strip()
            kind = (row.get("natura") or "").strip()
            if category and kind:
                natura[category] = kind
    if natura:
        print(f"  {len(natura)} categorie mappate su natura.csv")
    return natura


def load_rules(path):
    """regole.csv: pattern regex -> categoria. Vince sull'LLM, non sullo storico."""
    if not path.exists():
        return []
    rules = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            pattern = (row.get("pattern") or "").strip()
            category = (row.get("categoria") or "").strip()
            if not pattern or not category:
                continue
            try:
                rules.append((re.compile(pattern, re.IGNORECASE), category))
            except re.error as exc:
                print(f"  regola ignorata, regex non valida {pattern!r}: {exc}")
    if rules:
        print(f"  {len(rules)} regole caricate da regole.csv")
    return rules


# --------------------------------------------------------------------------
# categorizzazione
# --------------------------------------------------------------------------

class Categorizer:
    def __init__(self, history, rules, categories, cache_path, use_llm=True):
        self.history = history
        self.rules = rules
        self.categories = sorted(categories)
        self.cache_path = cache_path
        self.use_llm = use_llm
        self.cache = json.loads(cache_path.read_text(encoding="utf-8")) \
            if cache_path.exists() else {}
        self.llm_available = None
        self.stats = Counter()
        # Indice token -> descrizioni storiche, per non scorrere tutto lo
        # storico a ogni riga.
        self.index = defaultdict(list)
        self.history_tokens = {}
        # Voto per token: quante volte ogni token compare in ogni categoria.
        # Serve per le descrizioni che non somigliano a nulla nel loro insieme
        # ma contengono una parola decisiva ("pizza", "farmacia", "enel").
        self.token_votes = defaultdict(Counter)
        self.category_tokens = Counter()
        self.prior = Counter()
        vocabulary = set()
        for key, category in history.items():
            token_set = tokens(key)
            if not token_set:
                continue
            self.history_tokens[key] = token_set
            self.prior[category] += 1
            for token in token_set:
                self.index[token].append(key)
                self.token_votes[token][category] += 1
                self.category_tokens[category] += 1
                vocabulary.add(token)
        self.vocabulary_size = len(vocabulary)
        self.total_examples = sum(self.prior.values())

    def categorize(self, description):
        """Ritorna (categoria, origine, confidenza)."""
        key = normalize(description)
        if not key:
            self.stats["vuota"] += 1
            return "", "vuota", 0.0

        # I giroconti vanno riconosciuti prima di tutto: se una vecchia
        # etichetta manuale li chiama "spesa", il totale delle uscite mente.
        for pattern, category in self.rules:
            if category == TRANSFER and pattern.search(description):
                self.stats["giroconto"] += 1
                return category, "giroconto", 1.0

        if key in self.history:
            self.stats["storico-esatto"] += 1
            return self.history[key], "storico-esatto", 1.0

        match, score = self._best_history_match(key)
        if match and score >= JACCARD_MIN:
            self.stats["storico-simile"] += 1
            return self.history[match], "storico-simile", score

        for pattern, category in self.rules:
            if pattern.search(description):
                self.stats["regola"] += 1
                return category, "regola", 1.0

        category, margin = self._token_vote(key)
        if category and margin >= MARGIN_MIN:
            self.stats["voto-token"] += 1
            # Il margine e' illimitato verso l'alto: comprimilo in 0.5-0.95
            # cosi' resta sotto ai match esatti e sopra al residuo.
            return category, "voto-token", min(0.95, 0.5 + margin / 20)

        if key in self.cache:
            self.stats["cache"] += 1
            return self.cache[key], "cache", 0.5

        if self.use_llm:
            asked = self.stats["llm"] + self.stats["llm-vuoto"]
            if asked and asked % 10 == 0:
                print(f"  ...{asked} descrizioni chieste al modello")
            category = self._ask_llm(description)
            if category:
                self.cache[key] = category
                self.stats["llm"] += 1
                return category, "llm", 0.5

        if self.use_llm and self.llm_available:
            self.stats["llm-vuoto"] += 1
        self.stats["non-categorizzata"] += 1
        return "", "non-categorizzata", 0.0

    def _best_history_match(self, key):
        """Miglior match sullo storico per indice di Jaccard sui token."""
        query = tokens(key)
        if not query:
            return None, 0.0
        candidates = Counter()
        for token in query:
            for candidate in self.index.get(token, ()):
                candidates[candidate] += 1
        best, best_score = None, 0.0
        for candidate, _ in candidates.most_common(50):
            other = self.history_tokens[candidate]
            union = query | other
            if not union:
                continue
            score = len(query & other) / len(union)
            # A parita' di punteggio vince la descrizione piu' corta: e' la
            # piu' generica, quindi la piu' probabile come regola.
            if score > best_score or (score == best_score and best
                                      and len(candidate) < len(best)):
                best, best_score = candidate, score
        return best, best_score

    def _token_vote(self, key):
        """Naive Bayes sui token. Ritorna (categoria, distacco sulla seconda).

        Il distacco e' la differenza di log-verosimiglianza fra la prima e la
        seconda categoria: piccolo significa che il modello e' indeciso, e in
        quel caso e' meglio passare la palla al livello successivo.
        """
        present = [t for t in tokens(key) if t in self.token_votes]
        if not present or not self.total_examples:
            return None, 0.0
        scored = []
        for category in self.categories:
            score = math.log((self.prior[category] + 0.5) / self.total_examples)
            denominator = self.category_tokens[category] + ALPHA * self.vocabulary_size
            for token in present:
                score += math.log((self.token_votes[token][category] + ALPHA)
                                  / denominator)
            scored.append((score, category))
        scored.sort(reverse=True)
        if len(scored) < 2:
            return scored[0][1], 99.0
        return scored[0][1], scored[0][0] - scored[1][0]

    def _ask_llm(self, description):
        if self.llm_available is False:
            return ""
        prompt = (
            "Sei un classificatore di transazioni bancarie italiane.\n"
            "Scegli UNA categoria dalla lista, copiandola esattamente.\n"
            "Se nessuna e' adatta rispondi esattamente: NESSUNA\n"
            "Rispondi solo con la categoria, senza spiegazioni.\n\n"
            "Categorie disponibili:\n"
            + "\n".join(f"- {c}" for c in self.categories)
            + f"\n\nTransazione: {description}\nCategoria:"
        )
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 32},
        }).encode()
        request = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                answer = json.loads(response.read())["response"]
            self.llm_available = True
        except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
            if self.llm_available is None:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                    print(f"  Ollama risponde ma non ha il modello {OLLAMA_MODEL}.")
                    print(f"  Scaricalo con: ollama pull {OLLAMA_MODEL}")
                else:
                    print(f"  Ollama non raggiungibile ({exc}); salto il passo LLM.")
                    print("  Avvialo con: ollama serve")
            self.llm_available = False
            return ""

        answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
        answer = answer.strip().strip('"').strip()
        if answer in self.categories:
            return answer
        # Il modello a volte parafrasa: accetta solo se combacia normalizzato.
        target = normalize(answer)
        for category in self.categories:
            if normalize(category) == target:
                return category
        return ""

    def save_cache(self):
        # Via file temporaneo: ricostruire questa cache costa una chiamata al
        # modello per descrizione, cioe' minuti. Troncarla a meta' per un
        # arresto improvviso sarebbe il danno piu' caro del progetto.
        body = json.dumps(self.cache, ensure_ascii=False, indent=1,
                          sort_keys=True)
        temp = self.cache_path.with_suffix(".json.tmp")
        try:
            temp.write_text(body, encoding="utf-8")
            os.replace(temp, self.cache_path)
        finally:
            temp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
def load_history_transactions(db_path):
    """Le transazioni grezze di MoneyWiz, nello stesso formato degli export.

    Serve per avere una dashboard popolata prima ancora di scaricare gli
    export dalle banche, e per verificare le regole su dati veri.
    """
    conn = sqlite3.connect(db_path)
    conn.text_factory = lambda b: b.decode("utf-8", "replace")
    epoch = datetime(2001, 1, 1)
    rows = []
    query = """
        SELECT t.ZDESC2, t.ZAMOUNT1, t.ZDATE1, acc.ZNAME
        FROM ZSYNCOBJECT t
        LEFT JOIN ZSYNCOBJECT acc ON acc.Z_PK = t.ZACCOUNT2
        WHERE t.Z_ENT IN (37, 47) AND t.ZDATE1 IS NOT NULL
    """
    for desc, amount, stamp, account in conn.execute(query):
        description = anonymize(desc)
        if not description or not amount:
            continue
        rows.append({
            "Data": (epoch + timedelta(seconds=stamp)).strftime("%Y-%m-%d"),
            "Descrizione": description,
            "Importo": round(float(amount), 2),
            "Conto": account or "MoneyWiz",
        })
    conn.close()
    print(f"  {len(rows)} transazioni dallo storico MoneyWiz")
    return rows


# --------------------------------------------------------------------------
# lettura degli export
# --------------------------------------------------------------------------

def read_table(path):
    """Legge un export bancario provando le combinazioni piu' comuni."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    attempts = [
        {"sep": ";", "encoding": "utf-8-sig"},
        {"sep": ",", "encoding": "utf-8-sig"},
        {"sep": "\t", "encoding": "utf-8-sig"},
        {"sep": ";", "encoding": "latin-1"},
        {"sep": ",", "encoding": "latin-1"},
    ]
    for options in attempts:
        for skiprows in (0, 1):
            try:
                frame = pd.read_csv(path, skiprows=skiprows,
                                    dtype=str, **options)
            except Exception:
                continue
            if len(frame.columns) >= 3 and len(frame) > 0:
                return frame
    try:
        return pd.read_csv(path, sep=None, engine="python",
                           encoding="utf-8-sig", dtype=str)
    except Exception:
        return None


def find_columns(frame):
    """Individua le colonne data, descrizione e importo."""
    date_col = desc_col = amount_col = None
    debit_col = credit_col = None
    for column in frame.columns:
        name = normalize(column)
        if not date_col and re.search(r"\bdata\b|\bdate\b", name):
            date_col = column
        elif not desc_col and re.search(r"descrizione|operazione|causale|payee|memo", name):
            desc_col = column
        elif not amount_col and re.search(r"\bimporto\b|\bamount\b|\bcosto\b", name):
            amount_col = column
        elif not debit_col and re.search(r"dare|uscite|addebiti", name):
            debit_col = column
        elif not credit_col and re.search(r"avere|entrate|accrediti", name):
            credit_col = column
    return date_col, desc_col, amount_col, debit_col, credit_col


def load_transactions(path):
    """Righe grezze (data, descrizione, importo) da un singolo export."""
    frame = read_table(path)
    if frame is None or frame.empty:
        print(f"  {path.name}: non leggibile, saltato")
        return []
    frame = frame.dropna(how="all").dropna(axis=1, how="all")

    date_col, desc_col, amount_col, debit_col, credit_col = find_columns(frame)
    if not desc_col or not (amount_col or debit_col or credit_col):
        print(f"  {path.name}: colonne non riconosciute {list(frame.columns)}, saltato")
        return []
    if not date_col:
        date_col = frame.columns[0]

    rows = []
    for _, row in frame.iterrows():
        description = anonymize(row.get(desc_col))
        if not description or normalize(description) in ("nan", "bilancio totale"):
            continue
        if amount_col:
            amount = parse_amount(row.get(amount_col))
        else:
            # Colonne dare/avere separate: l'uscita e' negativa.
            debit = parse_amount(row.get(debit_col)) if debit_col else 0.0
            credit = parse_amount(row.get(credit_col)) if credit_col else 0.0
            amount = credit - abs(debit)
        if amount == 0.0:
            continue
        rows.append({
            "Data": parse_date(row.get(date_col)),
            "Descrizione": description,
            "Importo": round(amount, 2),
            "Conto": path.stem,
        })
    print(f"  {path.name}: {len(rows)} transazioni")
    return rows


def drop_internal_transfers(rows, days=3, tolerance=0.01):
    """Rimuove le coppie +X / -X fra conti propri, che raddoppiano i totali.

    Due righe si annullano se hanno importo opposto, conti diversi e date
    entro pochi giorni. Senza questo passo un giroconto conta due volte.
    """
    def as_date(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    negatives = defaultdict(list)
    for index, row in enumerate(rows):
        if row["Importo"] < 0:
            negatives[round(abs(row["Importo"]), 2)].append(index)

    matched = set()
    for index, row in enumerate(rows):
        if row["Importo"] <= 0 or index in matched:
            continue
        date = as_date(row["Data"])
        for candidate in negatives.get(round(row["Importo"], 2), ()):
            if candidate in matched or rows[candidate]["Conto"] == row["Conto"]:
                continue
            other = as_date(rows[candidate]["Data"])
            if date and other and abs((date - other).days) > days:
                continue
            matched.update({index, candidate})
            break

    if matched:
        print(f"  {len(matched)} righe rimosse: giroconti fra conti propri "
              f"({len(matched) // 2} coppie)")
    return [row for i, row in enumerate(rows) if i not in matched]


# --------------------------------------------------------------------------
# verifica
# --------------------------------------------------------------------------

def selftest():
    """Controlli sulle parti che, se si rompono, sbagliano in silenzio.

    Anonimizzazione e parsing degli importi: la prima e' una promessa di
    privacy, il secondo produce totali sbagliati ma plausibili.
    """
    # L'anonimizzazione deve togliere i dati sensibili...
    for sensitive, sample in [
        ("IT60X0542811101000000123456",
         "BONIFICO A IT60X0542811101000000123456 MARIO ROSSI"),
        ("1234", "PAGAMENTO POS CARTA **** 1234 ESSELUNGA"),
        ("4571", "ACQUISTO CARTA xxxx-4571 COOP"),
        ("02INTER20250502HSRT1382833170",
         "SDD 02INTER20250502HSRT1382833170 ENEL"),
        ("987654321098", "RIF 987654321098 PAGAMENTO"),
    ]:
        assert sensitive not in anonymize(sample), \
            f"anonimizzazione fallita, resta {sensitive!r} in {sample!r}"

    # ...senza toccare il nome del merchant, che serve a categorizzare.
    for merchant, sample in [
        ("ESSELUNGA", "PAGAMENTO POS CARTA **** 1234 ESSELUNGA"),
        ("ENEL", "SDD 02INTER20250502HSRT1382833170 ENEL"),
        ("Q8", "PAGAMENTO POS Q8 STAZIONE A4"),
        ("H&M", "ACQUISTO H&M MILANO"),
        ("COOP", "ACQUISTO CARTA xxxx-4571 COOP"),
    ]:
        assert merchant in anonymize(sample), \
            f"anonimizzazione troppo aggressiva, persa {merchant!r}"

    # Formati numerici europei e anglosassoni non devono confondersi.
    for raw, expected in [
        ("1.234,56", 1234.56), ("1,234.56", 1234.56), ("-84,30", -84.30),
        ("84.30", 84.30), ("1.234", 1234.0), ("12,5", 12.5),
        ("€ 1.500,00", 1500.0), ("", 0.0),
    ]:
        got = parse_amount(raw)
        assert abs(got - expected) < 0.005, f"{raw!r}: atteso {expected}, ottenuto {got}"

    # Un giroconto fra conti propri non deve contare due volte.
    pair = [
        {"Data": "2026-01-15", "Descrizione": "giroconto", "Importo": -200.0, "Conto": "a"},
        {"Data": "2026-01-16", "Descrizione": "ricarica", "Importo": 200.0, "Conto": "b"},
        {"Data": "2026-01-16", "Descrizione": "spesa", "Importo": -200.0, "Conto": "a"},
    ]
    assert len(drop_internal_transfers(pair)) == 1, "giroconti non rimossi"
    # Due movimenti opposti sullo STESSO conto non sono un giroconto.
    same = [
        {"Data": "2026-01-15", "Descrizione": "x", "Importo": -50.0, "Conto": "a"},
        {"Data": "2026-01-15", "Descrizione": "y", "Importo": 50.0, "Conto": "a"},
    ]
    assert len(drop_internal_transfers(same)) == 2, "rimossi movimenti dello stesso conto"

    # Grafie diverse dello stesso negozio devono dare lo stesso merchant,
    # altrimenti la classifica di spesa li conta come negozi distinti.
    shop = [(re.compile(r"(?i)rossetto"), "Iper Rossetto")]
    for spelling in ["IPER ROSSETTO SONA VR", "Rossetto", "iper rossetto",
                     "Rossetto 29.10", "PAGAMENTO POS IPER ROSSETTO"]:
        got = canonical_merchant(spelling, shop)
        assert got == "Iper Rossetto", f"{spelling!r} -> {got!r}"
    # Senza regola, il ripiego deve comunque dare un nome stabile e non vuoto.
    assert canonical_merchant("PAGAMENTO POS NEGOZIO NUOVO SRL", []) != ""

    # L'override deve battere lo storico: e' una decisione presa a mano.
    forced = {("2025-12-30", -3829.0): "Casa > Ristrutturazione"}
    assert forced.get(("2025-12-30", round(-3829.004, 2))) == "Casa > Ristrutturazione",         "chiave override sensibile agli arrotondamenti"
    assert forced.get(("2025-12-30", -3828.0)) is None, "override troppo permissivo"

    # Lo stesso export deve rigenerare gli stessi ID, altrimenti ogni
    # correzione manuale si stacca dalla sua riga alla prima riesecuzione.
    sample = [
        {"Conto": "intesa", "Data": "2026-01-12", "Importo": -84.30,
         "Descrizione": "ESSELUNGA"},
        {"Conto": "intesa", "Data": "2026-01-12", "Importo": -84.30,
         "Descrizione": "ESSELUNGA"},
        {"Conto": "fineco", "Data": "2026-01-12", "Importo": -84.30,
         "Descrizione": "ESSELUNGA"},
    ]
    first = [r["ID"] for r in assign_ids([dict(r) for r in sample])]
    again = [r["ID"] for r in assign_ids([dict(r) for r in sample])]
    assert first == again, f"ID non deterministici: {first} != {again}"
    assert first[0] != first[1], "due righe identiche hanno lo stesso ID"
    assert first[0] != first[2], "conti diversi, stesso ID"

    # Correggere un importo non deve cambiare l'ID della transazione.
    rows = assign_ids([dict(sample[0])])
    before = rows[0]["ID"]
    apply_corrections(rows, {before: {"Importo": "-90,00"}})
    assert rows[0]["ID"] == before, "l'ID e' cambiato dopo la correzione"
    assert rows[0]["Importo"] == -90.0, f"correzione non applicata: {rows[0]}"

    # L'override per ID deve battere quello per data+importo.
    row = {"ID": "abc#1", "Data": "2026-01-12", "Importo": -84.30}
    both = {"abc#1": "Casa > Bollette", ("2026-01-12", -84.30): "Shopping"}
    assert override_for(row, both) == "Casa > Bollette"
    assert override_for(row, {("2026-01-12", -84.30): "Shopping"}) == "Shopping"

    print("selftest: ok")


def selfcheck(history, rules, categories):
    """Accuratezza su holdout: 20% dello storico tenuto fuori dall'indice.

    Non e' una stima del mondo reale (le descrizioni dello storico sono gia'
    normalizzate da MoneyWiz), ma se scende molto vuol dire che il matching
    e' rotto.
    """
    keys = sorted(history)
    if len(keys) < 50:
        print("storico troppo piccolo per la verifica")
        return
    holdout = {k: history[k] for i, k in enumerate(keys) if i % 5 == 0}
    train = {k: v for k, v in history.items() if k not in holdout}

    categorizer = Categorizer(train, rules, categories,
                              Path("__selfcheck_cache.json"), use_llm=False)
    hits = Counter()
    seen = Counter()
    for desc, expected in holdout.items():
        predicted, source, _ = categorizer.categorize(desc)
        seen[source] += 1
        hits[source] += predicted == expected

    total = len(holdout)
    correct = sum(hits.values())
    for source, count in seen.most_common():
        print(f"  {source:<20} {count:>4} righe, {hits[source]:>4} corrette "
              f"({hits[source] / count:.0%})")
    baseline = max(Counter(history.values()).values()) / len(history)
    print(f"verifica: {correct}/{total} corrette sul 20% tenuto fuori "
          f"({correct / total:.0%}), baseline maggioranza {baseline:.0%}")
    assert correct / total > 0.55, (
        f"matching peggiorato: solo {correct / total:.0%} di accuratezza"
    )


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# esecuzione
# --------------------------------------------------------------------------

# File che vivono nella cartella ma non sono estratti conto. Senza questa lista
# la pipeline proverebbe a leggere come export i propri stessi output.
GENERATED = {
    "consolidato.csv", "da_rivedere.csv", "dashboard.xlsx", "dashboard.html",
    "categorie_merge.csv", "regole.csv", "merchant.csv", "override.csv",
    "natura.csv", "correzioni.csv", "categorie_cache.json",
    "dashboard_layout.json",
}


def load_config(folder):
    """Tutto cio' che guida la categorizzazione, piu' lo storico MoneyWiz."""
    folder = Path(folder)
    merge_map = load_merge_map(folder / "categorie_merge.csv")
    config = {
        "merge_map": merge_map,
        "rules": load_rules(folder / "regole.csv"),
        "overrides": load_overrides(folder / "override.csv"),
        "merchants": load_merchants(folder / "merchant.csv"),
        "natura": load_natura(folder / "natura.csv"),
        "corrections": load_corrections(folder / "correzioni.csv"),
    }
    # Il sqlite estratto pesa ~5 MB: tienilo fuori dalla cartella iCloud,
    # altrimenti viene risincronizzato a ogni esecuzione.
    config["db_path"] = extract_moneywiz_db(
        folder, Path(tempfile.gettempdir()) / "bilancio_moneywiz")
    config["history"] = (load_history(config["db_path"], merge_map)
                         if config["db_path"] else {})
    if config["history"]:
        print(f"  {len(config['history'])} descrizioni gia' etichettate")
    else:
        print("  nessuno storico MoneyWiz trovato: si usano solo regole e LLM")

    config["categories"] = sorted(
        set(config["history"].values())
        | {c for _, c in config["rules"]}
        | set(config["overrides"].values()))
    print(f"  {len(config['categories'])} categorie in uso")
    return config


def read_sources(folder, config, output, from_history):
    """Le transazioni grezze: dallo storico oppure dagli export nella cartella."""
    if from_history:
        print("STORICO MoneyWiz")
        return (load_history_transactions(config["db_path"])
                if config["db_path"] else [])

    print("EXPORT")
    skip = GENERATED | {output.lower()}
    rows = []
    for path in sorted(Path(folder).glob("*")):
        if path.suffix.lower() not in (".csv", ".xlsx", ".xls"):
            continue
        if path.name.lower() in skip or "report" in path.name.lower():
            continue
        rows.extend(load_transactions(path))
    return rows


def run(folder, use_llm=True, from_history=False, output="consolidato.csv",
        make_dashboard=True, config=None):
    """L'intera pipeline, chiamabile da codice. Ritorna il DataFrame finale.

    Estratta da main() perche' il server la richiama a ogni modifica: farlo
    con un sottoprocesso perderebbe sia il risultato sia i messaggi.
    """
    folder = Path(folder)
    print("categorie e storico")
    if config is None:
        config = load_config(folder)
    if not config["categories"]:
        raise RuntimeError("nessuna categoria disponibile: serve un backup "
                           "MoneyWiz in backup/ oppure un regole.csv")

    rows = read_sources(folder, config, output, from_history)
    if not rows:
        raise RuntimeError("nessuna transazione trovata: metti gli export "
                           "nella cartella, oppure usa --da-storico")

    # L'ordine conta: prima gli ID sui valori originali, poi le correzioni.
    # Al contrario, correggere un importo cambierebbe l'ID della sua riga e
    # la correzione si staccherebbe dalla transazione al giro successivo.
    assign_ids(rows)
    apply_corrections(rows, config["corrections"])
    rows = drop_internal_transfers(rows)

    print("")
    print("categorizzazione")
    categorizer = Categorizer(config["history"], config["rules"],
                              config["categories"],
                              folder / "categorie_cache.json", use_llm=use_llm)
    for row in rows:
        forced = override_for(row, config["overrides"])
        if forced:
            category, source, score = forced, "override", 1.0
            categorizer.stats["override"] += 1
        else:
            category, source, score = categorizer.categorize(row["Descrizione"])
        row["Categoria"] = category
        row["Merchant"] = canonical_merchant(row["Descrizione"],
                                             config["merchants"])
        row["Natura"] = config["natura"].get(category, "Da classificare")
        row["Origine"] = source
        row["Confidenza"] = round(score, 2)
    categorizer.save_cache()

    for source, count in categorizer.stats.most_common():
        print(f"  {count:>6}  {source}")

    columns = ["ID", "Data", "Descrizione", "Merchant", "Importo", "Conto",
               "Natura", "Categoria", "Origine", "Confidenza"]
    frame = pd.DataFrame(rows)[columns].sort_values(["Data", "Descrizione"])
    frame.to_csv(folder / output, index=False, sep=";", encoding="utf-8-sig")

    review = frame[frame["Confidenza"] < 1.0]
    review.to_csv(folder / "da_rivedere.csv", index=False, sep=";",
                  encoding="utf-8-sig")

    # I giroconti non sono ne' entrate ne' uscite: contarli falsa entrambi i
    # totali e fa sembrare che si spenda piu' di quanto si spende.
    moves = frame[frame["Natura"] == "Non spesa"]
    real = frame[frame["Natura"] != "Non spesa"]
    income = real[real.Importo > 0].Importo.sum()
    spent = real[real.Importo < 0].Importo.sum()

    print("")
    print(f"{len(frame)} transazioni -> {output}")
    print(f"{len(review)} da controllare -> da_rivedere.csv")
    print(f"entrate   {income:>12,.2f}")
    print(f"uscite    {spent:>12,.2f}")
    print(f"risparmio {income + spent:>12,.2f}")
    if len(moves):
        print(f"({len(moves)} giroconti per {moves.Importo.sum():,.2f} "
              "esclusi dai totali)")

    if dashboard and make_dashboard:
        for written in dashboard.generate(frame, folder):
            print(f"scritto {written.name}")
    return frame


def main():
    parser = argparse.ArgumentParser(
        description="Consolida ed etichetta le transazioni bancarie.")
    parser.add_argument("-f", "--folder", default=str(Path(__file__).parent),
                        help="cartella con gli export (default: questa)")
    parser.add_argument("-o", "--output", default="consolidato.csv")
    parser.add_argument("--no-llm", action="store_true",
                        help="salta il passo Ollama, lascia vuoto il residuo")
    parser.add_argument("--selfcheck", action="store_true",
                        help="misura l'accuratezza sullo storico ed esce")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="non rigenerare dashboard.html e .xlsx")
    parser.add_argument("--da-storico", action="store_true",
                        help="usa le transazioni MoneyWiz invece degli export")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        sys.exit(f"cartella inesistente: {folder}")

    if args.selfcheck:
        print("categorie e storico")
        config = load_config(folder)
        selftest()
        selfcheck(config["history"], config["rules"], config["categories"])
        return

    try:
        run(folder, use_llm=not args.no_llm, from_history=args.da_storico,
            output=args.output, make_dashboard=not args.no_dashboard)
    except RuntimeError as exc:
        sys.exit(str(exc))



if __name__ == "__main__":
    main()
