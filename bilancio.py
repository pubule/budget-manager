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
import tempfile
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

# Dove finisce un giroconto in uscita che nessuno riceve. drop_internal_
# transfers() ha gia' tolto le coppie vere: quel che resta marcato Giroconto e'
# denaro che esce e non torna, perche' il conto che riceve non e' caricato o
# perche' non e' un conto ma una persona. Lasciarlo "non spesa" vuol dire non
# contarlo da nessuna parte, e sul bilancio di casa erano 3.634 euro spariti
# dal quadro. Le gambe IN ENTRATA restano giroconti: sono l'altra meta' di
# bonifici partiti da conti caricati piu' tardi, e contarle come reddito
# gonfierebbe le entrate senza che nessuno abbia guadagnato niente.
TRANSFER_OUT = "Da identificare"


def transfer_out(category, amount, source):
    """La categoria di un giroconto in uscita rimasto senza chi lo riceve."""
    if category == TRANSFER and amount < 0 and source != "override":
        return TRANSFER_OUT
    return category


def categoria_finale(category, row, source):
    """La categoria dopo l'unica eccezione che vale sulla singola riga.

    Un rimborso in uscita ha la stessa forma di un giroconto spaiato, e senza
    questa guardia finirebbe in "Da identificare": la quota "saldo" dice che
    quella riga NON e' una spesa, e' il conto che si pareggia fra le due
    persone, quindi la categoria del categorizzatore resta quella che e'.
    Fuori da questo caso decide transfer_out(): un giroconto in uscita
    sopravvissuto all'appaiamento e' una spesa, non uno spostamento. Un
    override non ci finisce mai dentro: quello l'ha deciso una persona
    guardando la riga, e transfer_out() lo sa gia' dalla source.
    """
    if row.get("Quota") == "saldo":
        return category
    return transfer_out(category, row["Importo"], source)


# Le categorie hanno due livelli: "Casa > Casalinghi" e' l'area Casa e la
# sottocategoria Casalinghi. Dentro al motore viaggiano unite in una stringa
# sola - lo storico, il voto per token e la cache sono tutti indicizzati cosi',
# e separarle li' dentro vorrebbe dire riscriverli senza guadagnarci niente.
# Si dividono in due colonne quando escono: nei file di configurazione, nel
# consolidato e nell'interfaccia.
#
# Il livello che conta per la natura e' il SECONDO: "Casa" da sola contiene
# quattro nature diverse, perche' le bollette sono ricorrenti e una
# ristrutturazione e' straordinaria.
# Scritto cosi' perche' \n dentro a una heredoc di shell
# diventa un a capo vero prima ancora che Python lo veda.
NEWLINE = chr(10)

LEVEL_SEP = " > "


def _text(value):
    """Stringa ripulita, con il NaN di pandas trattato come vuoto.

    Serve perche' queste funzioni girano anche sulle colonne del frame, dove
    una sottocategoria assente e' NaN: senza questo controllo str() ne farebbe
    la stringa "nan" e nascerebbe una sottocategoria che non esiste.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def split_category(name):
    """"Casa > Casalinghi" -> ("Casa", "Casalinghi"). Senza separatore la
    sottocategoria resta vuota."""
    area, _, leaf = _text(name).partition(LEVEL_SEP)
    return area.strip(), leaf.strip()


def join_category(area, leaf=""):
    """L'inverso. Una sottocategoria vuota lascia solo l'area."""
    area, leaf = _text(area), _text(leaf)
    return f"{area}{LEVEL_SEP}{leaf}" if area and leaf else area

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
    """categorie_merge.csv: la coppia attuale -> la coppia finale.

    Nella colonna finale IGNORA non e' una categoria ma un marcatore, quindi
    resta intero: dice di non imparare quella voce dallo storico.
    """
    if not path.exists():
        return {}
    mapping = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            current = join_category(row.get("categoria_attuale"),
                                    row.get("sottocategoria_attuale"))
            final = (row.get("categoria_finale") or "").strip()
            if final != IGNORE:
                final = join_category(final, row.get("sottocategoria_finale"))
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
    """Assegna l'ID a ogni riga. Va fatto PRIMA di applicare le correzioni.

    Un ID gia' presente non si tocca: le righe scritte a mano se lo portano dal
    file, e ricalcolarlo dal contenuto le staccherebbe dalla loro categoria e
    dalla loro quota appena si corregge un importo.
    """
    seen = Counter()
    for row in rows:
        if not str(row.get("ID") or "").strip():
            row["ID"] = transaction_id(row, seen)
    return rows


# I campi che si possono correggere a mano su una singola transazione.
# Natura non c'e' di proposito: discende dalla coppia categoria +
# sottocategoria attraverso natura.csv, e deciderla per riga vorrebbe dire
# avere due transazioni della stessa voce con nature diverse.
CORREGGIBILI = ("Importo", "Data", "Descrizione", "Conto", "Merchant")


def load_corrections(path):
    """correzioni.csv: id -> {campo: valore}, per i fatti sbagliati.

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
            if key and field in CORREGGIBILI and value:
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


def load_exclusions(path):
    """escluse.csv: id -> motivo, per le righe che non devono comparire.

    Cancellare davvero una riga di banca sarebbe una bugia: il caricamento
    successivo la riporterebbe. Qui si registra la decisione, e la riga resta
    ripescabile togliendo la sua riga dal file.
    """
    if not path.exists():
        return {}
    fuori = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            key = (row.get("id") or "").strip()
            if key:
                fuori[key] = (row.get("motivo") or "").strip()
    if fuori:
        print(f"  {len(fuori)} righe escluse a mano")
    return fuori


def apply_exclusions(rows, exclusions, discarded=None):
    """Toglie le righe elencate in escluse.csv, annotando il motivo."""
    if not exclusions:
        return rows
    fuori = [r for r in rows if r.get("ID") in exclusions]
    for row in fuori:
        note_discarded(discarded, f"esclusa a mano: {exclusions[row['ID']]}",
                       [row])
    return [r for r in rows if r.get("ID") not in exclusions]


# I valori ammessi per la quota. "saldo" non e' una divisione: dice che QUELLA
# riga e' il rimborso, e muove il conto per intero. "niente" e' una lapide:
# dice che questa riga NON ha una quota, una scelta esplicita -- diversa dal
# non avere affatto una riga in quote.csv, che vuol dire "nessuno l'ha mai
# guardata". Serve perche' apply_shares() usa row.setdefault() apposta (una
# quota dedotta dalle colonne di Splitwise resta scritta nel derivato anche
# quando l'export sparisce, e setdefault() non deve mai cancellarla in
# silenzio): l'unico modo di togliere una quota e' scriverlo a voce alta.
QUOTE = ("meta", "tutto", "saldo", "niente")


def load_shares(path):
    """quote.csv: id -> {"Pagato da": ..., "Quota": ...}.

    Una riga "niente" torna stringhe vuote (non "niente": quella parola non
    deve mai finire in "Quota", o il filtro del registro la scambierebbe per
    una quota vera). apply_shares() poi la applica con row.update(), che a
    differenza di setdefault() sovrascrive anche una quota gia' dedotta.
    """
    if not path.exists():
        return {}
    quote = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            key = (row.get("id") or "").strip()
            quota = (row.get("quota") or "").strip().lower()
            if not key or quota not in QUOTE:
                continue
            if quota == "niente":
                quote[key] = {"Pagato da": "", "Quota": ""}
                continue
            pagante = (row.get("pagato_da") or "").strip().lower()
            if pagante not in ("io", "lei"):
                continue
            quote[key] = {"Pagato da": pagante, "Quota": quota}
    if quote:
        print(f"  {len(quote)} quote dichiarate a mano")
    return quote


def load_partita(path):
    """partita.csv: da quando conta il conto fra due persone, e da quanto.

    Ricostruirlo da tutto lo storico sarebbe piu' completo ma non verificabile:
    dove un estratto conto ha coperto una riga condivisa, la quota si e' persa
    prima che il travaso esistesse. Un punto di partenza scelto e' un numero di
    cui si risponde.
    """
    vuota = {"dal": "", "saldo": 0.0, "controparte": "la controparte"}
    if not path.exists():
        return vuota
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            return {"dal": (row.get("dal") or "").strip(),
                    "saldo": parse_amount(row.get("saldo") or ""),
                    "controparte": (row.get("controparte")
                                    or "la controparte").strip()}
    return vuota


def apply_shares(rows, quote):
    """Mette "Pagato da" e "Quota" su ogni riga, con le colonne sempre presenti.

    quote.csv vince sulla deduzione dalle colonne di Splitwise: la prima e' una
    decisione presa da una persona guardando la riga, la seconda un'inferenza
    su un file. Le righe senza niente prendono due stringhe vuote, cosi' il
    consolidato ha sempre le stesse colonne.
    """
    for row in rows:
        dichiarata = quote.get(row.get("ID"))
        if dichiarata:
            row.update(dichiarata)
        row.setdefault("Pagato da", "")
        row.setdefault("Quota", "")
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
            category = join_category(row.get("categoria"),
                                     row.get("sottocategoria"))
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
    #
    # Le date vanno scartate insieme ai numeri: "29/09/25" non e' isdigit()
    # per via delle barre, quindi passava il filtro e diventava il nome del
    # negozio. Ne sono nati merchant come "29/09/25 Unicredit".
    significant = [
        word for word in normalize(description).split()
        if len(word) > 2 and word not in STOPWORDS
        and not re.fullmatch(r"[\d/.\-]+", word)
    ]
    return " ".join(significant[:2]).title() if significant else ""


def merchant_words(description):
    """Parole utili a riconoscere un negozio, per il confronto fra sorgenti.

    Diverso da tokens(): spezza anche su / e . perche' le banche incollano il
    circuito al nome ("YYW1047657451655/PAYPAL", "WWW.AMAZON.IT"), e butta via
    i token che sono solo cifre o date, che nelle causali abbondano e non
    dicono nulla su chi ha incassato.
    """
    pieces = re.split(r"[/.]", str(description or ""))
    words = set()
    for piece in pieces:
        for word in normalize(piece).split():
            if len(word) <= 2 or word in STOPWORDS:
                continue
            if re.fullmatch(r"[\d\-]+", word):
                continue
            words.add(word)
    return words


def same_expense(first, second, merchants=()):
    """Le due descrizioni parlano dello stesso acquisto?

    Serve a non fondere righe che combaciano per importo e data ma sono spese
    diverse. Al primo estratto conto vero, su 8 accoppiamenti 2 erano sbagliati
    ("Pannello per tettoia" fuso con "VINSANTO CAFE'"): con importi tondi in una
    citta' piccola le collisioni casuali arrivano subito.

    Tre condizioni, basta che ne valga una.
    """
    # 1. Stesso negozio secondo merchant.csv. E' qui che si insegnano le
    #    equivalenze non ovvie: "Rata condominio" e "BONIFICO A BORGO MANGANO"
    #    finiscono entrambe su "Borgo Mangano" perche' il file lo dice.
    one = canonical_merchant(str(first or ""), merchants)
    two = canonical_merchant(str(second or ""), merchants)
    if one and two and one == two:
        return True

    words_one, words_two = merchant_words(first), merchant_words(second)

    # 3. Se una delle due non ha nessuna parola utile e' gergo bancario puro
    #    ("ADDEBITO SEPA DD PER FATTURA A VOSTRO CARICO"): non c'e' niente su
    #    cui decidere, quindi ci si fida di importo e data come prima.
    if not words_one or not words_two:
        return True

    # 2. Una parola in comune. Il prefisso conta perche' le banche troncano:
    #    "PARROCCHIA S.ANNA DI LUGA" e' "Lugagnano" tagliato.
    if words_one & words_two:
        return True
    for short, long in ((words_one, words_two), (words_two, words_one)):
        for word in short:
            if len(word) >= 4 and any(other.startswith(word) for other in long):
                return True
    return False


def load_natura(path):
    """natura.csv: categoria + sottocategoria -> natura.

    La natura sta sulla COPPIA, non sull'area: dentro "Casa" le bollette sono
    ricorrenti e una ristrutturazione e' straordinaria.
    """
    if not path.exists():
        return {}
    natura = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            category = join_category(row.get("categoria"),
                                     row.get("sottocategoria"))
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
            category = join_category(row.get("categoria"),
                                     row.get("sottocategoria"))
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

        # Le regole PRIMA dello storico. Sono l'unica cosa scritta apposta:
        # lo storico e' quello che MoneyWiz aveva etichettato negli anni, a
        # volte male. Con l'ordine inverso una regola nuova non spostava
        # niente - lo storico decide l'82% delle righe - e cambiare la
        # tassonomia diventava impossibile se non a colpi di override.
        for pattern, category in self.rules:
            if pattern.search(description):
                self.stats["regola"] += 1
                return category, "regola", 1.0

        if key in self.history:
            self.stats["storico-esatto"] += 1
            return self.history[key], "storico-esatto", 1.0

        match, score = self._best_history_match(key)
        if match and score >= JACCARD_MIN:
            self.stats["storico-simile"] += 1
            return self.history[match], "storico-simile", score

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
        quotas = quota_columns(frame)
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

def usable(frame):
    """Le COLONNE bastano a tirare fuori data, descrizione e importo?

    Guarda solo le intestazioni, non le righe: serve anche per fiutare una
    riga candidata a fare da intestazione, che di righe sotto non ne ha.
    """
    if frame is None:
        return False
    date_col, desc_col, amount_col, debit, credit = find_columns(frame)
    return bool(date_col and desc_col and (amount_col or (debit and credit)))


def header_row(raw, limit=30):
    """In quale riga sta l'intestazione vera.

    Diverse banche premettono un preambolo con numero di conto, periodo e
    filtri, e solo dopo la tabella. Leggendo con l'intestazione alla riga zero
    escono colonne senza nome e il file sembra vuoto: succedeva con un export
    dove la tabella cominciava alla riga 18.
    """
    for i in range(min(limit, len(raw))):
        nomi = [str(x) for x in raw.iloc[i].tolist()]
        try:
            if usable(pd.DataFrame(columns=nomi)):
                return i
        except ValueError:          # nomi ripetuti nella riga: non e' quella
            continue
    return None


def read_table(path):
    """Legge un export bancario provando le combinazioni piu' comuni."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        frame = pd.read_excel(path)
        if usable(frame):
            return frame
        raw = pd.read_excel(path, header=None)
        riga = header_row(raw)
        if riga is not None:
            print(f"  {path.name}: intestazione alla riga {riga + 1}, "
                  f"sopra c'e' un preambolo")
            return pd.read_excel(path, skiprows=riga)
        return frame
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
                if usable(frame) or skiprows:
                    return frame
                # Colonne trovate ma inservibili: puo' esserci un preambolo
                # piu' lungo di una riga.
                try:
                    raw = pd.read_csv(path, header=None, dtype=str, **options)
                except Exception:
                    return frame
                riga = header_row(raw)
                if riga is None:
                    return frame
                print(f"  {path.name}: intestazione alla riga {riga + 1}, "
                      f"sopra c'e' un preambolo")
                return pd.read_csv(path, skiprows=riga, dtype=str, **options)
    try:
        return pd.read_csv(path, sep=None, engine="python",
                           encoding="utf-8-sig", dtype=str)
    except Exception:
        return None


# Cosa cercare in ogni colonna, IN ORDINE DI PREFERENZA. La prima riga che
# trova una colonna vince, e solo a parita' conta l'ordine dentro al file.
#
# Serve perche' un estratto conto puo' avere piu' colonne plausibili per lo
# stesso ruolo. UniCredit ne ha due per la descrizione, "Causale" e
# "Descrizione": la causale e' l'etichetta generica del tipo di movimento
# ("PAGAMENTO POS"), la descrizione e' quella che dice davvero cosa hai
# comprato. Scegliendo la prima che capitava nel file vinceva la causale, e il
# consolidato si riempiva di righe tutte uguali.
CANDIDATE = {
    "data": [r"data (contabile|registrazione|operazione)", r"\bdata\b", r"\bdate\b"],
    "descrizione": [r"descrizione", r"\boperazione\b", r"beneficiario|payee",
                    r"\bmemo\b", r"causale"],
    "importo": [r"\bimporto\b", r"\bamount\b", r"\bcosto\b"],
    "dare": [r"dare|uscite|addebiti"],
    "avere": [r"avere|entrate|accrediti"],
}


def find_columns(frame):
    """Individua le colonne data, descrizione e importo."""
    nomi = [(column, normalize(column)) for column in frame.columns]
    presi, scelte = set(), {}
    for ruolo, preferenze in CANDIDATE.items():
        for regola in preferenze:
            trovata = next((c for c, n in nomi
                            if c not in presi and re.search(regola, n)), None)
            if trovata is not None:
                # Una colonna sola per un ruolo solo: senza questo, in un file
                # con "Data operazione" e nessuna descrizione, la stessa
                # colonna finirebbe a fare sia da data sia da descrizione.
                presi.add(trovata)
                scelte[ruolo] = trovata
                break
    return tuple(scelte.get(r) for r in
                 ("data", "descrizione", "importo", "dare", "avere"))


def quota_columns(frame):
    """Le colonne numeriche in piu' rispetto a quelle standard, quelle vive.

    Vive vuol dire: non tutte NaN e non tutte zero. Commissioni, spese e
    competenze valgono 0,00 su ogni riga in molti tracciati bancari:
    dropna() non le toglie (zero non e' NaN), sommano zero su tutte le righe
    e farebbero scattare la firma condivisa su un estratto conto qualunque,
    con la conseguenza peggiore possibile - ogni entrata diventa un'uscita.
    """
    known = {c for c in find_columns(frame) if c}
    extra = [c for c in frame.columns if c not in known]
    if not extra:
        return pd.DataFrame()
    quotas = pd.DataFrame({c: pd.to_numeric(frame[c], errors="coerce")
                           for c in extra}).dropna(axis=1, how="all")
    if quotas.empty or not len(quotas.columns):
        return pd.DataFrame()
    return quotas.loc[:, (quotas.fillna(0) != 0).any()]


# Come si chiama Fabio nelle colonne persona di Splitwise. Serve a sapere
# quale dei due saldi e' il tuo: gli altri campi sono simmetrici e senza
# questo non si distingue "ho pagato io" da "ha pagato lei".
IO = re.compile(r"(?i)fabio")


def shares_from_quotas(costo, saldi):
    """Chi ha pagato e quanto deve l'altro, dai saldi di una riga condivisa.

    Il valore nella colonna di una persona e' il suo saldo su quella riga,
    cioe' PAGATO MENO DOVUTO, e i due saldi sommano zero. Con due persone e un
    pagatore solo:

        chi ha pagato              = quello col saldo positivo
        quota di chi non ha pagato = -saldo
        quota del pagatore         = costo - quota dell'altro

    Torna None quando la riga non rientra nel modello: tre persone, nessuno in
    credito, un costo a zero. Inventare una quota li' sarebbe peggio che non
    averla, perche' finirebbe in un registro di debiti senza dirlo.
    """
    vivi = {nome: valore for nome, valore in saldi.items()
            if valore is not None and not math.isnan(valore)}
    if len(vivi) != 2 or not costo:
        return None
    mio = next((n for n in vivi if IO.search(n)), None)
    if mio is None:
        return None
    altro = next(n for n in vivi if n != mio)
    if abs(vivi[mio] + vivi[altro]) > 0.01:
        return None
    if abs(vivi[mio]) < 0.01:
        return None
    pagante = "io" if vivi[mio] > 0 else "lei"
    dovuta = abs(vivi[mio] if pagante == "lei" else vivi[altro])
    if abs(dovuta - abs(costo)) < 0.01:
        return {"Pagato da": pagante, "Quota": "tutto"}
    # Splitwise divide al centesimo, non all'euro: l'unico scarto legittimo
    # da meta' esatta e' il mezzo centesimo di un importo dispari (60,01 fa
    # 30,01 / 30,00, scarto 0,005). 0,02 e' largo per quello e stretto per
    # tutto il resto: una tolleranza piu' larga (es. 0,51) etichetterebbe
    # come "meta'" anche uno split 90/10 su un 1,00 euro.
    if abs(dovuta - abs(costo) / 2) < 0.02:
        return {"Pagato da": pagante, "Quota": "meta"}
    return None


def is_shared_export(frame):
    """Vero se il file divide ogni spesa fra piu' persone.

    La firma e' numerica, non nominale: oltre alle colonne che find_columns()
    riconosce, ce ne sono altre numeriche la cui somma per riga fa zero. Chi
    anticipa ha un credito, gli altri un debito pari e contrario.

    Riconoscerlo dai nomi delle persone si romperebbe al primo cambio di nome
    o all'ingresso di un terzo. Dal nome del file non funziona affatto:
    Splitwise lo chiama "koala_<data>_export.csv".
    """
    quotas = quota_columns(frame)
    # Serve piu' di una colonna viva: una sola (un saldo progressivo) non e'
    # una divisione fra persone, e nessuna nemmeno.
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


def load_transactions(path, account=None, discarded=None):
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
    # Le colonne persona: fino a oggi si buttavano. Contengono chi ha pagato e
    # quanto deve l'altro, per ogni riga condivisa.
    quote = quota_columns(frame) if shared else pd.DataFrame()
    category_col = next((c for c in frame.columns
                         if normalize(c) == "categorie"), None)

    rows, settled = [], []
    for _, row in frame.iterrows():
        description = anonymize(row.get(desc_col))
        if not description or normalize(description) in ("nan", "bilancio totale"):
            continue
        if shared and is_settlement(description,
                                    row.get(category_col) if category_col else ""):
            settled.append({
                "Data": parse_date(row.get(date_col)),
                "Descrizione": description,
                "Importo": round(parse_amount(row.get(amount_col))
                                 if amount_col else 0.0, 2),
                "Conto": account or path.stem,
                "Origine file": path.name,
                "Rango": source_rank(path, shared),
            })
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
        riga = {
            "Data": parse_date(row.get(date_col)),
            "Descrizione": description,
            "Importo": round(amount, 2),
            "Conto": account or path.stem,
            # Serve a distinguere i doppioni fra export diversi da due spese
            # identiche dentro lo stesso file.
            "Origine file": path.name,
            "Rango": source_rank(path, shared),
        }
        if not quote.empty and row.name in quote.index:
            dedotta = shares_from_quotas(
                abs(amount), quote.loc[row.name].to_dict())
            if dedotta:
                riga.update(dedotta)
        rows.append(riga)

    print(f"  {path.name}: {len(rows)} transazioni")
    if settled:
        totale = sum(r["Importo"] for r in settled)
        print(f"    {len(settled)} saldi scartati per {totale:,.2f}: "
              f"{', '.join(r['Descrizione'][:34] for r in settled[:3])}")
        note_discarded(discarded, "saldo", settled)
    # Un estratto conto ha entrambi i segni. Se non li ha, o e' una lista di
    # spese o il parser ha sbagliato colonna: dirlo evita di scoprirlo dai
    # totali.
    if rows and not shared and all(r["Importo"] > 0 for r in rows):
        print(f"    attenzione: {path.name} ha solo importi positivi. "
              "Controlla che la colonna dell'importo sia quella giusta")
    if shared:
        dedotte = sum(1 for r in rows if r.get("Quota"))
        print(f"    {dedotte} righe su {len(rows)} con pagatore e quota "
              f"dedotti dalle colonne persona")
    return rows


def note_discarded(discarded, step, rows):
    """Annota le righe tolte da un passo di scarto, per scartate.csv.

    Sapere QUANTE righe sono sparite non basta a controllarle: con estratti
    conto veri i numeri crescono e nessuno rifa' la verifica a mano.
    """
    if discarded is None:
        return
    discarded.extend(dict(row, **{"Scartata da": step}) for row in rows)


def drop_cross_file_duplicates(rows, discarded=None):
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
            note_discarded(discarded, "doppione fra file", [row])
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


# Chi vince quando due sorgenti descrivono lo stesso acquisto. Numero basso
# significa priorita' alta.
RANK_BANK, RANK_SHARED, RANK_HISTORY = 0, 1, 2


def source_rank(path, shared):
    """Il rango di una sorgente, dedotto dal file."""
    if Path(path).name == "storico-moneywiz.csv":
        return RANK_HISTORY
    return RANK_SHARED if shared else RANK_BANK


def drop_covered_by(rows, days=3, discarded=None, merchants=()):
    """Scarta le righe gia' coperte da una sorgente di rango superiore.

    Due casi, un meccanismo solo:
    - Splitwise contro banca: se Fabio paga con la carta l'uscita e' gia'
      nell'estratto conto; se paga Michela non compare mai sul suo conto e la
      riga condivisa va tenuta.
    - Storico migrato contro banca: lo storico copre 2022-2026 e si
      sovrappone a qualunque estratto conto scaricato per quel periodo.

    Accoppiamento uno-a-uno, preferendo la data piu' vicina, come in
    drop_internal_transfers().

    Le due righe devono avere lo STESSO SEGNO. Sul valore assoluto un
    accredito bancario di +60,00 cancellava una spesa condivisa di -60,00,
    che non c'entra niente con lui.
    """
    def as_date(value):
        try:
            return datetime.strptime(str(value), "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    # Chiave con segno: una copertura vale solo per righe dello stesso verso.
    higher = defaultdict(list)
    for position, row in enumerate(rows):
        higher[round(row.get("Importo", 0), 2)].append(position)

    dropped, used, ambiguous, contese = set(), set(), 0, 0
    diverse = []
    counts = Counter()
    # Si scorre partendo dal numero di rango piu' alto, cioe' dalla sorgente
    # meno affidabile: RANK_HISTORY (2), poi RANK_SHARED (1). Le righe
    # RANK_BANK (0) non vengono mai scartate.
    #
    # Quando la stessa spesa esiste in tutte e tre le sorgenti, l'accoppiamento
    # uno-a-uno ne lascia comunque una di troppo: la copertura bancaria viene
    # consumata da una sola. Con quest'ordine a sopravvivere e' la riga
    # condivisa, che porta la descrizione migliore ("Camminamento Borgo
    # Mangano" invece di "DISPOSIZIONE DI BONIFICO SEPA A:").
    #
    # Oggi il caso e' inerte: senza estratti conto in export/ il rango 0 non
    # esiste proprio.
    order = sorted(range(len(rows)),
                   key=lambda i: -rows[i].get("Rango", RANK_BANK))
    for position in order:
        row = rows[position]
        rank = row.get("Rango", RANK_BANK)
        if rank == RANK_BANK:
            continue
        day = as_date(row.get("Data"))
        candidates = []
        consumed = False
        for other in higher.get(round(row.get("Importo", 0), 2), ()):
            if other == position:
                continue
            if rows[other].get("Rango", RANK_BANK) >= rank:
                continue
            when = as_date(rows[other].get("Data"))
            distance = abs((day - when).days) if day and when else 99
            if distance > days:
                continue
            if not same_expense(row.get("Descrizione"),
                                rows[other].get("Descrizione"), merchants):
                # Stesso importo, stessa data, ma non e' lo stesso acquisto.
                diverse.append((row, rows[other]))
                continue
            if other in used or other in dropped:
                # C'era una copertura valida, ma un'altra sorgente l'ha gia'
                # presa: e' la contesa che "ambiguous" non vede, perche' qui
                # candidates puo' restare vuoto.
                consumed = True
                continue
            candidates.append((distance, other))
        if not candidates:
            if consumed:
                contese += 1
            continue
        if len(candidates) > 1:
            ambiguous += 1
        candidates.sort()
        # Uno-a-uno: la riga che copre non puo' coprirne una seconda.
        superstite = rows[candidates[0][1]]
        # La riga condivisa se ne va, ma e' l'unica a sapere com'era divisa la
        # spesa: senza il travaso la quota si perderebbe proprio sulle spese
        # fatte con la carta, che sono la maggioranza.
        if row.get("Quota") and not superstite.get("Quota"):
            superstite["Quota"] = row["Quota"]
            superstite["Pagato da"] = row.get("Pagato da", "")
        used.add(candidates[0][1])
        dropped.add(position)
        counts[row.get("Conto", "?")] += 1
        note_discarded(discarded, "coperta da sorgente superiore", [row])

    if dropped:
        print(f"  {len(dropped)} righe scartate: gia' coperte da una sorgente "
              "di rango superiore")
        for conto, count in counts.most_common():
            print(f"    {count:>5} da {conto}")
        if ambiguous:
            print(f"    {ambiguous} avevano piu' di un candidato: se questo "
                  "numero cresce, serve una coda di revisione")
    if contese:
        print(f"    {contese} righe avevano una copertura gia' consumata da "
              "un'altra sorgente: restano nel consolidato come possibile doppione")
    if diverse:
        # Non e' un errore: e' il motivo per cui due righe simili restano due.
        # Se una coppia qui sotto e' davvero lo stesso posto, la si insegna
        # aggiungendo una riga a merchant.csv.
        print(f"  {len(diverse)} coppie non fuse: stesso importo e data ma "
              "descrizioni diverse")
        for mine, other in diverse[:8]:
            print(f"    {mine.get('Importo', 0):>9,.2f}  "
                  f"{str(mine.get('Descrizione'))[:30]:<32}<-> "
                  f"{str(other.get('Descrizione'))[:34]}")
    return [row for position, row in enumerate(rows) if position not in dropped]


def drop_internal_transfers(rows, days=3, tolerance=0.01, discarded=None,
                            transfers=()):
    """Rimuove le coppie +X / -X fra conti propri, che raddoppiano i totali.

    Due righe si annullano se hanno importo opposto, conti diversi, date entro
    pochi giorni E ALMENO UNA DELLE DUE DICE di essere un giroconto.

    L'ultima condizione e' la piu' importante. Con il solo importo e la sola
    data le collisioni casuali arrivano subito: quattro pagamenti F24 dello
    stesso giorno (-12, -114, -116, -452) sparivano annullati da entrate
    qualsiasi di pari importo su un altro conto. E qui non si scarta una riga
    sola: se ne perdono DUE, una per parte, e i soldi svaniscono da entrambi i
    conti senza che niente lo segnali.

    Cosa "dice di essere un giroconto" lo decidono le regole con categoria
    Giroconto in regole.csv, che sono gia' il posto dove questa conoscenza
    vive: nominano i conti propri e i familiari.

    Un giroconto e' un movimento fra conti bancari: si appaiano solo righe di
    Rango RANK_BANK. Ne' Splitwise ne' lo storico migrato sono conti bancari
    (una spesa Splitwise e un residuo dello storico con importo opposto non
    sono un giroconto, sono coperti da drop_covered_by).
    """
    def dichiarato(row):
        descrizione = str(row.get("Descrizione", ""))
        return any(p.search(descrizione) for p in transfers)
    def as_date(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    negatives = defaultdict(list)
    for index, row in enumerate(rows):
        if row["Importo"] < 0 and row.get("Rango", RANK_BANK) == RANK_BANK:
            negatives[round(abs(row["Importo"]), 2)].append(index)

    matched, mute = set(), []
    for index, row in enumerate(rows):
        if row["Importo"] <= 0 or index in matched:
            continue
        if row.get("Rango", RANK_BANK) != RANK_BANK:
            continue
        date = as_date(row["Data"])
        for candidate in negatives.get(round(row["Importo"], 2), ()):
            if candidate in matched or rows[candidate]["Conto"] == row["Conto"]:
                continue
            other = as_date(rows[candidate]["Data"])
            if date and other and abs((date - other).days) > days:
                continue
            if not (dichiarato(row) or dichiarato(rows[candidate])):
                mute.append((row, rows[candidate]))
                continue
            matched.update({index, candidate})
            break

    if mute:
        # Vederle serve: se qui finisce un giroconto vero, gli manca una regola
        # in regole.csv. Se ci finisce altro, il filtro sta lavorando.
        print(f"  {len(mute)} coppie NON annullate: importo e data combaciano "
              f"ma nessuna delle due dice di essere un giroconto")
        for entrata, uscita in mute[:6]:
            print(f"    {entrata['Importo']:>9.2f}  "
                  f"{str(entrata.get('Descrizione'))[:30]:<30} <-> "
                  f"{str(uscita.get('Descrizione'))[:30]}")

    if matched:
        print(f"  {len(matched)} righe rimosse: giroconti fra conti propri "
              f"({len(matched) // 2} coppie)")
        note_discarded(discarded, "giroconto",
                       [rows[i] for i in sorted(matched)])
    return [row for i, row in enumerate(rows) if i not in matched]


# Un bonifico che la banca annulla e riaccredita. La descrizione lo dichiara:
# "STORNO DI OPERAZIONE BONIFICO ISTANTANEO NON ESEGUITO".
REVERSAL = re.compile(r"(?i)\bstorno\b|non eseguit|\bannullat")


def drop_reversals(rows, days=7, discarded=None):
    """Toglie uno storno e l'operazione che annulla.

    Un bonifico non eseguito torna indietro: sul conto restano due righe di
    pari importo e segno opposto, e nessuna delle due e' una transazione del
    bilancio. Lasciarle produce un'asimmetria brutta, perche' l'uscita
    finisce fra i giroconti ed e' esclusa dai totali mentre il riaccredito
    resta e viene contato come ENTRATA.

    Vincoli stretti: stesso conto, stesso importo al centesimo, entro pochi
    giorni, e uno-a-uno. Un rimborso vero non si chiama storno.
    """
    negative = defaultdict(list)
    for position, row in enumerate(rows):
        if row.get("Importo", 0) < 0:
            key = (row.get("Conto"), round(abs(row["Importo"]), 2))
            negative[key].append(position)

    dropped, used = set(), set()
    for position, row in enumerate(rows):
        amount = row.get("Importo", 0)
        if amount <= 0 or not REVERSAL.search(str(row.get("Descrizione", ""))):
            continue
        giorno = parse_date(row.get("Data"))
        for other in negative.get((row.get("Conto"), round(amount, 2)), ()):
            if other in used or other == position:
                continue
            altro = parse_date(rows[other].get("Data"))
            if giorno and altro and abs((
                    datetime.fromisoformat(giorno)
                    - datetime.fromisoformat(altro)).days) > days:
                continue
            used.add(other)
            dropped.update((position, other))
            break

    if dropped:
        totale = sum(abs(rows[i]["Importo"]) for i in dropped) / 2
        print(f"  {len(dropped)} righe rimosse: storni e operazioni annullate "
              f"({len(dropped) // 2} coppie) per {totale:,.2f}")
        note_discarded(discarded, "storno annullato",
                       [rows[i] for i in sorted(dropped)])
    return [row for i, row in enumerate(rows) if i not in dropped]


def drop_import_artifacts(rows, discarded=None):
    """Toglie i residui POSITIVI che l'importazione MoneyWiz si e' lasciata
    dietro a fronte di una spesa condivisa.

    MoneyWiz non importava sempre le due quote negative: per una parte delle
    spese Splitwise ha scritto anche una riga col costo PIENO e segno
    positivo ("Rossetto +203,03" contro "Rossetto -203,03" condivisa dello
    stesso giorno). drop_shared_halves() non le prende perche' accoppia sulla
    quota, non sul costo pieno.

    Non e' un'entrata: nessun soldo e' mai arrivato. Vincoli stretti - stessa
    data esatta, rango RANK_HISTORY, accoppiamento uno-a-uno con una riga
    RANK_SHARED - perche' allentandoli si mangiano entrate vere dello stesso
    importo.
    """
    shared = defaultdict(list)
    for position, row in enumerate(rows):
        if row.get("Rango", RANK_BANK) == RANK_SHARED:
            key = (row.get("Data"), round(abs(row.get("Importo", 0)), 2))
            shared[key].append(position)

    dropped, used = set(), set()
    for position, row in enumerate(rows):
        amount = row.get("Importo", 0)
        if amount <= 0 or row.get("Rango", RANK_BANK) != RANK_HISTORY:
            continue
        for other in shared.get((row.get("Data"), round(amount, 2)), ()):
            if other in used:
                continue
            used.add(other)
            dropped.add(position)
            break

    if dropped:
        totale = sum(rows[i]["Importo"] for i in dropped)
        print(f"  {len(dropped)} righe rimosse: artefatti di importazione "
              f"(entrate finte a fronte di una spesa condivisa) per {totale:,.2f}")
        note_discarded(discarded, "artefatto di importazione",
                       [rows[i] for i in sorted(dropped)])
    return [row for i, row in enumerate(rows) if i not in dropped]


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

    # Il ripiego su consolidato.csv deve tornare le righe con gli STESSI ID.
    # Ricalcolarli sui valori gia' corretti li staccherebbe dalle correzioni,
    # e al riavvio l'importo sistemato a mano tornerebbe quello sbagliato
    # senza che niente lo dica.
    with tempfile.TemporaryDirectory() as cartella:
        righe = [
            "ID;Data;Descrizione;Merchant;Importo;Conto;Natura;Categoria;"
            "Sottocategoria;Origine;Confidenza",
            "abc123#1;2026-01-15;spesa al mercato;Mercato;-12,50;Koala;"
            "Quotidiane;Cibo & Mangiare;Alimentari;regola;1.0",
            "def456#1;2026-01-16;;;-3,00;Koala;;;;;",
            ";;riga senza data;;-1,00;Koala;;;;;",
        ]
        (Path(cartella) / "consolidato.csv").write_text(
            NEWLINE.join(righe) + NEWLINE, encoding="utf-8-sig")
        ripescate = read_consolidato(cartella, "consolidato.csv")
        assert read_consolidato(cartella, "non-esiste.csv") == [], \
            "un file che non c'e' deve dare zero righe, non esplodere"
    assert len(ripescate) == 2, f"ripescate {len(ripescate)} righe invece di 2"
    assert ripescate[0]["ID"] == "abc123#1", "l'ID non e' quello del file"
    assert abs(ripescate[0]["Importo"] + 12.50) < 0.005, "importo non letto"
    assert ripescate[0]["Conto"] == "Koala", "conto perso"

    # Un giroconto in uscita che nessuno riceve e' una spesa, non uno
    # spostamento: la coppia vera l'ha gia' tolta drop_internal_transfers, e
    # quel che resta e' denaro uscito dal quadro. Le entrate no: sono l'altra
    # meta' di bonifici partiti da conti caricati piu' tardi.
    assert transfer_out(TRANSFER, -3829.0, "regola") == TRANSFER_OUT,         "l'uscita scoperta resta non spesa"
    assert transfer_out(TRANSFER, 200.0, "regola") == TRANSFER,         "l'entrata scoperta e' diventata spesa"
    assert transfer_out(TRANSFER, -3829.0, "override") == TRANSFER,         "l'override non ha retto"
    assert transfer_out("Casa > Luce e gas", -50.0, "regola") == "Casa > Luce e gas",         "una spesa qualsiasi e' stata scambiata per un giroconto"
    # categoria_finale() e' la sola porta per le due eccezioni di riga: un
    # test sul solo transfer_out() non vedrebbe mai la guardia sul rimborso,
    # perche' quella guardia vive fuori da transfer_out().
    saldo = {"Importo": -500.0, "Quota": "saldo"}
    assert categoria_finale(TRANSFER, saldo, "regola") == TRANSFER, \
        "un rimborso (Quota 'saldo') non deve diventare 'Da identificare'"
    #   La stessa riga, ma senza la quota: il comportamento di prima resta.
    senza_saldo = {"Importo": -500.0, "Quota": ""}
    assert categoria_finale(TRANSFER, senza_saldo, "regola") == TRANSFER_OUT, \
        "senza quota un giroconto in uscita spaiato resta una spesa"

    # Escludere e' l'unica forma di cancellazione: una riga di banca tornerebbe
    # comunque al caricamento dopo, quindi sparire davvero sarebbe una bugia.
    righe = [
        {"ID": "aaa#1", "Descrizione": "spesa buona", "Importo": -10.0},
        {"ID": "bbb#1", "Descrizione": "doppione", "Importo": -10.0},
    ]
    buttate = []
    resto = apply_exclusions(righe, {"bbb#1": "doppione di luglio"}, buttate)
    assert [r["ID"] for r in resto] == ["aaa#1"], "l'esclusione non ha tolto la riga"
    assert len(buttate) == 1, "la riga esclusa non e' finita fra le scartate"
    assert buttate[0]["Scartata da"] == "esclusa a mano: doppione di luglio",         f"motivo perso: {buttate[0]['Scartata da']!r}"
    assert apply_exclusions(righe, {}, []) == righe,         "senza esclusioni non deve cambiare niente"

    # Una riga scritta a mano porta il suo ID dal file: gli altri nascono dal
    # contenuto, e cambiando l'importo l'ID cambierebbe staccando la riga dalla
    # sua categoria e dalla sua quota.
    mano = [{"ID": "man-20260825-01", "Data": "2026-08-24",
             "Descrizione": "Cena", "Importo": -84.0, "Conto": "Contanti"}]
    assign_ids(mano)
    assert mano[0]["ID"] == "man-20260825-01", "l'ID scritto a mano e' stato sovrascritto"
    mano[0]["Importo"] = -90.0
    assign_ids(mano)
    assert mano[0]["ID"] == "man-20260825-01", "l'ID e' cambiato con l'importo"

    # consolidato.csv puo' gia' contenere la riga scritta a mano (il giro
    # precedente l'ha scritta lui): merge_manual() deve toglierla da "rows"
    # prima di riaggiungere le manuali, non sommarle.
    ferma = {"ID": "man-1", "Data": "2026-08-24", "Descrizione": "Cena",
             "Importo": -84.0, "Conto": "Contanti", "Rango": RANK_BANK}
    corretta = {"ID": "man-1", "Data": "2026-08-24", "Descrizione": "Cena",
                "Importo": -90.0, "Conto": "Contanti", "Rango": RANK_BANK}
    fuso = merge_manual([ferma], [corretta])
    assert len(fuso) == 1, "la riga scritta a mano si e' duplicata"
    assert fuso[0]["Importo"] == -90.0,         "transazioni.csv non ha vinto sulla copia ferma nel derivato"

    # prepara_righe() e' la sequenza VERA usata da run(): un test che chiama
    # merge_manual() e apply_corrections() a mano, nell'ordine che gli pare,
    # non si accorgerebbe se qualcuno li scambiasse DENTRO prepara_righe().
    # Tutti e quattro gli ingredienti insieme, quindi, attraverso la funzione
    # vera: una di banca senza ID, una a mano col suo ID, una correzione
    # sulla riga a mano, un'esclusione su un'altra riga.
    banca = [{"Data": "2026-01-15", "Descrizione": "spesa", "Importo": -12.0,
              "Conto": "Koala"}]
    da_escludere = {"Data": "2026-01-16", "Descrizione": "doppione",
                    "Importo": -5.0, "Conto": "Koala"}
    id_da_escludere = transaction_id(da_escludere, Counter())
    riga_mano = {"ID": "man-1", "Data": "2026-08-24", "Descrizione": "Cena",
                 "Importo": -84.0, "Conto": "Contanti", "Rango": RANK_BANK}
    config_prova = {"corrections": {"man-1": {"Importo": -90.0}},
                    "exclusions": {id_da_escludere: "doppione"}}
    rows = prepara_righe(banca + [da_escludere], [riga_mano], config_prova,
                         [], False)
    per_id = {r["ID"]: r for r in rows}
    assert id_da_escludere not in per_id, "la riga esclusa non e' sparita"
    assert per_id["man-1"]["Importo"] == -90.0,         "la correzione su una riga scritta a mano non si applica"
    assert "#" in banca[0]["ID"], "la riga di banca non ha preso un ID"

    # Il saldo di una persona su una riga condivisa e' "pagato meno dovuto", e
    # i due saldi sommano zero. Da li' si ricavano pagatore e quote senza che
    # nessuno marchi niente a mano.
    #   Costo 60, Michela +30, Fabio -30  ->  ha pagato lei, meta' ciascuno
    assert shares_from_quotas(60.0, {"Fabio Stocco": -30.0,
                                     "Mikela bogoni": 30.0}) == \
        {"Pagato da": "lei", "Quota": "meta"}, "meta' pagata da lei sbagliata"
    #   Costo 60, Fabio +30, Michela -30  ->  ha pagato lui, meta' ciascuno
    assert shares_from_quotas(60.0, {"Fabio Stocco": 30.0,
                                     "Mikela bogoni": -30.0}) == \
        {"Pagato da": "io", "Quota": "meta"}, "meta' pagata da me sbagliata"
    #   Costo 24, Fabio +24, Michela -24  ->  ho pagato io, tutto suo
    assert shares_from_quotas(24.0, {"Fabio Stocco": 24.0,
                                     "Mikela bogoni": -24.0}) == \
        {"Pagato da": "io", "Quota": "tutto"}, "quota intera sbagliata"
    #   Nessuno in credito: non e' derivabile, e inventarlo sarebbe peggio.
    assert shares_from_quotas(60.0, {"Fabio Stocco": 0.0,
                                     "Mikela bogoni": 0.0}) is None, \
        "una riga senza pagatore non deve produrre una quota"
    #   Tre persone: fuori dal modello, si lascia stare.
    assert shares_from_quotas(90.0, {"a": 60.0, "b": -30.0, "c": -30.0}) is None, \
        "con tre persone la derivazione deve tacere"
    #   1,00 euro diviso 90/10: e' uno split vero, non una meta'. Con la
    #   tolleranza stretta (0,02) non deve piu' passare per "meta'".
    assert shares_from_quotas(1.0, {"Fabio Stocco": 0.10,
                                     "Mikela bogoni": -0.10}) is None, \
        "uno split 90/10 su un euro e' stato letto come meta'"
    #   Nessuna delle due colonne e' Fabio: non si sa quale saldo e' il tuo,
    #   e indovinare significherebbe scambiare chi paga con chi riceve.
    assert shares_from_quotas(60.0, {"Michela": 30.0, "Anna": -30.0}) is None, \
        "senza una colonna riconoscibile come Fabio non deve inventare"

    # quote.csv vince sulla deduzione: e' una decisione presa da una persona
    # guardando la riga, la deduzione e' un'inferenza su un file.
    righe = [{"ID": "x#1", "Pagato da": "lei", "Quota": "meta"},
             {"ID": "y#1"}]
    apply_shares(righe, {"x#1": {"Pagato da": "io", "Quota": "tutto"}})
    assert righe[0]["Quota"] == "tutto", "quote.csv non ha vinto sulla deduzione"
    assert righe[0]["Pagato da"] == "io", "il pagatore di quote.csv non ha vinto"
    assert righe[1]["Quota"] == "", "una riga senza quota deve avere le colonne vuote"
    assert righe[1]["Pagato da"] == "", "il pagatore vuoto deve esserci comunque"

    # La terza gamba: una riga con una quota gia' DEDOTTA da Splitwise (Task 3)
    # e nessuna voce in quote.csv per quell'ID. setdefault() non deve toccarla:
    # e' il caso che un refuso su row.update()/setdefault() romperebbe per
    # primo, in silenzio, perche' non tocca ne' il ramo "vince quote.csv" ne'
    # il ramo "riga del tutto vuota".
    dedotta = [{"ID": "z#1", "Pagato da": "io", "Quota": "meta"}]
    apply_shares(dedotta, {})
    assert dedotta[0]["Quota"] == "meta", "la quota dedotta e' sparita senza quote.csv"
    assert dedotta[0]["Pagato da"] == "io", "il pagatore dedotto e' sparito senza quote.csv"

    # La lapide "niente": load_shares() non deve far trapelare la parola
    # "niente" stessa ne' un None, o finirebbe in "Quota" e il filtro del
    # registro la scambierebbe per una quota vera.
    with tempfile.TemporaryDirectory() as cartella:
        percorso = Path(cartella) / "quote.csv"
        percorso.write_text(
            "id;pagato_da;quota;nota" + NEWLINE +
            "w#1;;niente;" + NEWLINE, encoding="utf-8-sig")
        lapide = load_shares(percorso)
    assert lapide == {"w#1": {"Pagato da": "", "Quota": ""}}, \
        f"la lapide non e' tornata vuota: {lapide}"

    # Il caso che avrebbe dovuto fallire senza la lapide: una riga con una
    # quota gia' DEDOTTA (come "dedotta" sopra, stesso ID) ma con un "niente"
    # esplicito in quote.csv per quell'ID. Il dizionario delle quote viene da
    # load_shares() vero, non costruito a mano: senza il ramo "niente" li'
    # dentro, "z#1" non ci finirebbe proprio, e setdefault() in apply_shares()
    # lascerebbe risorgere "meta"/"io" -- il bug esatto segnalato.
    with tempfile.TemporaryDirectory() as cartella:
        percorso = Path(cartella) / "quote.csv"
        percorso.write_text(
            "id;pagato_da;quota;nota" + NEWLINE +
            "z#1;;niente;" + NEWLINE, encoding="utf-8-sig")
        quote_lapide = load_shares(percorso)
    lapidata = [{"ID": "z#1", "Pagato da": "io", "Quota": "meta"}]
    apply_shares(lapidata, quote_lapide)
    assert lapidata[0]["Quota"] == "", "la lapide non ha cancellato la quota dedotta"
    assert lapidata[0]["Pagato da"] == "", "la lapide non ha cancellato il pagatore dedotto"

    # Il punto di partenza del registro: senza il file non deve esplodere, e
    # deve dare un saldo zero, non un buco che rompe la somma piu' avanti.
    assert load_partita(Path("non-esiste-partita.csv")) == \
        {"dal": "", "saldo": 0.0, "controparte": "la controparte"}, \
        "senza partita.csv il punto di partenza deve essere neutro"
    with tempfile.TemporaryDirectory() as cartella:
        percorso = Path(cartella) / "partita.csv"
        percorso.write_text(
            "dal;saldo;controparte;nota" + NEWLINE +
            "2026-01-01;0,00;Michela;" + NEWLINE, encoding="utf-8-sig")
        letta = load_partita(percorso)
    assert letta == {"dal": "2026-01-01", "saldo": 0.0, "controparte": "Michela"}, \
        f"partita.csv letto male: {letta}"

    # Il travaso: drop_covered_by tiene la riga di banca e butta quella
    # condivisa, che pero' e' l'unica a sapere com'era divisa la spesa.
    coppia = [
        {"Data": "2026-02-10", "Descrizione": "Eurospin", "Importo": -60.0,
         "Conto": "UniCredit", "Rango": RANK_BANK},
        {"Data": "2026-02-10", "Descrizione": "Eurospin", "Importo": -60.0,
         "Conto": "Koala", "Rango": RANK_SHARED,
         "Pagato da": "io", "Quota": "meta"},
    ]
    resto = drop_covered_by(coppia)
    assert len(resto) == 1 and resto[0]["Conto"] == "UniCredit", \
        "la copertura non ha funzionato"
    assert resto[0].get("Quota") == "meta", "la quota si e' persa nel travaso"
    assert resto[0].get("Pagato da") == "io", "il pagatore si e' perso nel travaso"

    # Le altre righe continuano a prendere l'ID dal contenuto.
    banca = [{"Data": "2026-01-15", "Descrizione": "spesa", "Importo": -12.0,
              "Conto": "Koala"}]
    assign_ids(banca)
    assert banca[0]["ID"] and "#" in banca[0]["ID"], "l'ID automatico non c'e' piu'"

    # Un giroconto fra conti propri non deve contare due volte.
    pair = [
        {"Data": "2026-01-15", "Descrizione": "giroconto", "Importo": -200.0, "Conto": "a"},
        {"Data": "2026-01-16", "Descrizione": "ricarica", "Importo": 200.0, "Conto": "b"},
        {"Data": "2026-01-16", "Descrizione": "spesa", "Importo": -200.0, "Conto": "a"},
    ]
    # Le regole vanno passate: senza, nessuna riga "dice" di essere un
    # giroconto e non si annulla piu' niente. E' voluto.
    GIRO = [re.compile(r"(?i)giroconto|ricarica")]
    assert len(drop_internal_transfers(pair, transfers=GIRO)) == 1,         "giroconti non rimossi"
    assert len(drop_internal_transfers(pair)) == 3,         "senza regole non deve annullare niente"
    # Due movimenti opposti sullo STESSO conto non sono un giroconto.
    same = [
        {"Data": "2026-01-15", "Descrizione": "x", "Importo": -50.0, "Conto": "a"},
        {"Data": "2026-01-15", "Descrizione": "y", "Importo": 50.0, "Conto": "a"},
    ]
    assert len(drop_internal_transfers(same)) == 2, "rimossi movimenti dello stesso conto"
    # Un giroconto e' fra conti bancari: una spesa Splitwise e il residuo
    # dello storico con importo opposto non vanno appaiati qui, li tratta
    # drop_covered_by (rango superiore).
    banca_e_condiviso = [
        {"Data": "2024-09-15", "Descrizione": "Rossetto", "Importo": -203.03,
         "Conto": "Koala", "Rango": RANK_SHARED},
        {"Data": "2024-09-15", "Descrizione": "Rossetto", "Importo": 203.03,
         "Conto": "Storico", "Rango": RANK_BANK},
    ]
    assert len(drop_internal_transfers(banca_e_condiviso)) == 2, \
        "una riga condivisa non e' un giroconto, non va accoppiata alla banca"

    # Fra sorgenti che descrivono lo stesso acquisto vince l'estratto conto:
    # se Fabio paga Esselunga con la carta, l'uscita e' gia' li'. Le due righe
    # devono parlare dello STESSO negozio: la prima stesura di questo caso
    # usava "ESSELUNGA" contro "Eurospin", due supermercati diversi, e
    # pretendeva che venissero fusi.
    righe = [
        {"Data": "2026-01-12", "Descrizione": "PAGAMENTO POS ESSELUNGA",
         "Importo": -60.0, "Conto": "Intesa", "Rango": 0},
        {"Data": "2026-01-13", "Descrizione": "Esselunga",
         "Importo": -60.0, "Conto": "Splitwise", "Rango": 1},
        {"Data": "2026-01-12", "Descrizione": "Spesa di Michela",
         "Importo": -25.0, "Conto": "Splitwise", "Rango": 1},
    ]
    resto = drop_covered_by(righe)
    assert len(resto) == 2, f"attese 2 righe, trovate {len(resto)}"
    assert not any(r["Descrizione"] == "Esselunga" for r in resto), \
        "la riga condivisa coperta dalla banca doveva sparire"
    assert any(r["Descrizione"] == "Spesa di Michela" for r in resto), \
        "cio' che la banca non vede va tenuto: e' il motivo per cui Splitwise serve"

    # E due negozi diversi con lo stesso importo lo stesso giorno restano due
    # spese: e' il difetto trovato al primo estratto conto vero.
    diversi = [
        {"Data": "2026-06-25", "Descrizione": "VINSANTO CAFE VERONA VR",
         "Importo": -28.0, "Conto": "Hype", "Rango": 0},
        {"Data": "2026-06-25", "Descrizione": "Pannello per tettoia",
         "Importo": -28.0, "Conto": "Koala", "Rango": 1},
    ]
    assert len(drop_covered_by(diversi)) == 2, \
        "un pannello per la tettoia non e' un caffe: restano due spese"

    # La banca non viene mai scartata da una sorgente piu' bassa.
    solo_banca = [
        {"Data": "2026-01-12", "Descrizione": "A", "Importo": -60.0,
         "Conto": "Intesa", "Rango": 0},
        {"Data": "2026-01-12", "Descrizione": "B", "Importo": -60.0,
         "Conto": "Intesa", "Rango": 0},
    ]
    assert len(drop_covered_by(solo_banca)) == 2, \
        "due movimenti bancari uguali sono due spese, non un doppione"

    # Contesa: la stessa spesa in tutte e tre le sorgenti. L'uno-a-uno lascia
    # comunque un doppione (la banca non se ne libera mai), ma la banca puo'
    # coprire una sola fra Splitwise e storico, non entrambe.
    contesa = [
        {"Data": "2026-02-01", "Descrizione": "PAGAMENTO POS FARMACIA",
         "Importo": -40.0, "Conto": "Intesa", "Rango": RANK_BANK},
        {"Data": "2026-02-01", "Descrizione": "Farmacia",
         "Importo": -40.0, "Conto": "Splitwise", "Rango": RANK_SHARED},
        {"Data": "2026-02-01", "Descrizione": "DISPOSIZIONE BONIFICO",
         "Importo": -40.0, "Conto": "Storico", "Rango": RANK_HISTORY},
    ]
    resto = drop_covered_by(contesa)
    assert len(resto) == 2, f"attese 2 righe (contesa risolta una volta), trovate {len(resto)}"
    scartate = {"Splitwise", "Storico"} - {r["Conto"] for r in resto}
    assert len(scartate) == 1, \
        f"deve sparire una sola fra Splitwise e Storico, sparite: {scartate}"

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

    # Tutti i campi correggibili devono arrivare fino in fondo, e nessuno di
    # loro puo' spostare l'ID: si corregge una riga, non se ne crea un'altra.
    rows = assign_ids([dict(sample[0])])
    before = rows[0]["ID"]
    apply_corrections(rows, {before: {
        "Data": "15/01/2026", "Descrizione": "ALTRO NEGOZIO",
        "Conto": "Hype", "Merchant": "Altro Negozio", "Importo": "-1,50"}})
    assert rows[0]["ID"] == before, "l'ID si e' mosso correggendo i campi"
    assert rows[0]["Data"] == "2026-01-15", rows[0]["Data"]
    assert rows[0]["Descrizione"] == "ALTRO NEGOZIO"
    assert rows[0]["Conto"] == "Hype"
    assert rows[0]["Merchant"] == "Altro Negozio"
    assert set(CORREGGIBILI) == {"Importo", "Data", "Descrizione", "Conto",
                                 "Merchant"}, CORREGGIBILI
    assert "Natura" not in CORREGGIBILI,         "la natura discende dalla categoria, non si corregge per riga"
    assert "Categoria" not in CORREGGIBILI,         "la categoria e' un'interpretazione: va in override.csv"

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
        "Saldo": ["1250.00"],
    })
    assert not is_shared_export(con_saldo), "colonna saldo scambiata per quote"

    # Il caso che faceva davvero danno: due colonne numeriche in piu' che
    # valgono SEMPRE zero (commissioni, spese, competenze). Sommano zero su
    # tutte le righe, dropna() non le toglie, e la firma scattava: ogni
    # entrata dell'estratto conto diventava un'uscita.
    con_zeri = pd.DataFrame({
        "Data": ["13/01/2026", "14/01/2026"],
        "Descrizione": ["STIPENDIO", "PAGAMENTO POS ESSELUNGA"],
        "Importo": ["2450,00", "-84,30"],
        "Commissioni": ["0", "0"],
        "Spese": ["0.0", "0.0"],
    })
    assert not is_shared_export(con_zeri),         "colonne sempre a zero scambiate per quote: le entrate diventano uscite"

    # Una copertura vale solo a parita' di segno: un accredito di +60,00 non
    # e' la stessa spesa di un'uscita condivisa di -60,00.
    segni = [
        {"Data": "2026-03-02", "Descrizione": "RIMBORSO ASSICURAZIONE",
         "Importo": 60.0, "Conto": "Intesa", "Rango": RANK_BANK},
        {"Data": "2026-03-02", "Descrizione": "Cena fuori",
         "Importo": -60.0, "Conto": "Koala", "Rango": RANK_SHARED},
    ]
    assert len(drop_covered_by(segni)) == 2,         "un accredito bancario non copre una spesa condivisa di pari valore assoluto"

    # Una riga positiva dello storico che combacia, stesso giorno, con una
    # spesa condivisa e' un residuo dell'importazione, non un'entrata.
    artefatti = [
        {"Data": "2024-09-15", "Descrizione": "Rossetto", "Importo": -203.03,
         "Conto": "Koala", "Rango": RANK_SHARED},
        {"Data": "2024-09-15", "Descrizione": "Rossetto", "Importo": 203.03,
         "Conto": "Storico", "Rango": RANK_HISTORY},
        {"Data": "2024-09-15", "Descrizione": "VOSTRI EMOLUMENTI",
         "Importo": 2450.0, "Conto": "Storico", "Rango": RANK_HISTORY},
    ]
    resto = drop_import_artifacts(artefatti)
    assert len(resto) == 2, f"atteso un solo artefatto tolto, restano {len(resto)}"
    assert any(r["Descrizione"] == "VOSTRI EMOLUMENTI" for r in resto),         "lo stipendio non e' un artefatto di importazione"
    # Uno-a-uno: una sola riga condivisa non giustifica due entrate.
    doppio = artefatti[:2] + [dict(artefatti[1])]
    assert len(drop_import_artifacts(doppio)) == 2,         "accoppiamento non uno-a-uno: una riga condivisa ne copre solo una"

    # L'ordine dei passi: se le coperture si cercassero PRIMA dei giroconti,
    # la spesa condivisa verrebbe coperta da una gamba di giroconto che poi
    # sparisce, e tre righe darebbero zero superstiti.
    ordine = [
        {"Data": "2026-03-10", "Descrizione": "GIROCONTO A HYPE", "Importo": -300.0,
         "Conto": "Intesa", "Rango": RANK_BANK},
        {"Data": "2026-03-10", "Descrizione": "RICARICA DA INTESA", "Importo": 300.0,
         "Conto": "Hype", "Rango": RANK_BANK},
        {"Data": "2026-03-10", "Descrizione": "Affitto marzo", "Importo": -300.0,
         "Conto": "Koala", "Rango": RANK_SHARED},
    ]
    resto = drop_covered_by(drop_internal_transfers(ordine, transfers=GIRO))
    assert len(resto) == 1 and resto[0]["Descrizione"] == "Affitto marzo",         f"la spesa condivisa non deve sparire con i giroconti: {resto}"

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

    # Due righe che combaciano per importo e data non sono per forza la stessa
    # spesa. Casi presi dal primo estratto conto vero caricato.
    negozi = load_merchants(Path("merchant.csv"))

    # Stesso negozio secondo merchant.csv: e' li' che si insegnano le
    # equivalenze non ovvie.
    assert same_expense("DISPOSIZIONE DI BONIFICO SEPA A: BORGO MANGANO PER: Rata",
                        "Rata condominio", negozi), "merchant.csv non consultato"
    # Una parola in comune.
    assert same_expense("BISSOLO CASA S.R.L. GAMBELLARA VI", "Bissolo")
    assert same_expense("El Bagolo Ristorantino Sona", "Cena Bagolo")
    # Le banche troncano: "LUGA" e' "Lugagnano" tagliato.
    assert same_expense("PARROCCHIA S.ANNA DI LUGA SONA VR", "Sagra Lugagnano"),         "il troncamento della banca deve contare come parola in comune"
    # La barra incolla il circuito al nome e nasconde la parola utile.
    assert same_expense("YYW1047657451655/PAYPAL", "/PAYPAL")
    # Gergo bancario puro: nessuna parola su cui decidere, si torna a importo
    # e data come prima.
    assert same_expense("ADDEBITO SEPA DD PER FATTURA A VOSTRO CARICO",
                        "Bolletta luce"), "il gergo bancario va esentato"

    # E i due che al primo estratto conto vero erano stati fusi per sbaglio.
    assert not same_expense("5274 UCAGRIC BAR VERONA", "rossetto del 11/04"),         "un bar non e' il supermercato Rossetto"
    assert not same_expense("VINSANTO CAFE' VERONA VR", "Pannello per tettoia"),         "un pannello per la tettoia non e' un caffe'"

    # Il nome del negozio non puo' essere una data. "29/09/25" non e'
    # isdigit() per via delle barre, quindi passava il filtro del ripiego e
    # nascevano merchant come "29/09/25 Unicredit".
    assert canonical_merchant("Obi del 4/11/24", ()) == "Obi",         canonical_merchant("Obi del 4/11/24", ())
    # Un export con il preambolo davanti alla tabella. Diverse banche mettono
    # numero di conto, periodo e filtri prima dell'intestazione vera: leggendo
    # la riga zero escono colonne senza nome e il file sembra vuoto.
    with tempfile.TemporaryDirectory() as temporary:
        percorso = Path(temporary) / "conto.csv"
        percorso.write_text(
            "Lista movimenti;;;\n"
            "Conto corrente:;0064/00380700;;\n"
            "Periodo:;01/01/2026;25/08/2026;\n"
            ";;;\n"
            "Data;Operazione;Valuta;Importo\n"
            "04/08/2026;BONIFICO DA INQUILINO;EUR;750,00\n"
            "03/08/2026;ADDEBITO DIRETTO;EUR;-357,24\n", encoding="utf-8")
        tabella = read_table(percorso)
        assert usable(tabella), list(tabella.columns)
        assert len(tabella) == 2, len(tabella)
        righe = load_transactions(percorso, "Prova")
        assert len(righe) == 2, righe
        assert righe[0]["Importo"] == 750.0, righe[0]
        # E un file normale, senza preambolo, non deve peggiorare.
        dritto = Path(temporary) / "dritto.csv"
        dritto.write_text("Data;Descrizione;Importo\n"
                          "01/02/2026;ESSELUNGA;-12,50\n", encoding="utf-8")
        assert len(load_transactions(dritto, "Prova")) == 1

    # Un giroconto si annulla solo se ALMENO UNA delle due righe dice di
    # esserlo. Con il solo importo e la sola data quattro pagamenti F24 dello
    # stesso giorno sparivano annullati da entrate qualsiasi di pari importo su
    # un altro conto - e qui non si perde una riga, se ne perdono due.
    regole_giro = [re.compile(r"(?i)\ba:? *(hype|fideuram)\b"),
                   re.compile(r"(?i)giroconto|ricarica")]
    f24 = [
        {"Data": "2026-06-16", "Importo": -452.0, "Conto": "UniCredit",
         "Descrizione": "PAGAMENTO DELEGHE F23/F24 PRENOTATE FISCO/INPS"},
        {"Data": "2026-06-15", "Importo": 452.0, "Conto": "Hype",
         "Descrizione": "BONIFICO DA CLIENTE PER FATTURA"},
    ]
    resto = drop_internal_transfers([dict(r) for r in f24], transfers=regole_giro)
    assert len(resto) == 2,         "un F24 e un incasso di pari importo sono stati presi per un giroconto"
    # Ma un giroconto vero, che lo dichiara, si annulla ancora.
    vero = [
        {"Data": "2026-06-16", "Importo": -200.0, "Conto": "UniCredit",
         "Descrizione": "DISPOSIZIONE DI BONIFICO A HYPE"},
        {"Data": "2026-06-16", "Importo": 200.0, "Conto": "Hype",
         "Descrizione": "RICARICA CONTO"},
    ]
    assert drop_internal_transfers([dict(r) for r in vero],
                                   transfers=regole_giro) == [],         "un giroconto dichiarato non viene piu' annullato"
    # E basta che lo dica UNA delle due: l'altra gamba spesso non lo dice.
    meta = [dict(vero[0]), dict(vero[1])]
    meta[1]["Descrizione"] = "ACCREDITO"
    assert drop_internal_transfers(meta, transfers=regole_giro) == [],         "serve che lo dicano tutte e due, invece ne basta una"

    # Uno storno e l'operazione che annulla se ne vanno insieme. Se restasse
    # solo il riaccredito verrebbe contato come entrata, mentre l'uscita
    # finisce fra i giroconti ed e' esclusa dai totali: soldi comparsi dal
    # nulla nel KPI delle entrate.
    coppia = [
        {"Data": "2026-07-27", "Importo": -200.0, "Conto": "UniCredit",
         "Descrizione": "DISPOSIZIONE DI BONIFICO ISTANTANEO DEL 24.07.2026"},
        {"Data": "2026-07-27", "Importo": 200.0, "Conto": "UniCredit",
         "Descrizione": "STORNO DI OPERAZIONE BONIFICO ISTANTANEO NON ESEGUITO"},
        {"Data": "2026-07-28", "Importo": 200.0, "Conto": "UniCredit",
         "Descrizione": "BONIFICO A VOSTRO FAVORE DA: QUALCUNO"},
    ]
    resto = drop_reversals([dict(r) for r in coppia])
    assert len(resto) == 1, resto
    assert resto[0]["Descrizione"].startswith("BONIFICO A VOSTRO FAVORE"),         "e' stata tolta l'entrata vera invece dello storno"
    # Un conto diverso non e' la stessa operazione.
    altro = [dict(coppia[0]), dict(coppia[1])]
    altro[1]["Conto"] = "Hype"
    assert len(drop_reversals(altro)) == 2, "accoppiati due conti diversi"
    # E nemmeno un importo diverso.
    diverso = [dict(coppia[0]), dict(coppia[1])]
    diverso[1]["Importo"] = 199.0
    assert len(drop_reversals(diverso)) == 2, "accoppiati importi diversi"
    # Un rimborso vero non si chiama storno e deve restare.
    rimborso = [dict(coppia[0]),
                {"Data": "2026-07-27", "Importo": 200.0, "Conto": "UniCredit",
                 "Descrizione": "RIMBORSO ASSICURAZIONE"}]
    assert len(drop_reversals(rimborso)) == 2, "tolto un rimborso vero"

    # La scelta delle colonne va per PREFERENZA, non per ordine nel file.
    def scelte(*colonne):
        d, de, i, dare, avere = find_columns(pd.DataFrame(columns=list(colonne)))
        return d, de, i

    # UniCredit ha due colonne buone per la descrizione, e "Causale" viene
    # prima: e' l'etichetta generica del movimento, non dice cosa hai comprato.
    assert scelte("Data registrazione", "Data valuta", "Causale",
                  "Descrizione", "Importo") ==         ("Data registrazione", "Descrizione", "Importo")
    # Ma se la causale e' l'unica cosa che c'e', si usa quella.
    assert scelte("Data", "Causale", "Importo") == ("Data", "Causale", "Importo")
    # Due date: vince quella dell'operazione, non quella di valuta.
    assert scelte("Data valuta", "Data operazione", "Descrizione", "Importo")[0]         == "Data operazione"
    # Una colonna non puo' fare due mestieri: senza descrizione, "Data
    # operazione" non deve diventare la descrizione solo perche' contiene
    # la parola "operazione".
    assert scelte("Data operazione", "Importo")[1] is None
    # I formati gia' in uso non devono cambiare interpretazione.
    assert scelte("Data", "Descrizione", "Categorie", "Costo", "Valuta") ==         ("Data", "Descrizione", "Costo")
    assert scelte("Data", "Operazione", "Dettagli", "Valuta", "Importo") ==         ("Data", "Operazione", "Importo")

    # usable() guarda le colonne, non le righe: una tabella di sole
    # intestazioni deve poter essere giudicata, e serve a fiutare il preambolo.
    assert usable(pd.DataFrame(columns=["Data", "Operazione", "Importo"]))
    assert not usable(pd.DataFrame(columns=["Unnamed: 0", "Unnamed: 1"]))

    # Vuoto e' una risposta legittima: in "Spesa 4/11/24" un nome di negozio
    # non c'e'. Quello che non deve mai succedere e' che ci finiscano cifre.
    for grezza in ("PAGAMENTO del 01/05/2025 SUPERMERCATO",
                   "PRELIEVO MASTERCARD DEL 28/09/25 UNICREDIT ATM",
                   "Spesa 4/11/24", "12.03.2025 Farmacia Vitalba"):
        nome = canonical_merchant(grezza, ())
        assert not re.search(r"\d", nome), f"{grezza!r} -> {nome!r}"

    # I due livelli della categoria. Il giro completo deve tornare al punto di
    # partenza, altrimenti una categoria cambia nome passando dai file al
    # consolidato e i confronti smettono di combaciare.
    assert split_category("Casa > Casalinghi") == ("Casa", "Casalinghi")
    assert split_category("Stipendio") == ("Stipendio", "")
    assert split_category("") == ("", "")
    assert split_category(None) == ("", "")
    assert join_category("Casa", "Casalinghi") == "Casa > Casalinghi"
    assert join_category("Stipendio", "") == "Stipendio"
    assert join_category("Stipendio") == "Stipendio"
    assert join_category("", "Casalinghi") == "",         "senza area non si inventa una categoria"
    for nome in ("Casa > Casalinghi", "Stipendio", "Cibo & Mangiare > Alimentari"):
        assert join_category(*split_category(nome)) == nome, nome
    # Gli spazi intorno al separatore non devono creare una categoria diversa.
    assert split_category("Casa >  Casalinghi ") == ("Casa", "Casalinghi")
    # Il NaN di pandas arriva dalle colonne del frame e non deve diventare
    # la sottocategoria "nan".
    assert join_category("Stipendio", float("nan")) == "Stipendio"
    assert split_category(float("nan")) == ("", "")

    # Un export sostituito da uno scarico piu' recente non deve rientrare
    # dalla finestra. archived_exports() fa rglob() sugli elaborati, quindi
    # basta annidare la cartella dei sostituiti li' dentro perche' le righe
    # appena rimpiazzate tornino nel consolidato senza che nessuno se ne
    # accorga. Questa asserzione e' la guardia di quella scelta.
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / EXPORT_DIR
        (root / ARCHIVE_DIR / "2026-08").mkdir(parents=True)
        (root / ARCHIVE_DIR / "2026-08" / "conto.csv").write_text("", "utf-8")
        (root / SUPERSEDED_DIR).mkdir(parents=True)
        (root / SUPERSEDED_DIR / "conto.csv").write_text("", "utf-8")
        found = archived_exports(temporary)
        assert [p.name for p in found] == ["conto.csv"], found
        assert SUPERSEDED_DIR not in found[0].parts,             f"i sostituiti non devono stare sotto {ARCHIVE_DIR}/: {found[0]}"
        # E non devono nemmeno passare per export in attesa di caricamento.
        assert not pending_exports(temporary), pending_exports(temporary)

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
# Gli export rimpiazzati da uno scarico piu' recente. Deve stare dentro
# export/ ma FUORI da export/elaborati/: archived_exports() fa rglob() sugli
# elaborati, quindi una sottocartella li' dentro riporterebbe nel consolidato
# le righe appena sostituite, cioe' l'opposto di quello che serve.
SUPERSEDED_DIR = "sostituiti"

# File che vivono nella cartella ma non sono estratti conto. Senza questa lista
# la pipeline proverebbe a leggere come export i propri stessi output.
GENERATED = {
    "consolidato.csv", "da_rivedere.csv", "dashboard.xlsx", "dashboard.html",
    "categorie_merge.csv", "regole.csv", "merchant.csv", "override.csv",
    "natura.csv", "correzioni.csv", "categorie_cache.json",
    "dashboard_layout.json", "conti.csv", "scartate.csv",
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
        "exclusions": load_exclusions(folder / "escluse.csv"),
        "shares": load_shares(folder / "quote.csv"),
        "partita": load_partita(folder / "partita.csv"),
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


def read_sources(folder, config, output, include_pending=False, discarded=None):
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
        rows.extend(load_transactions(path, account_of(path.name, accounts),
                                      discarded))
    return rows


def read_manual(folder):
    """Le transazioni scritte a mano, da transazioni.csv.

    Sono di rango RANK_BANK come gli estratti conto: non vanno mai scartate da
    drop_covered_by, e possono coprire una riga condivisa (paghi in contanti,
    la registri qui e su Splitwise: la seconda e' un doppione della prima).
    """
    path = Path(folder) / "transazioni.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for riga in csv.DictReader(handle, delimiter=";"):
            data = (riga.get("data") or "").strip()
            key = (riga.get("id") or "").strip()
            if not data or not key:
                continue
            rows.append({
                "ID": key,
                "Data": data,
                "Descrizione": (riga.get("descrizione") or "").strip(),
                "Importo": round(parse_amount(riga.get("importo") or ""), 2),
                "Conto": (riga.get("conto") or "A mano").strip(),
                "Origine file": "transazioni.csv",
                "Rango": RANK_BANK,
            })
    if rows:
        print(f"  {len(rows)} transazioni scritte a mano")
    return rows


def merge_manual(rows, manuali):
    """Fonde le righe scritte a mano con le altre, senza doppioni.

    Il tranello: quando le manuali sono passate da "rows" in un giro con
    export, finiscono scritte anche loro dentro consolidato.csv. Al giro
    successivo senza export, read_consolidato() le rilegge da li' - e
    sommare "manuali" sopra le raddoppierebbe. transazioni.csv resta la
    fonte autorevole delle SUE righe: si toglie da rows ogni ID che compare
    fra le manuali, poi si aggiungono le manuali. Cosi' anche una riga
    corretta a mano nel file vince sulla copia ferma nel derivato.
    """
    manuali_ids = {r["ID"] for r in manuali}
    return [r for r in rows if r["ID"] not in manuali_ids] + manuali


def read_consolidato(folder, output):
    """Le transazioni gia' elaborate, quando la sorgente non c'e' piu'.

    Chi carica un estratto conto e poi lo cancella fa la cosa giusta: quel
    file contiene IBAN, numeri di carta e ogni movimento. Ma la pipeline
    ricostruisce sempre tutto dalla sorgente, e al riavvio successivo l'app
    apriva vuota con dentro cinquantasei mesi di lavoro.

    consolidato.csv resta un DERIVATO: nessuno lo modifica a mano, e ogni giro
    lo riscrive da capo. Solo che, quando la sorgente e' sparita, e' l'unica
    copia rimasta di cio' che la sorgente diceva.

    Le righe tornano gia' ripulite: doppioni, storni, giroconti e coperture li
    ha tolti il giro che le ha scritte. Rifare quei passi su di loro non
    toglierebbe niente di nuovo, ma potrebbe togliere di troppo: un giroconto
    rimasto spaiato si appaierebbe con una spesa qualsiasi di pari importo. La
    categorizzazione invece si rifa' sempre, cosi' una regola aggiunta oggi
    vale anche su questi dati.
    """
    path = Path(folder) / output
    if not path.exists():
        return []
    frame = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    if not usable(frame):
        return []
    rows = []
    for _, riga in frame.iterrows():
        if not _text(riga.get("Data")):
            continue
        rows.append({
            # L'ID viene dal file e non si ricalcola: sui valori gia' corretti
            # darebbe un id diverso, e ogni correzione si staccherebbe dalla
            # sua transazione al primo riavvio.
            "ID": _text(riga.get("ID")),
            "Data": _text(riga.get("Data")),
            "Descrizione": _text(riga.get("Descrizione")),
            "Importo": parse_amount(_text(riga.get("Importo"))),
            "Conto": _text(riga.get("Conto")),
            "Pagato da": _text(riga.get("Pagato da")),
            "Quota": _text(riga.get("Quota")),
            "Rango": RANK_BANK,
        })
    if rows:
        print(f"  export non trovati: riparto da {output}, "
              f"{len(rows)} transazioni gia' elaborate")
    return rows


def prepara_righe(rows, manuali, config, scartate, gia_pulite):
    """Mette insieme le righe prima di categorizzarle. L'ORDINE e' il punto:

    1. assign_ids(), ma solo se le righe non arrivano gia' pulite da
       consolidato.csv: al contrario correggere un importo cambierebbe l'ID
       della sua riga e la correzione si staccherebbe dalla transazione al
       giro successivo.
    2. merge_manual(), che porta dentro le righe scritte a mano (il loro ID
       viene dal file, non passano mai da assign_ids) e toglie prima
       l'eventuale copia gia' ferma in "rows" (consolidato.csv puo' averla
       scritta il giro precedente: vedi merge_manual() per il perche').
    3. apply_corrections(), DOPO le manuali: una correzione in
       correzioni.csv e' chiave sull'ID, e apply_corrections() scorre solo
       le righe che gli si passano. Prima delle manuali, una correzione
       scritta per una riga a mano non troverebbe mai la sua riga e
       resterebbe inerte per sempre.
    4. apply_exclusions(), per ultima: una riga esclusa non deve nemmeno
       partecipare agli appaiamenti dei passi successivi (giroconti,
       coperture), o consumerebbe la copertura di una riga buona.
    """
    if not gia_pulite:
        assign_ids(rows)
    rows = merge_manual(rows, manuali)
    apply_corrections(rows, config["corrections"])
    return apply_exclusions(rows, config["exclusions"], scartate)


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

    scartate = []
    rows = read_sources(folder, config, output, include_pending, scartate)
    # Il ripiego guarda SOLO gli export: con le righe scritte a mano fra le
    # sorgenti "rows" non sarebbe mai vuoto, e al riavvio senza export l'app
    # mostrerebbe tre righe manuali al posto di cinquantasei mesi di storia.
    gia_pulite = False
    if not rows:
        rows = read_consolidato(folder, output)
        gia_pulite = bool(rows)
    manuali = read_manual(folder)
    if not rows and not manuali:
        raise RuntimeError(
            f"nessuna transazione: metti gli export in {EXPORT_DIR}/ e premi "
            "Carica dati. Se e' la prima volta, esegui prima "
            "python bilancio.py --migra-storico")

    # Vedi prepara_righe() per il perche' di questo ordine esatto.
    rows = prepara_righe(rows, manuali, config, scartate, gia_pulite)
    if not gia_pulite:
        rows = drop_cross_file_duplicates(rows, scartate)
        # I giroconti PRIMA delle coperture: da quando drop_internal_transfers()
        # tocca solo il rango 0, anticiparlo non cambia niente per la banca e
        # toglie le gambe di giroconto dall'indice delle coperture. Al contrario,
        # una spesa condivisa "coperta" da una gamba poi annullata spariva del
        # tutto: tre righe dentro, zero fuori.
        # Prima dei giroconti: un bonifico annullato somiglia a uno spostamento
        # fra conti propri, e senza questo passo l'uscita sparirebbe fra i
        # giroconti lasciando il riaccredito a contare come entrata.
        rows = drop_reversals(rows, discarded=scartate)
        # Le regole con categoria Giroconto sono gia' il posto dove vive la
        # conoscenza di quali conti e quali persone sono "in famiglia".
        giroconti = [p for p, c in config["rules"] if c == TRANSFER]
        rows = drop_internal_transfers(rows, discarded=scartate,
                                       transfers=giroconti)
        rows = drop_import_artifacts(rows, scartate)
        rows = drop_covered_by(rows, discarded=scartate,
                               merchants=config["merchants"])

    # Prima della categorizzazione: il guardiano sui rimborsi qui sotto legge
    # row["Quota"], e deve trovarla gia' scritta su ogni riga.
    apply_shares(rows, config["shares"])

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
        # Le due eccezioni che riguardano solo questa riga vivono in
        # categoria_finale(): un rimborso ("saldo") o un giroconto senza
        # ritorno. Vedi la sua docstring per il perche' di entrambe.
        if categoria_finale(category, row, source) != category:
            category, source = TRANSFER_OUT, "giroconto senza ritorno"
            # Confidenza sotto 1: la riga va in da_rivedere. "Da identificare"
            # e' un parcheggio, non una risposta -- solo tu sai se quel
            # bonifico era l'affitto, un prestito o la ricarica di una carta.
            score = 0.5
            categorizer.stats["giroconto senza ritorno"] += 1
        # La natura si cerca sulla coppia intera, prima di dividerla: e' li'
        # che natura.csv la tiene.
        row["Natura"] = config["natura"].get(category, "Da classificare")
        row["Categoria"], row["Sottocategoria"] = split_category(category)
        # Un merchant corretto a mano deve sopravvivere: qui verrebbe
        # ricalcolato da merchant.csv e la correzione sparirebbe in silenzio.
        if "Merchant" not in config["corrections"].get(row["ID"], {}):
            row["Merchant"] = canonical_merchant(row["Descrizione"],
                                                 config["merchants"])
        row["Origine"] = source
        row["Confidenza"] = round(score, 2)
    categorizer.save_cache()

    for source, count in categorizer.stats.most_common():
        print(f"  {count:>6}  {source}")

    columns = ["ID", "Data", "Descrizione", "Merchant", "Importo", "Conto",
               "Natura", "Categoria", "Sottocategoria", "Pagato da", "Quota",
               "Origine", "Confidenza"]
    frame = pd.DataFrame(rows)[columns].sort_values(["Data", "Descrizione"])
    frame.to_csv(folder / output, index=False, sep=";", encoding="utf-8-sig")

    review = frame[frame["Confidenza"] < 1.0]
    review.to_csv(folder / "da_rivedere.csv", index=False, sep=";",
                  encoding="utf-8-sig")

    # Quali righe sono sparite, e per mano di quale passo. Il totale deve
    # tornare con la somma dei conteggi stampati qui sopra.
    pd.DataFrame(scartate, columns=[
        "Scartata da", "Data", "Descrizione", "Importo", "Conto",
        "Origine file", "Rango", "ID"]).to_csv(
        folder / "scartate.csv", index=False, sep=";", encoding="utf-8-sig")

    # I giroconti non sono ne' entrate ne' uscite: contarli falsa entrambi i
    # totali e fa sembrare che si spenda piu' di quanto si spende.
    moves = frame[frame["Natura"] == "Non spesa"]
    real = frame[frame["Natura"] != "Non spesa"]
    income = real[real.Importo > 0].Importo.sum()
    spent = real[real.Importo < 0].Importo.sum()

    print("")
    print(f"{len(frame)} transazioni -> {output}")
    # Quante righe ogni conto ha perso per strada, e in quale passo. Senza
    # questo prospetto un file che entra con 117 movimenti e ne lascia 80 nel
    # consolidato non dice DOVE sono finiti gli altri 37, e per scoprirlo
    # tocca rifare il giro a mano confrontando i conteggi.
    if scartate:
        persi = defaultdict(Counter)
        for riga in scartate:
            persi[riga.get("Conto") or "?"][riga.get("Scartata da", "?")] += 1
        print("  righe perse per strada, per conto:")
        for conto in sorted(persi):
            dettaglio = ", ".join(f"{n} {passo}"
                                  for passo, n in persi[conto].most_common())
            print(f"    {conto:<14} {sum(persi[conto].values()):>4}  ({dettaglio})")
    print(f"{len(review)} da controllare -> da_rivedere.csv")
    print(f"{len(scartate)} scartate -> scartate.csv")
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
