#!/usr/bin/env python3
"""
Server locale della dashboard.

    python server.py

Apre l'interfaccia nel browser. Da li' si correggono le categorie, si gestisce
la tassonomia, si scrivono le regole e si filtrano i grafici. Ogni modifica
finisce in uno dei CSV di configurazione e fa ripartire la pipeline.

Sorveglia anche la cartella: appena un export nuovo compare, rielabora da solo.

Ascolta SOLO su 127.0.0.1. Sono dati bancari e non devono essere raggiungibili
dalle altre macchine della rete.
"""

import csv
import json
import os
import re
import threading
import time
import urllib.parse
import traceback
import webbrowser
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

import bilancio
import dashboard

HOST = "127.0.0.1"
PORT = 8770

# Ogni quanto guardare se sono comparsi export nuovi.
WATCH_SECONDS = 3

FOLDER = Path(__file__).parent

# Intestazioni dei file di configurazione, per riscriverli senza perdere colonne.
SCHEMA = {
    "regole.csv": ["pattern", "categoria", "sottocategoria"],
    "merchant.csv": ["pattern", "merchant"],
    "natura.csv": ["categoria", "sottocategoria", "natura"],
    "override.csv": ["id", "data", "importo", "categoria", "sottocategoria",
                     "nota"],
    "correzioni.csv": ["id", "campo", "valore", "nota"],
    "categorie_merge.csv": ["categoria_attuale", "sottocategoria_attuale",
                            "transazioni", "categoria_finale",
                            "sottocategoria_finale"],
}

DEFAULT_LAYOUT = [
    {"id": "indicatori", "titolo": "Indicatori", "visibile": True},
    {"id": "natura", "titolo": "Dove puoi agire", "visibile": True},
    {"id": "andamento", "titolo": "Andamento", "visibile": True},
    {"id": "aree", "titolo": "Aree, anno su anno", "visibile": True},
    {"id": "voci", "titolo": "Voci per costo annuo", "visibile": True},
    {"id": "ricorrenti", "titolo": "Costi ricorrenti", "visibile": True},
    {"id": "merchant", "titolo": "Dove finiscono i soldi", "visibile": True},
    {"id": "revisione", "titolo": "Da rivedere", "visibile": True},
]


# --------------------------------------------------------------------------
# lettura e scrittura dei CSV di configurazione
# --------------------------------------------------------------------------

def read_rows(name):
    path = FOLDER / name
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle, delimiter=";")
                if any((v or "").strip() for v in row.values())]


def write_atomic(path, write):
    """Scrive tramite file temporaneo e rinomina.

    Aprire il file vero in scrittura lo tronca subito: se il processo muore a
    meta', quel file di configurazione resta mutilato e il lavoro fatto a mano
    e' perso. Con la rinomina o c'e' la versione vecchia o quella nuova, mai
    una via di mezzo.
    """
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8-sig", newline="") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)          # atomico anche su Windows
    finally:
        temp.unlink(missing_ok=True)


def write_rows(name, rows):
    """Riscrive un file di configurazione."""
    fields = SCHEMA[name]

    def write(handle):
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";",
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fields})

    write_atomic(FOLDER / name, write)


def editable(rows):
    """Scarta le righe di commento: iniziano con # e non hanno un valore."""
    return [r for r in rows
            if not (r.get("pattern") or "").lstrip().startswith("#")]


def read_layout():
    path = FOLDER / "dashboard_layout.json"
    if not path.exists():
        return DEFAULT_LAYOUT
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return DEFAULT_LAYOUT
    # I riquadri nuovi introdotti da una versione successiva devono comparire
    # comunque, altrimenti restano invisibili a chi ha gia' salvato un layout.
    known = {card["id"] for card in saved}
    return saved + [c for c in DEFAULT_LAYOUT if c["id"] not in known]


class LiveLog:
    """Raccoglie l'output della pipeline riga per riga, mentre gira.

    Prima il log si leggeva solo alla fine, da uno StringIO: durante i minuti
    del passo LLM l'interfaccia non aveva niente da mostrare. Qui ogni riga
    completa e' disponibile subito, e il browser se le porta via a pezzi.
    """

    def __init__(self, limit=4000):
        self.lock = threading.Lock()
        self.lines = []
        self.partial = ""
        self.limit = limit

    def write(self, text):
        with self.lock:
            self.partial += text
            while "\n" in self.partial:
                line, self.partial = self.partial.split("\n", 1)
                self.lines.append(line)
            # Tagliare sposterebbe gli indici sotto i piedi del browser, che
            # chiede "dammi dalla riga N": si tiene largo e si azzera solo fra
            # un giro e l'altro, in reset().
            if len(self.lines) > self.limit * 2:
                del self.lines[:len(self.lines) - self.limit]
        return len(text)

    def flush(self):
        pass

    def reset(self):
        with self.lock:
            self.lines, self.partial = [], ""

    def since(self, index):
        """Le righe dalla N in poi, piu' il nuovo segnalibro."""
        with self.lock:
            index = max(0, min(index, len(self.lines)))
            return self.lines[index:], len(self.lines)

    def tail(self, n=40):
        with self.lock:
            return self.lines[-n:]



# --------------------------------------------------------------------------
# stato dell'applicazione
# --------------------------------------------------------------------------

class State:
    """Il risultato dell'ultima elaborazione, piu' cio' che serve a rifarla."""

    def __init__(self, folder):
        self.folder = folder
        self.lock = threading.Lock()
        self.version = 0
        self.running = False
        self.error = None
        self.log = LiveLog()
        self.frame = pd.DataFrame()
        self.again = False        # una modifica e' arrivata durante un giro

    def has_exports(self):
        """C'e' almeno una sorgente gia' caricata sotto export/elaborati/?

        Dopo la migrazione dello storico e' di fatto sempre vera: quel file
        vive li' dentro. Non distingue piu' "ci sono estratti conto veri" da
        "c'e' solo lo storico" - resta solo per dire all'interfaccia che il
        consolidato ha una sorgente, e per distinguerla dal caso a cartella
        vuota (prima migrazione mai fatta).
        """
        return bool(bilancio.archived_exports(self.folder))

    def pending(self):
        """Gli export depositati e non ancora caricati."""
        return [p.name for p in bilancio.pending_exports(self.folder)]

    def signature(self):
        """Impronta degli export in attesa: cambia quando ne arriva uno.

        Serve solo ad AVVISARE. Prima faceva partire l'elaborazione da sola, e
        siccome guarda data e dimensione un file ancora in copia le cambia
        entrambe: la pipeline poteva leggere meta' di un .xlsx.
        """
        marks = {}
        for path in bilancio.pending_exports(self.folder):
            try:
                stat = path.stat()
            except OSError:
                continue
            marks[path.name] = (int(stat.st_mtime), stat.st_size)
        return marks

    def refresh(self, use_llm=True, include_pending=False):
        """Riesegue la pipeline. Un giro alla volta, gli altri si accodano."""
        if self.running:
            self.again = True
            return
        with self.lock:
            self.running, self.error = True, None
            self.log.reset()
            try:
                with redirect_stdout(self.log), redirect_stderr(self.log):
                    self.frame = bilancio.run(
                        self.folder, use_llm=use_llm,
                        make_dashboard=False,
                        include_pending=include_pending)
            except Exception as exc:                      # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                self.log.write(traceback.format_exc() + chr(10))
            finally:
                self.version += 1
                self.running = False
        if self.again:
            self.again = False
            self.refresh(use_llm=use_llm, include_pending=include_pending)

    def payload(self):
        """Tutto cio' che serve al browser, in un colpo solo.

        Le transazioni viaggiano intere (circa 300 KB): filtri, aggregazioni e
        grafici avvengono nel browser, cosi' ogni interazione e' istantanea e
        non serve una chiamata per ogni clic.
        """
        frame = self.frame
        if frame.empty:
            transactions, accounts = [], []
        else:
            clean = frame.where(pd.notna(frame), None)
            transactions = clean.to_dict("records")
            accounts = sorted(frame["Conto"].dropna().unique().tolist())

        # Le categorie viaggiano con i due livelli separati E col nome intero:
        # il nome intero e' la chiave con cui il browser le confronta e le
        # rimanda indietro, i due livelli servono a raggrupparle e a riempire
        # i menu a cascata.
        natura = {bilancio.join_category(r["categoria"], r["sottocategoria"]):
                  r["natura"] for r in read_rows("natura.csv")}
        totals = {}
        if not frame.empty:
            pairs = frame.assign(
                nome=[bilancio.join_category(a, s) for a, s in
                      zip(frame["Categoria"], frame["Sottocategoria"])])
            grouped = pairs.groupby("nome").Importo.agg(["sum", "count"])
            totals = {str(k): {"totale": round(r["sum"], 2),
                               "n": int(r["count"])}
                      for k, r in grouped.iterrows()}

        names = sorted(set(natura) | set(totals))
        categories = []
        for n in names:
            area, leaf = bilancio.split_category(n)
            categories.append({"nome": n, "categoria": area,
                               "sottocategoria": leaf,
                               "natura": natura.get(n, "Da classificare"),
                               "totale": totals.get(n, {}).get("totale", 0),
                               "n": totals.get(n, {}).get("n", 0)})

        return {
            "version": self.version,
            "running": self.running,
            "error": self.error,
            "log": self.log.tail(),
            "transactions": transactions,
            "accounts": accounts,
            "categories": categories,
            "nature": sorted({c["natura"] for c in categories}),
            # Le righe di commento restano nel file ma non sono regole:
            # mostrarle nell'elenco farebbe apparire una regola senza
            # categoria, e premere "salva" li' darebbe errore.
            "rules": editable(read_rows("regole.csv")),
            "merchants": editable(read_rows("merchant.csv")),
            "merge": read_rows("categorie_merge.csv"),
            "overrides": read_rows("override.csv"),
            "corrections": read_rows("correzioni.csv"),
            "layout": read_layout(),
            "has_exports": self.has_exports(),
            "pending": self.pending(),
        }


STATE = State(FOLDER)


# --------------------------------------------------------------------------
# operazioni richiamabili dall'interfaccia
# --------------------------------------------------------------------------

def upsert(name, rows, key_fields, payload, delete=False):
    """Inserisce, aggiorna o elimina una riga in un file di configurazione."""
    def same(row):
        return all((row.get(f) or "").strip() == (payload.get(f) or "").strip()
                   for f in key_fields)

    kept = [r for r in rows if not same(r)]
    if not delete:
        kept.append(payload)
    write_rows(name, kept)


def set_transaction(payload):
    """Categoria, importo, data o descrizione di una singola transazione.

    La categoria va in override.csv perche' e' un'interpretazione; importo e
    data vanno in correzioni.csv perche' cambiano i fatti. Restano separati:
    ricategorizzare non deve poter alterare un importo per sbaglio.
    """
    key = (payload.get("id") or "").strip()
    if not key:
        raise ValueError("id mancante")

    if payload.get("azzera"):
        # "annulla" deve togliere sia l'interpretazione sia la correzione dei
        # fatti, altrimenti la riga torna a una categoria automatica ma resta
        # con l'importo modificato a mano.
        write_rows("override.csv", [r for r in read_rows("override.csv")
                                    if (r.get("id") or "").strip() != key])
        write_rows("correzioni.csv", [r for r in read_rows("correzioni.csv")
                                      if (r.get("id") or "").strip() != key])
        return

    if "categoria" in payload:
        rows = [r for r in read_rows("override.csv")
                if (r.get("id") or "").strip() != key]
        category = (payload.get("categoria") or "").strip()
        if category:
            area, leaf = bilancio.split_category(category)
            rows.append({"id": key, "data": payload.get("data", ""),
                         "importo": payload.get("importo", ""),
                         "categoria": area, "sottocategoria": leaf,
                         "nota": payload.get("nota", "")})
        write_rows("override.csv", rows)

    fixes = [r for r in read_rows("correzioni.csv")
             if (r.get("id") or "").strip() != key
             or (r.get("campo") or "").strip().capitalize()
             not in {f.capitalize() for f in payload if f in
                     ("importo", "data", "descrizione")}]
    for field in ("importo", "data", "descrizione"):
        if field in payload and str(payload[field]).strip():
            fixes.append({"id": key, "campo": field.capitalize(),
                          "valore": str(payload[field]).strip(),
                          "nota": payload.get("nota", "")})
    write_rows("correzioni.csv", fixes)


def set_category(payload):
    """Rinomina, unisci, elimina una categoria oppure cambiane la natura."""
    action = payload.get("azione")
    name = (payload.get("nome") or "").strip()
    if not name:
        raise ValueError("nome categoria mancante")

    # Dal browser i nomi arrivano interi ("Casa > Casalinghi"); nei file
    # stanno su due colonne. Si confronta sul nome intero e si scrive diviso.
    area, leaf = bilancio.split_category(name)

    def whole(row, base, sub):
        return bilancio.join_category(row.get(base), row.get(sub))

    if action == "natura":
        rows = [r for r in read_rows("natura.csv")
                if whole(r, "categoria", "sottocategoria") != name]
        kind = (payload.get("natura") or "").strip()
        if kind:
            rows.append({"categoria": area, "sottocategoria": leaf,
                         "natura": kind})
        write_rows("natura.csv", rows)
        return

    if action in ("rinomina", "unisci", "elimina"):
        target = (payload.get("destinazione") or "").strip()
        if action == "elimina":
            target = bilancio.IGNORE
        if not target:
            raise ValueError("destinazione mancante")
        # IGNORA non e' una categoria ma un marcatore: non si divide.
        if target == bilancio.IGNORE:
            to_area, to_leaf = target, ""
        else:
            to_area, to_leaf = bilancio.split_category(target)

        rows = read_rows("categorie_merge.csv")
        found = False
        for row in rows:
            if whole(row, "categoria_attuale", "sottocategoria_attuale") == name:
                row["categoria_finale"], row["sottocategoria_finale"] = to_area, to_leaf
                found = True
            # Una categoria gia' rediretta su questa deve seguirla, altrimenti
            # dopo due rinomini si torna a puntare a un nome che non esiste.
            elif whole(row, "categoria_finale", "sottocategoria_finale") == name:
                row["categoria_finale"], row["sottocategoria_finale"] = to_area, to_leaf
        if not found:
            rows.append({"categoria_attuale": area,
                         "sottocategoria_attuale": leaf, "transazioni": "",
                         "categoria_finale": to_area,
                         "sottocategoria_finale": to_leaf})
        write_rows("categorie_merge.csv", rows)

        # Le regole che producevano il vecchio nome devono produrre il nuovo.
        rules = read_rows("regole.csv")
        for rule in rules:
            if whole(rule, "categoria", "sottocategoria") == name:
                rule["categoria"], rule["sottocategoria"] = to_area, to_leaf
        write_rows("regole.csv", rules)
        return

    raise ValueError(f"azione sconosciuta: {action}")


def set_rule(payload):
    """Crea, modifica o elimina una regola oppure un merchant canonico."""
    name = "merchant.csv" if payload.get("tipo") == "merchant" else "regole.csv"
    pattern = (payload.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("pattern mancante")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"espressione regolare non valida: {exc}") from exc

    rows = read_rows(name)
    old = (payload.get("pattern_precedente") or pattern).strip()
    rows = [r for r in rows if (r.get("pattern") or "").strip() != old]
    if not payload.get("elimina"):
        value = (payload.get("valore") or "").strip()
        if not value:
            raise ValueError("categoria o merchant mancante")
        if name == "merchant.csv":
            entry = {"pattern": pattern, "merchant": value}
        else:
            area, leaf = bilancio.split_category(value)
            entry = {"pattern": pattern, "categoria": area,
                     "sottocategoria": leaf}
        position = payload.get("posizione")
        # L'ordine conta: vince la prima regola che combacia, quindi le
        # specifiche devono poter stare sopra le generiche.
        if isinstance(position, int) and 0 <= position <= len(rows):
            rows.insert(position, entry)
        else:
            rows.append(entry)
    write_rows(name, rows)


def preview_rule(payload):
    """Quante e quali transazioni colpirebbe questa regola, senza salvare."""
    pattern = (payload.get("pattern") or "").strip()
    try:
        matcher = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return {"errore": str(exc), "n": 0, "esempi": []}

    frame = STATE.frame
    if frame.empty or not pattern:
        return {"n": 0, "esempi": []}

    hit = frame[frame["Descrizione"].fillna("").str.contains(matcher)]
    # Le regole scattano solo dopo lo storico: distinguere quante righe
    # cambierebbero davvero categoria da quante gia' ce l'hanno giusta.
    # Il confronto e' sul nome intero, perche' cambiare solo sottocategoria
    # dentro la stessa area e' comunque un cambio.
    target = (payload.get("valore") or "").strip()
    whole = [bilancio.join_category(a, s) for a, s in
             zip(hit["Categoria"], hit["Sottocategoria"])]
    changed = hit[[w != target for w in whole]] if target else hit
    esempi = hit.head(8)[["Data", "Descrizione", "Importo", "Categoria",
                          "Sottocategoria"]].to_dict("records")
    for riga, nome in zip(esempi, whole):
        riga["Categoria intera"] = nome
    return {
        "n": int(len(hit)),
        "cambierebbero": int(len(changed)),
        "totale": round(float(hit["Importo"].sum()), 2),
        "esempi": esempi,
    }


def archive_exports(names):
    """Sposta in export/elaborati/AAAA-MM/ e scrive le copie anonime.

    Solo dopo un giro riuscito: se il parsing fallisce il file deve restare
    dov'e', altrimenti sparisce dalla vista senza essere stato elaborato.
    """
    root = FOLDER / bilancio.EXPORT_DIR
    accounts = bilancio.load_accounts(FOLDER / "conti.csv")
    moved, skipped = [], []
    for name in names:
        source = root / name
        if not source.exists():
            continue
        # La copia si scrive PRIMA di spostare, rileggendo quel file: deve
        # rispecchiare quell'export, non il consolidato intero.
        #
        # Vale anche da controllo: load_transactions non solleva errori, su un
        # file illeggibile stampa "saltato" e torna vuoto. Senza questo, un
        # export che non e' stato letto verrebbe archiviato lo stesso e
        # sparirebbe dalla vista senza essere mai entrato nei conti.
        if bilancio.write_anonymous_copy(FOLDER, source, accounts) is None:
            skipped.append((name, "non ne e' stata letta nessuna transazione, "
                                  "controlla il formato del file"))
            continue
        # Stesso nome file vuol dire stesso conto e stesso periodo: e' un
        # riscarico, non un export nuovo. Vince il piu' recente, altrimenti
        # l'archivio accumula copie quasi identiche e una transazione che la
        # banca ha stornato resterebbe nel bilancio per sempre.
        #
        # Il gemello si cerca fra TUTTI gli archiviati, non nella cartella del
        # mese corrente: un export di agosto ricaricato a settembre finirebbe
        # in elaborati/2026-09/ e i due non si incontrerebbero mai.
        twin = next((p for p in bilancio.archived_exports(FOLDER)
                     if p.name == name), None)
        target = root / bilancio.ARCHIVE_DIR / time.strftime("%Y-%m")
        target.mkdir(parents=True, exist_ok=True)
        destination = target / name
        # Il gemello si tocca solo DOPO che il nuovo e' al sicuro. Se occupa
        # gia' la casella buona il nuovo si posa accanto con un nome
        # provvisorio e ci trasloca alla fine, cosi' un'archiviazione fallita
        # non lascia il bilancio senza nessuna delle due copie.
        staged = destination
        if destination.exists():
            staged = target / (f"{source.stem}-{time.strftime('%d%H%M%S')}"
                               f"{source.suffix}")
        source.replace(staged)
        # Dentro iCloud Drive su Windows replace() puo' tornare senza errore
        # e senza aver spostato niente. Dichiarare l'archiviazione riuscita
        # a quel punto e' il danno peggiore: il giro dopo gira con
        # include_pending=False e quelle transazioni spariscono dal
        # consolidato dopo essere state viste una volta.
        if not (staged.exists() and not source.exists()):
            skipped.append((name, "lo spostamento non e' andato a buon fine "
                                  "(iCloud Drive?)"))
            continue
        if twin:
            retired = retire(twin)
            print(f"{name} sostituisce la copia caricata prima: quella e' ora "
                  f"in {bilancio.EXPORT_DIR}/{bilancio.SUPERSEDED_DIR}/"
                  f"{retired.name}")
            if staged != destination:
                staged.replace(destination)
                staged = destination
        moved.append(staged.name)
    return moved, skipped


def retire(path):
    """Mette da parte un export sostituito da uno scarico piu' recente.

    Non si cancella: se il nuovo scarico fosse monco, e' l'unica copia da cui
    tornare indietro. Il nome si conserva, col timestamp solo se serve a non
    coprire un pensionato precedente.
    """
    target = FOLDER / bilancio.EXPORT_DIR / bilancio.SUPERSEDED_DIR
    target.mkdir(parents=True, exist_ok=True)
    destination = target / path.name
    if destination.exists():
        destination = target / (f"{path.stem}-{time.strftime('%d%H%M%S')}"
                                f"{path.suffix}")
    path.replace(destination)
    return destination


def load_and_archive(use_llm=True):
    """Elabora e, solo se e' andata bene, archivia gli originali."""
    names = STATE.pending()
    STATE.refresh(use_llm=use_llm, include_pending=True)

    # redirect_stdout vale solo dentro refresh(): scrivendo con print, questi
    # messaggi finirebbero nel terminale invece che nel pannello che l'utente
    # sta guardando proprio adesso.
    def say(text):
        STATE.log.write(text + chr(10))
        print(text)

    if STATE.error:
        say(f"elaborazione fallita: i {len(names)} file restano in "
            f"{bilancio.EXPORT_DIR}/, non sono stati archiviati")
        return
    try:
        moved, skipped = archive_exports(names)
    except OSError as exc:
        say(f"archiviazione fallita ({exc}). I dati sono stati elaborati, "
            f"i file restano in {bilancio.EXPORT_DIR}/")
        return
    for name, motivo in skipped:
        say(f"{name} NON archiviato: {motivo}. "
            f"Resta in {bilancio.EXPORT_DIR}/")
    if moved:
        say(f"archiviati {len(moved)} export in {bilancio.EXPORT_DIR}/"
            f"{bilancio.ARCHIVE_DIR}/: {', '.join(moved)}")
        say(f"copie senza IBAN in {bilancio.EXPORT_DIR}/{bilancio.ANON_DIR}/ "
            f"(attenzione: restano importi, date e negozi)")


ACTIONS = {
    "/api/transaction": set_transaction,
    "/api/categories": set_category,
    "/api/rules": set_rule,
}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "Bilancio"

    def log_message(self, *args):
        pass                        # niente rumore nel terminale

    def local_only(self):
        """Difesa contro il DNS rebinding: un sito web non deve poter parlare
        con questo server usando il browser dell'utente come ponte."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self.fail(403, "solo da locale")
            return False
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", ""):
            self.fail(403, f"Host non ammesso: {host}")
            return False
        return True

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.send_json({"errore": message}, status=status)

    def do_GET(self):
        if not self.local_only():
            return
        if self.path.split("?")[0] == "/":
            page = FOLDER / "app.html"
            if not page.exists():
                return self.fail(500, "app.html mancante")
            body = page.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/state"):
            if "light=1" in self.path:
                # Il browser controlla ogni pochi secondi se e' cambiato
                # qualcosa: mandargli 300 KB di transazioni per dirgli "no"
                # sarebbe uno spreco.
                # Il conteggio degli export in attesa viaggia anche qui:
                # senza elaborazione automatica la versione non cambia mai, e
                # un file appena depositato resterebbe invisibile finche' non
                # si ricarica la pagina a mano.
                return self.send_json({"version": STATE.version,
                                       "running": STATE.running,
                                       "pending": len(STATE.pending()),
                                       "error": STATE.error})
            return self.send_json(STATE.payload())
        if self.path.startswith("/api/log"):
            # "dammi dalla riga N in poi": il browser tiene il segno e non
            # riscarica quello che ha gia'. Deve restare leggibile mentre
            # la pipeline gira, altrimenti il log non serve a niente.
            query = urllib.parse.urlparse(self.path).query
            start = urllib.parse.parse_qs(query).get("from", ["0"])[0]
            try:
                start = int(start)
            except ValueError:
                start = 0
            entries, total = STATE.log.since(start)
            return self.send_json({"lines": entries, "next": total,
                                   "running": STATE.running,
                                   "version": STATE.version})
        self.fail(404, "non trovato")

    def do_POST(self):
        if not self.local_only():
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            return self.fail(400, f"JSON non valido: {exc}")

        route = self.path.split("?")[0]
        try:
            if route == "/api/rules/preview":
                return self.send_json(preview_rule(payload))
            if route == "/api/layout":
                write_atomic(FOLDER / "dashboard_layout.json",
                             lambda h: h.write(json.dumps(
                                 payload.get("layout", DEFAULT_LAYOUT),
                                 ensure_ascii=False, indent=1)))
                return self.send_json({"ok": True})
            if route == "/api/export":
                if STATE.frame.empty:
                    return self.fail(400, "nessun dato da esportare")
                written = dashboard.generate(STATE.frame, FOLDER)
                return self.send_json({"ok": True,
                                       "file": [p.name for p in written]})
            if route == "/api/carica":
                if STATE.running:
                    return self.send_json({"ok": True, "gia_in_corso": True})
                threading.Thread(
                    target=load_and_archive,
                    kwargs={"use_llm": payload.get("llm", True)},
                    daemon=True).start()
                return self.send_json({"ok": True, "avviato": True})
            if route == "/api/run":
                # Col modello un giro dura minuti: la richiesta non puo
                # restare appesa, altrimenti il browser molla e il log non
                # si vede proprio nel momento in cui serve.
                if STATE.running:
                    return self.send_json({"ok": True, "gia_in_corso": True})
                threading.Thread(
                    target=STATE.refresh,
                    kwargs={"use_llm": payload.get("llm", True)},
                    daemon=True).start()
                return self.send_json({"ok": True, "avviato": True})
            if route in ACTIONS:
                ACTIONS[route](payload)
                STATE.refresh(use_llm=False)
                return self.send_json({"ok": True, "version": STATE.version,
                                       "errore": STATE.error})
        except ValueError as exc:
            return self.fail(400, str(exc))
        except Exception as exc:                          # noqa: BLE001
            return self.fail(500, f"{type(exc).__name__}: {exc}")
        self.fail(404, "non trovato")


# --------------------------------------------------------------------------

def watch():
    """Rielabora appena un export compare o cambia nella cartella."""
    previous = STATE.signature()
    while True:
        time.sleep(WATCH_SECONDS)
        try:
            current = STATE.signature()
        except OSError:
            continue
        if current == previous:
            continue
        added = sorted(set(current) - set(previous))
        previous = current
        # Avvisa e basta. Elaborare da solo significava rischiare di leggere un
        # file ancora in copia; adesso decide il pulsante "Carica dati".
        if added:
            print(f"{len(added)} export in attesa: {', '.join(added)}")


def main():
    print(f"prima elaborazione da {FOLDER}")
    # Il primo giro salta l'LLM: con descrizioni nuove ogni chiamata a Ollama
    # costa una decina di secondi, e l'interfaccia resterebbe chiusa per
    # minuti. Il pulsante "Rielabora" lo attiva quando serve davvero.
    STATE.refresh(use_llm=False)
    if STATE.error:
        print(f"attenzione: {STATE.error}")
    else:
        print(f"{len(STATE.frame)} transazioni pronte")

    threading.Thread(target=watch, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"dashboard su {url}   (ctrl+c per fermare)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nchiuso")


if __name__ == "__main__":
    main()
