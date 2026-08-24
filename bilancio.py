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
import time
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


def load_accounts(path):
    """conti.csv: regex sul nome del file -> nome del conto.

    Senza questa mappa il "conto" e' il nome del file, quindi due export dello
    stesso conto in mesi diversi diventano due conti. Non e' un dettaglio
    estetico: drop_internal_transfers riconosce i giroconti proprio guardando
    che i conti siano diversi, e con conti fasulli sbaglia.
    """
    if not path.exists():
        return []
    accounts = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            pattern = (row.get("pattern") or "").strip()
            name = (row.get("conto") or "").strip()
            if not pattern or not name:
                continue
            try:
                accounts.append((re.compile(pattern, re.IGNORECASE), name))
            except re.error as exc:
                print(f"  conto ignorato, regex non valida {pattern!r}: {exc}")
    if accounts:
        print(f"  {len(accounts)} conti riconosciuti da conti.csv")
    return accounts


def account_of(filename, accounts):
    """Il conto a cui appartiene un export, dal suo nome."""
    for pattern, name in accounts:
        if pattern.search(filename):
            return name
    # Ripiego: quello che viene prima della prima cifra o del primo separatore.
    # "intesa-2026-01.csv" e "intesa_gennaio.csv" danno entrambi "Intesa".
    base = re.split(r"[-_ ]?\d|[-_]", Path(filename).stem, maxsplit=1)[0]
    return base.strip().title() or Path(filename).stem


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
        started = time.monotonic()
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

        elapsed = time.monotonic() - started
        # Il ragionamento del modello non serve alla decisione, ma vederlo
        # aiuta a capire perche' ha risposto cosi'.
        thinking = " ".join(re.findall(r"<think>(.*?)</think>", answer,
                                       flags=re.DOTALL)).strip()
        answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL)
        answer = answer.strip().strip('"').strip()

        def report(outcome, detail=""):
            print(f"LLM {elapsed:5.1f}s | {description[:64]}")
            if thinking:
                print(f"LLM       ragiona: {thinking[:180]}")
            print(f"LLM       risponde: {answer[:80]!r} -> {outcome}{detail}")

        if answer in self.categories:
            report("accettata")
            return answer
        # Il modello a volte parafrasa: accetta solo se combacia normalizzato.
        target = normalize(answer)
        for category in self.categories:
            if normalize(category) == target:
                report("accettata", f" (normalizzata in {category!r})")
                return category
        report("SCARTATA", " (non e' una categoria esistente)"
               if answer.upper() != "NESSUNA" else " (il modello si e' astenuto)")
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


def drop_shared_halves(rows, shares, days=1):
    """Toglie dallo storico le meta' di spese gia' presenti in un export condiviso.

    MoneyWiz importava ogni spesa Splitwise come le due quote, entrambe
    negative: "Eurospin -30,00" due volte per una spesa da 60. Misurato
    sull'export reale, l'84% delle righe condivise trova corrispondenza sulla
    quota, contro il 12% sul costo pieno.

    Si tolgono al massimo DUE righe per ogni riga condivisa: di piu'
    cancellerebbe spese distinte che per caso hanno lo stesso importo.
    """
    index = defaultdict(list)
    for position, row in enumerate(rows):
        index[(row.get("Data"), round(abs(row.get("Importo", 0)), 2))].append(position)

    offsets = [0]
    for distance in range(1, days + 1):
        offsets.extend((-distance, distance))

    removed = set()
    for day, share in shares:
        if not day or not share:
            continue
        taken = 0
        for offset in offsets:
            try:
                near = (datetime.strptime(day, "%Y-%m-%d")
                        + timedelta(days=offset)).strftime("%Y-%m-%d")
            except ValueError:
                continue
            for position in index.get((near, round(abs(share), 2)), []):
                if position in removed:
                    continue
                removed.add(position)
                taken += 1
                if taken == 2:
                    break
            if taken == 2:
                break
    return [row for position, row in enumerate(rows) if position not in removed]


def shared_shares(folder):
    """(data, quota) da tutti gli export condivisi presenti in export/.

    La quota e' il valore assoluto della colonna di una persona: in una
    divisione fra due le due coincidono, quindi ne basta una.
    """
    shares = []
    for path in all_exports(folder, include_pending=True):
        frame = read_table(path)
        if frame is None or frame.empty or not is_shared_export(frame):
            continue
        known = {c for c in find_columns(frame) if c}
        extra = [c for c in frame.columns if c not in known]
        quotas = pd.DataFrame({c: pd.to_numeric(frame[c], errors="coerce")
                               for c in extra}).dropna(axis=1, how="all")
        if quotas.empty or not len(quotas.columns):
            continue
        date_col = find_columns(frame)[0] or frame.columns[0]
        column = quotas.columns[0]
        for _, row in frame.iterrows():
            value = pd.to_numeric(row.get(column), errors="coerce")
            if pd.notna(value) and value:
                shares.append((parse_date(row.get(date_col)), abs(float(value))))
    return shares


def migrate_history(folder, db_path):
    """Scrive export/elaborati/storico-moneywiz.csv, una volta sola.

    Dopo questo passo lo storico MoneyWiz non e' piu' una sorgente di
    transazioni: resta il maestro delle categorie. Il backup non viene mai
    scritto, quindi l'operazione si annulla cancellando il file prodotto.
    """
    folder = Path(folder)
    if not db_path:
        print("nessun backup MoneyWiz in backup/: niente da migrare")
        return None

    rows = load_history_transactions(db_path)
    shares = shared_shares(folder)
    print(f"  {len(shares)} quote lette dagli export condivisi")
    kept = drop_shared_halves(rows, shares)

    uscite = sum(r["Importo"] for r in kept if r["Importo"] < 0)
    print(f"  {len(rows) - len(kept)} righe riconosciute come meta' condivise")
    print(f"  {len(kept)} righe superstiti: "
          f"{sum(1 for r in kept if r['Importo'] < 0)} uscite per {uscite:,.2f}, "
          f"{sum(1 for r in kept if r['Importo'] > 0)} entrate")

    target = folder / EXPORT_DIR / ARCHIVE_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / "storico-moneywiz.csv"
    pd.DataFrame(kept)[["Data", "Descrizione", "Importo", "Conto"]].to_csv(
        path, index=False, sep=";", encoding="utf-8-sig")
    print(f"  scritto {path}")
    print("  controlla i numeri: se non tornano, cancella il file e rilancia")
    return path


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


def is_shared_export(frame):
    """Vero se il file divide ogni spesa fra piu' persone.

    La firma e' numerica, non nominale: oltre alle colonne che find_columns()
    riconosce, ce ne sono altre numeriche la cui somma per riga fa zero. Chi
    anticipa ha un credito, gli altri un debito pari e contrario.

    Riconoscerlo dai nomi delle persone si romperebbe al primo cambio di nome
    o all'ingresso di un terzo. Dal nome del file non funziona affatto:
    Splitwise lo chiama "koala_<data>_export.csv".
    """
    known = {c for c in find_columns(frame) if c}
    extra = [c for c in frame.columns if c not in known]
    if not extra:
        return False
    quotas = pd.DataFrame({c: pd.to_numeric(frame[c], errors="coerce")
                           for c in extra}).dropna(axis=1, how="all")
    # Serve piu' di una colonna: una sola (un saldo progressivo) non e' una
    # divisione fra persone.
    if len(quotas.columns) < 2:
        return False
    balanced = quotas.fillna(0).sum(axis=1).abs() < 0.01
    return bool(len(balanced) and balanced.mean() >= 0.95)


# Come si riconosce un saldo fra le persone di un export condiviso. Servono
# entrambi i segnali: Splitwise non marca tutto (2023-12-08 "Pareggia tutti i
# bilanci" ha categoria "Generali") e la descrizione puo' cambiare formula.
SETTLEMENT_CATEGORY = "pagamento"
SETTLEMENT_WORDS = re.compile(r"ha pagato|pareggia.*bilanc", re.IGNORECASE)


def is_settlement(description, category=""):
    """Vero se la riga bilancia debiti fra le persone invece di essere spesa.

    NON si guarda l'importo. Un criterio basato sull'importo ("il costo vale
    una quota intera") colpirebbe 31 spese vere pagate al 100% da uno e
    attribuite al 100% all'altro: farmacia per Fabio, Netflix Michela,
    Colliri post operazione. Uno split 100/0 e' normale.
    """
    if str(category or "").strip().lower() == SETTLEMENT_CATEGORY:
        return True
    return bool(SETTLEMENT_WORDS.search(str(description or "")))


def load_transactions(path, account=None):
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

    # In un export condiviso l'importo e' il costo pieno, sempre positivo nel
    # file ma sempre un'uscita; le colonne persona sono saldi e non servono.
    shared = is_shared_export(frame)
    category_col = next((c for c in frame.columns
                         if normalize(c) == "categorie"), None)

    rows, settled = [], []
    for _, row in frame.iterrows():
        description = anonymize(row.get(desc_col))
        if not description or normalize(description) in ("nan", "bilancio totale"):
            continue
        if shared and is_settlement(description,
                                    row.get(category_col) if category_col else ""):
            settled.append((description,
                            parse_amount(row.get(amount_col)) if amount_col else 0.0))
            continue
        if amount_col:
            amount = parse_amount(row.get(amount_col))
        else:
            # Colonne dare/avere separate: l'uscita e' negativa.
            debit = parse_amount(row.get(debit_col)) if debit_col else 0.0
            credit = parse_amount(row.get(credit_col)) if credit_col else 0.0
            amount = credit - abs(debit)
        if shared:
            amount = -abs(amount)
        if amount == 0.0:
            continue
        rows.append({
            "Data": parse_date(row.get(date_col)),
            "Descrizione": description,
            "Importo": round(amount, 2),
            "Conto": account or path.stem,
            # Serve a distinguere i doppioni fra export diversi da due spese
            # identiche dentro lo stesso file.
            "Origine file": path.name,
        })

    print(f"  {path.name}: {len(rows)} transazioni")
    if settled:
        totale = sum(a for _, a in settled)
        print(f"    {len(settled)} saldi scartati per {totale:,.2f}: "
              f"{', '.join(d[:34] for d, _ in settled[:3])}")
    # Un estratto conto ha entrambi i segni. Se non li ha, o e' una lista di
    # spese o il parser ha sbagliato colonna: dirlo evita di scoprirlo dai
    # totali.
    if rows and not shared and all(r["Importo"] > 0 for r in rows):
        print(f"    attenzione: {path.name} ha solo importi positivi. "
              "Controlla che la colonna dell'importo sia quella giusta")
    return rows


def drop_cross_file_duplicates(rows):
    """Scarta le righe presenti in piu' export, tenendo la prima.

    Due estratti conto che si sovrappongono di periodo contengono le stesse
    transazioni: senza questo passo verrebbero contate due volte, e siccome
    l'ID include il conto nemmeno gli ID coinciderebbero.

    Dentro lo STESSO file i doppioni restano: due caffe' uguali lo stesso
    giorno sono due spese vere, non un errore di scarico.
    """
    seen = {}
    kept, dropped = [], Counter()
    for row in rows:
        key = (row.get("Data"), round(row.get("Importo", 0), 2),
               row.get("Descrizione"), row.get("Conto"))
        source = row.get("Origine file")
        first = seen.get(key)
        if first is not None and first != source:
            dropped[source] += 1
            continue
        if first is None:
            seen[key] = source
        kept.append(row)

    if dropped:
        total = sum(dropped.values())
        print(f"  {total} righe scartate: gia' presenti in un altro export")
        for source, count in dropped.most_common():
            print(f"    {count:>5} da {source}")
    return kept


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

    # Un export di spese condivise si riconosce da una firma numerica, non
    # dai nomi delle persone: oltre alle colonne standard ce ne sono altre
    # che si annullano riga per riga. Chi anticipa ha un credito, gli altri
    # un debito pari e contrario.
    condiviso = pd.DataFrame({
        "Data": ["2022-01-12", "2022-01-15"],
        "Descrizione": ["Eurospin", "Bolletta luce"],
        "Categorie": ["Generali", "Generali"],
        "Costo": ["60.00", "86.40"],
        "Valuta": ["EUR", "EUR"],
        "Tizio": ["30.00", "-43.20"],
        "Caio": ["-30.00", "43.20"],
    })
    assert is_shared_export(condiviso), "export condiviso non riconosciuto"

    # Un estratto conto normale non deve essere scambiato per condiviso.
    banca = pd.DataFrame({
        "Data": ["12/01/2026"],
        "Descrizione": ["PAGAMENTO POS ESSELUNGA"],
        "Importo": ["-84,30"],
    })
    assert not is_shared_export(banca), "estratto conto scambiato per condiviso"

    # Nemmeno uno con una colonna numerica in piu' (saldo progressivo): una
    # colonna sola non e' una divisione fra persone.
    con_saldo = pd.DataFrame({
        "Data": ["12/01/2026"],
        "Descrizione": ["PAGAMENTO POS ESSELUNGA"],
        "Importo": ["-84,30"],
        "Saldo": ["1250,00"],
    })
    assert not is_shared_export(con_saldo), "colonna saldo scambiata per quote"

    # Un saldo fra le due persone bilancia spese gia' tracciate: non e' una
    # transazione di questo bilancio.
    assert is_settlement("Fabio S. ha pagato Mikela b.", "Pagamento")
    assert is_settlement("Pareggia tutti i bilanci", "Generali"), \
        "Splitwise non marca tutti i saldi: serve anche la descrizione"
    assert is_settlement("Eurospin", "Pagamento"), "categoria Pagamento ignorata"

    # E soprattutto: una spesa pagata interamente da uno e attribuita
    # interamente all'altro NON e' un saldo. Sono 31 righe nel file vero, e
    # un criterio basato sull'importo le avrebbe cancellate in silenzio.
    for descrizione in ("farmacia per Fabio", "Netflix Michela",
                        "Colliri post operazione", "Regalo Mauro Fabio e genitori"):
        assert not is_settlement(descrizione, "Spese mediche"), \
            f"{descrizione!r} e' una spesa, non un saldo"

    # La migrazione toglie dallo storico le righe che sono le due meta' di una
    # spesa Splitwise: stessa data (+-1 giorno), importo pari alla quota, al
    # massimo due per riga condivisa.
    storico = [
        {"Data": "2022-01-12", "Descrizione": "Eurospin", "Importo": -30.0},
        {"Data": "2022-01-12", "Descrizione": "Eurospin", "Importo": -30.0},
        {"Data": "2022-01-12", "Descrizione": "Eurospin", "Importo": -30.0},
        {"Data": "2022-01-20", "Descrizione": "VOSTRI EMOLUMENTI", "Importo": 2450.0},
    ]
    quote = [("2022-01-12", 30.0)]
    resto = drop_shared_halves(storico, quote)
    assert len(resto) == 2, f"attese 2 righe superstiti, trovate {len(resto)}"
    assert any(r["Descrizione"] == "VOSTRI EMOLUMENTI" for r in resto), \
        "lo stipendio non deve essere scambiato per meta' Splitwise"
    assert sum(1 for r in resto if r["Descrizione"] == "Eurospin") == 1, \
        "vanno tolte al massimo due meta' per riga condivisa, non tutte"

    # Il parametro "days" deve davvero allargare la finestra di ricerca:
    # una riga a due giorni di distanza resta con days=1, sparisce con days=2.
    lontano = [
        {"Data": "2022-02-14", "Descrizione": "Farmacia", "Importo": -20.0},
    ]
    quota_lontana = [("2022-02-12", 20.0)]
    assert len(drop_shared_halves(lontano, quota_lontana, days=1)) == 1, \
        "days=1 non deve raggiungere una riga a due giorni di distanza"
    assert len(drop_shared_halves(lontano, quota_lontana, days=2)) == 0, \
        "days=2 deve raggiungere una riga a due giorni di distanza"

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

# Dove si depositano gli estratti conto. Sta tutto fuori da git: dentro ci
# sono IBAN e movimenti, e una volta committati restano per sempre.
EXPORT_DIR = "export"
ARCHIVE_DIR = "elaborati"       # dentro export/, dopo un caricamento riuscito
ANON_DIR = "anonimi"            # copie senza IBAN, accanto agli originali

# File che vivono nella cartella ma non sono estratti conto. Senza questa lista
# la pipeline proverebbe a leggere come export i propri stessi output.
GENERATED = {
    "consolidato.csv", "da_rivedere.csv", "dashboard.xlsx", "dashboard.html",
    "categorie_merge.csv", "regole.csv", "merchant.csv", "override.csv",
    "natura.csv", "correzioni.csv", "categorie_cache.json",
    "dashboard_layout.json", "conti.csv",
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
        "accounts": load_accounts(folder / "conti.csv"),
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


def write_anonymous_copy(folder, source, accounts=()):
    """Scrive in export/anonimi/ lo stesso export senza dati sensibili.

    Rilegge il file invece di ritagliare il consolidato: la copia deve
    rispecchiare QUELL'export, non la vista d'insieme.

    Attenzione al nome: "anonimo" vuol dire senza IBAN, numeri di carta e
    codici tecnici. Importi, date, negozi e saldi restano tutti, quindi non e'
    un file da mandare in giro alla leggera.
    """
    source = Path(source)
    rows = load_transactions(source, account_of(source.name, accounts))
    if not rows:
        return None
    target = Path(folder) / EXPORT_DIR / ANON_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / (source.stem + ".csv")
    pd.DataFrame(rows)[["Data", "Descrizione", "Importo", "Conto"]].to_csv(
        path, index=False, sep=";", encoding="utf-8-sig")
    return path


def pending_exports(folder):
    """Gli export appena depositati, non ancora archiviati."""
    root = Path(folder) / EXPORT_DIR
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*")
                  if p.is_file() and p.suffix.lower() in (".csv", ".xlsx", ".xls"))


def archived_exports(folder):
    """Gli export gia' caricati, cioe' quelli sotto export/elaborati/.

    Archiviare e' organizzare, non escludere: se gli archiviati non si
    leggessero, il consolidato perderebbe tutta la storia a ogni caricamento.
    """
    root = Path(folder) / EXPORT_DIR / ARCHIVE_DIR
    if not root.is_dir():
        return []
    found = [p for p in root.rglob("*")
             if p.is_file() and p.suffix.lower() in (".csv", ".xlsx", ".xls")]
    return sorted(found, key=lambda p: (p.name, str(p)))


def all_exports(folder, include_pending=False):
    """Gli export da leggere.

    Quelli in attesa restano fuori finche' non si preme "Carica dati": se
    entrassero da soli, il pulsante non caricherebbe niente, si limiterebbe a
    spostare file gia' dentro ai conti.
    """
    found = archived_exports(folder)
    if include_pending:
        found = found + pending_exports(folder)
    return found


def read_sources(folder, config, output, include_pending=False):
    """Le transazioni grezze, dagli export in export/.

    Lo storico MoneyWiz non e' piu' una sorgente: le sue transazioni sono
    state migrate una volta in export/elaborati/storico-moneywiz.csv, e
    quello che resta (i 1635 esempi etichettati) alimenta solo il motore di
    categorizzazione.
    """
    print("EXPORT")
    # Un estratto conto lasciato nella radice e' un file dimenticato, non un
    # export: dirlo evita di chiedersi perche' quei movimenti non compaiono.
    skip = GENERATED | {output.lower()}
    strays = [p.name for p in Path(folder).glob("*")
              if p.is_file() and p.suffix.lower() in (".csv", ".xlsx", ".xls")
              and p.name.lower() not in skip and "report" not in p.name.lower()]
    if strays:
        print(f"  attenzione: {len(strays)} file nella radice non vengono letti "
              f"({', '.join(strays[:4])}). Gli export vanno in {EXPORT_DIR}/")

    accounts = config.get("accounts", [])
    rows = []
    for path in all_exports(folder, include_pending):
        rows.extend(load_transactions(path, account_of(path.name, accounts)))
    return rows


def run(folder, use_llm=True, output="consolidato.csv",
        make_dashboard=True, config=None, include_pending=False):
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

    rows = read_sources(folder, config, output, include_pending)
    if not rows:
        raise RuntimeError(
            f"nessuna transazione: metti gli export in {EXPORT_DIR}/ e premi "
            "Carica dati. Se e' la prima volta, esegui prima "
            "python bilancio.py --migra-storico")

    # L'ordine conta: prima gli ID sui valori originali, poi le correzioni.
    # Al contrario, correggere un importo cambierebbe l'ID della sua riga e
    # la correzione si staccherebbe dalla transazione al giro successivo.
    assign_ids(rows)
    apply_corrections(rows, config["corrections"])
    rows = drop_cross_file_duplicates(rows)
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
    parser.add_argument("--migra-storico", action="store_true",
                        help="estrae lo storico MoneyWiz in export/elaborati/ "
                             "e smette di usarlo come sorgente")
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

    if args.migra_storico:
        print("migrazione dello storico MoneyWiz")
        config = load_config(folder)
        migrate_history(folder, config["db_path"])
        return

    try:
        run(folder, use_llm=not args.no_llm,
            output=args.output, make_dashboard=not args.no_dashboard)
    except RuntimeError as exc:
        sys.exit(str(exc))



if __name__ == "__main__":
    main()
