#!/usr/bin/env python3
"""
Server locale della dashboard.

    python server.py

Apre l'interfaccia nel browser. Da li' si correggono le categorie, si gestisce
la tassonomia, si scrivono le regole e si filtrano i grafici. Ogni modifica
finisce in uno dei CSV di configurazione e fa ripartire la pipeline.

Sorveglia anche la cartella: appena un export nuovo compare, rielabora da solo.

Ascolta su tutte le interfacce (serve al tunnel Cloudflare per raggiungerlo
da fuori casa), ma accetta solo due tipi di richiesta: quelle che arrivano
davvero da 127.0.0.1, e quelle che portano un JWT di Cloudflare Access
verificato (firma, scadenza, audience) per una delle email autorizzate.
Sono dati bancari: senza quel secondo controllo chiunque sulla LAN di casa
(o su una VPN attiva su questo PC) potrebbe raggiungerlo lo stesso.
Vedi local_only()/access_email().
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

import jwt
import pandas as pd

import bilancio
import dashboard

HOST = "0.0.0.0"
PORT = 8770
# webbrowser.open() deve restare qui, non su HOST: "0.0.0.0" non e' un
# indirizzo navigabile in tutti i browser, "127.0.0.1" lo e' sempre.
LOCAL_URL = f"http://127.0.0.1:{PORT}/"

# Ogni quanto guardare se sono comparsi export nuovi.
WATCH_SECONDS = 3

FOLDER = Path(__file__).parent

# Team, AUD e email autorizzate per Cloudflare Access vivono in
# access.json, FUORI da questo file: e' un dettaglio d'account (e le email
# sono dati personali di Fabio e Michela), e questo repo e' su GitHub. Il
# file e' in .gitignore; senza, l'accesso da fuori casa resta spento e in
# locale non cambia niente (vedi access_email()).
def load_access_config():
    path = FOLDER / "access.json"
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        return {
            "team_domain": config["team_domain"],
            "aud": config["aud"],
            "allowed_emails": {e.strip().lower() for e in config["allowed_emails"]},
            "jwks_client": jwt.PyJWKClient(
                f"https://{config['team_domain']}/cdn-cgi/access/certs"),
        }
    except (OSError, ValueError, KeyError) as exc:
        print(f"attenzione: access.json illeggibile ({exc}), "
              "l'accesso da fuori casa resta spento")
        return None


ACCESS = load_access_config()

# Intestazioni dei file di configurazione, per riscriverli senza perdere colonne.
SCHEMA = {
    "regole.csv": ["pattern", "categoria", "sottocategoria"],
    "merchant.csv": ["pattern", "merchant"],
    "natura.csv": ["categoria", "sottocategoria", "natura"],
    "override.csv": ["id", "data", "importo", "categoria", "sottocategoria",
                     "nota"],
    "correzioni.csv": ["id", "campo", "valore", "nota"],
    "escluse.csv": ["id", "motivo"],
    "quote.csv": ["id", "pagato_da", "quota", "nota"],
    "partita.csv": ["dal", "saldo", "controparte", "nota"],
    "categorie_merge.csv": ["categoria_attuale", "sottocategoria_attuale",
                            "transazioni", "categoria_finale",
                            "sottocategoria_finale"],
    "transazioni.csv": ["id", "data", "descrizione", "importo", "conto", "nota"],
}

# Il PRIMO riquadro acceso prende la colonna larga della dashboard e ne porta
# il peso; gli altri si impilano a destra. Cinque riquadri di prima -- natura,
# ricorrenti, voci, aree, merchant -- rispondevano tutti a "dove vanno i
# soldi" con le stesse righe misurate in modi diversi: adesso sono le tre
# viste di "dove". I loro id restano nei layout gia' salvati e vengono
# ignorati, perche' l'interfaccia disegna solo i riquadri che conosce.
DEFAULT_LAYOUT = [
    {"id": "dove", "titolo": "Dove vanno i soldi", "visibile": True},
    {"id": "leve", "titolo": "Le leve, dalla piu' facile", "visibile": True},
    {"id": "anno", "titolo": "Anno su anno", "visibile": True},
    {"id": "andamento", "titolo": "Uscite mese per mese", "visibile": True},
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
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
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
            # Le colonne grezze (bilancio.RAW_COLUMNS) sono un dettaglio
            # interno di read_consolidato(): servono a far ripartire una
            # correzione dal fatto vero quando l'estratto sparisce, non a
            # essere lette nel browser.
            visibili = [c for c in frame.columns if c not in bilancio.RAW_COLUMNS]
            clean = frame[visibili].where(pd.notna(frame[visibili]), None)
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
            "partita": bilancio.load_partita(FOLDER / "partita.csv"),
            "has_exports": self.has_exports(),
            # Se i dati vengono dal derivato invece che dagli export,
            # l'interfaccia deve dirlo: non e' un errore, ma neanche
            # una cosa da scoprire per caso.
            "derivato": not self.has_exports() and not self.frame.empty,
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

    if payload.get("elimina"):
        # Solo le righe scritte a mano: per un movimento di banca "sparire"
        # vorrebbe dire riapparire al prossimo giro (read_sources rilegge
        # sempre l'estratto, finche' c'e'), e se l'estratto non c'e' piu'
        # bilancio.read_consolidato() lo riprende comunque dal derivato. Chi
        # vuole togliere una riga di banca dalla vista usa "escludi", che
        # lascia scritto il perche'; una riga a mano invece non ha nessuna
        # fonte a monte, e transazioni.csv e' l'unico posto in cui vive.
        if not key.startswith("man-"):
            raise ValueError('solo le transazioni scritte a mano si possono '
                             'eliminare; per le altre usa "escludi"')
        write_rows("transazioni.csv", [r for r in read_rows("transazioni.csv")
                                       if (r.get("id") or "").strip() != key])
        write_rows("override.csv", [r for r in read_rows("override.csv")
                                    if (r.get("id") or "").strip() != key])
        write_rows("correzioni.csv", [r for r in read_rows("correzioni.csv")
                                      if (r.get("id") or "").strip() != key])
        write_rows("quote.csv", [r for r in read_rows("quote.csv")
                                 if (r.get("id") or "").strip() != key])
        write_rows("escluse.csv", [r for r in read_rows("escluse.csv")
                                   if (r.get("id") or "").strip() != key])
        return

    if "escludi" in payload:
        # Un motivo vuoto vuol dire "ripesca": la riga torna nel consolidato.
        rows = [r for r in read_rows("escluse.csv")
                if (r.get("id") or "").strip() != key]
        motivo = (payload.get("escludi") or "").strip()
        if motivo:
            rows.append({"id": key, "motivo": motivo})
        write_rows("escluse.csv", rows)
        return

    if "quota" in payload or "pagato_da" in payload:
        righe = [r for r in read_rows("quote.csv")
                 if (r.get("id") or "").strip() != key]
        quota = (payload.get("quota") or "").strip().lower()
        pagante = (payload.get("pagato_da") or "").strip().lower()
        # Un valore che non esiste non deve poter CANCELLARE quello buono: la
        # riga vecchia e' gia' stata tolta qui sopra, e nessuno dei due rami
        # seguenti la riscriverebbe. Si rifiuta prima di toccare il file.
        if quota and quota not in bilancio.QUOTE:
            raise ValueError(f"quota sconosciuta: {quota!r}")
        if pagante and pagante not in ("io", "lei"):
            raise ValueError(f"pagatore sconosciuto: {pagante!r}")
        # Servono tutti e due: "meta'" senza sapere chi ha pagato non dice da
        # che parte va il debito.
        if quota and pagante:
            righe.append({"id": key, "pagato_da": pagante, "quota": quota,
                          "nota": (payload.get("nota") or "").strip()})
        elif not quota and not pagante:
            # Svuotare i due menu e' una decisione, non un vuoto: se la riga
            # viene da un estratto conto ormai sparito, senza questa lapide
            # una quota dedotta da Splitwise nel derivato tornerebbe da sola
            # (vedi QUOTE in bilancio.py). Si scrive solo qui, non per le
            # righe che nessuno ha mai toccato.
            righe.append({"id": key, "pagato_da": "", "quota": "niente",
                          "nota": ""})
        write_rows("quote.csv", righe)
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
             not in {f.capitalize() for f in payload
                     if f.capitalize() in bilancio.CORREGGIBILI}]
    for field in (c.lower() for c in bilancio.CORREGGIBILI):
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
    """Scrive le copie anonime e cancella gli originali.

    Non si archiviano piu' in export/elaborati/: l'originale contiene IBAN e
    numeri di conto, e tenerlo vorrebbe dire lasciarlo dentro iCloud Drive
    per sempre. Dopo un caricamento riuscito bastano la copia senza IBAN in
    export/anonimi/ e il consolidato: read_consolidato() (bilancio.py) e'
    pensato apposta a riprendere da li' le transazioni il cui export non c'e'
    piu', e app.html lo dichiara ("sorgente: consolidato.csv").

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
        # La copia si scrive PRIMA di cancellare, rileggendo quel file: deve
        # rispecchiare quell'export, non il consolidato intero.
        #
        # Vale anche da controllo: load_transactions non solleva errori, su un
        # file illeggibile stampa "saltato" e torna vuoto. Senza questo, un
        # export che non e' stato letto verrebbe cancellato lo stesso e
        # sparirebbe dalla vista senza essere mai entrato nei conti.
        if bilancio.write_anonymous_copy(FOLDER, source, accounts) is None:
            skipped.append((name, "non ne e' stata letta nessuna transazione, "
                                  "controlla il formato del file"))
            continue
        # Un archivio scritto prima di questo cambio (export/elaborati/) puo'
        # ancora contenere un file con lo stesso nome: e' un riscarico, non un
        # export nuovo, e va ritirato per non farlo rileggere insieme al
        # nuovo (una transazione che la banca ha stornato resterebbe nel
        # bilancio per sempre). Il gemello si cerca fra TUTTI gli archiviati
        # rimasti, non solo nel mese corrente.
        twin = next((p for p in bilancio.archived_exports(FOLDER)
                     if p.name == name), None)
        if twin:
            retired = retire(twin)
            print(f"{name} sostituisce la copia caricata prima: quella e' ora "
                  f"in {bilancio.EXPORT_DIR}/{bilancio.SUPERSEDED_DIR}/"
                  f"{retired.name}")
        source.unlink()
        # Dentro iCloud Drive su Windows le operazioni sui file possono
        # tornare senza errore senza aver fatto niente. Dichiarare la
        # cancellazione riuscita a quel punto e' il danno peggiore: l'IBAN
        # resterebbe dentro iCloud senza che nessuno se ne accorga.
        if source.exists():
            skipped.append((name, "la cancellazione non e' andata a buon fine "
                                  "(iCloud Drive?)"))
            continue
        moved.append(name)
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
    """Elabora e, solo se e' andata bene, cancella gli originali."""
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
            f"{bilancio.EXPORT_DIR}/, non sono stati cancellati")
        return
    try:
        moved, skipped = archive_exports(names)
    except OSError as exc:
        say(f"cancellazione fallita ({exc}). I dati sono stati elaborati, "
            f"i file restano in {bilancio.EXPORT_DIR}/")
        return
    for name, motivo in skipped:
        say(f"{name} NON cancellato: {motivo}. "
            f"Resta in {bilancio.EXPORT_DIR}/")
    if moved:
        say(f"cancellati {len(moved)} export dopo il caricamento: "
            f"{', '.join(moved)}")
        say(f"copie senza IBAN in {bilancio.EXPORT_DIR}/{bilancio.ANON_DIR}/ "
            f"(attenzione: restano importi, date e negozi)")


def nuova_transazione(payload):
    """Aggiunge una riga a transazioni.csv, con un ID che non cambiera' mai.

    L'ID lo genera il server e non deriva dal contenuto: cosi' correggere
    l'importo di una riga scritta a mano non la stacca dalla sua categoria.
    """
    data = (payload.get("data") or "").strip()
    descrizione = (payload.get("descrizione") or "").strip()
    if not data or not descrizione:
        raise ValueError("servono data e descrizione")
    righe = read_rows("transazioni.csv")
    progressivo = len(righe) + 1
    while any((r.get("id") or "") == f"man-{data.replace('-','')}-{progressivo:02d}"
              for r in righe):
        progressivo += 1
    nuovo_id = f"man-{data.replace('-', '')}-{progressivo:02d}"
    righe.append({
        "id": nuovo_id,
        "data": data,
        "descrizione": descrizione,
        "importo": (payload.get("importo") or "0").strip(),
        "conto": (payload.get("conto") or "A mano").strip(),
        "nota": (payload.get("nota") or "").strip(),
    })
    write_rows("transazioni.csv", righe)
    # Torna l'id vero invece di lasciare che il browser lo indovini
    # cercando un segnaposto per testo (fragile: due creazioni di fila
    # senza aver ancora corretto la prima si confondevano fra loro).
    return {"id": nuovo_id}


ACTIONS = {
    "/api/transaction": set_transaction,
    "/api/categories": set_category,
    "/api/rules": set_rule,
    "/api/nuova": nuova_transazione,
}


def access_email(headers):
    """Email verificata dal JWT di Cloudflare Access, o None.

    Verifica la FIRMA (RS256 contro le chiavi pubbliche di Cloudflare), non
    solo la presenza dell'header: senza, chiunque sulla LAN di casa o su una
    VPN attiva su questo PC potrebbe scriversi a mano
    "Cf-Access-Jwt-Assertion: qualsiasi cosa" e passare. Controlla anche
    l'audience, cosi' un JWT valido ma emesso per un'ALTRA Access
    Application (es. "roccamora") non basta.

    Fallisce chiuso: qualunque eccezione - JWKS irraggiungibile, token
    scaduto, firma sbagliata, audience sbagliata - conta come "non
    autenticato", mai come errore da far esplodere fino al chiamante.
    """
    token = headers.get("Cf-Access-Jwt-Assertion")
    if not token or not ACCESS:
        return None
    try:
        key = ACCESS["jwks_client"].get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"],
                            audience=ACCESS["aud"])
    except Exception:                                       # noqa: BLE001
        return None
    email = (claims.get("email") or "").strip().lower()
    return email if email in ACCESS["allowed_emails"] else None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "Bilancio"

    def log_message(self, *args):
        pass                        # niente rumore nel terminale

    def local_only(self):
        """Locale, o autenticato via Cloudflare Access. Nient'altro.

        Il ramo locale e' la stessa difesa di sempre contro il DNS
        rebinding: un sito web non deve poter parlare con questo server
        usando il browser dell'utente come ponte, quindi conta solo un
        127.0.0.1 vero E un Host header locale. Esce prima e non tocca mai
        access_email(): l'uso da questo PC resta esattamente come prima,
        zero chiamate a Cloudflare per verificare un JWT.
        """
        if self.client_address[0] in ("127.0.0.1", "::1"):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host in ("127.0.0.1", "localhost", "[::1]", ""):
                return True
        if access_email(self.headers):
            return True
        self.fail(403, "accesso non autorizzato")
        return False

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
        if self.path.split("?")[0] == "/api/export.xlsx":
            # GET, non POST: e' un download vero, il browser deve poterlo
            # aprire da solo (un <a href>, non fetch+JSON) -- e deve
            # funzionare identico dal telefono sulla rete di casa, non solo
            # dal PC dove gira il server. Niente scritto su disco qui: il
            # file esiste solo nella risposta.
            if STATE.frame.empty:
                return self.fail(400, "nessun dato da esportare")
            body = dashboard.excel_bytes(STATE.frame)
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             'attachment; filename="bilancio.xlsx"')
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
            if route == "/api/scontrino":
                # Solo lettura: non scrive nessun CSV, quindi niente
                # STATE.refresh() dopo -- lo farebbe ripartire da capo la
                # pipeline intera per un giro che non ha cambiato niente.
                immagine = payload.get("immagine") or ""
                if not immagine:
                    return self.fail(400, "manca l'immagine")
                letto = bilancio.leggi_scontrino(immagine)
                if not letto.get("errore") and letto.get("negozio"):
                    letto.update(bilancio.suggerisci_categoria(
                        FOLDER, letto["negozio"]))
                return self.send_json(letto)
            if route in ACTIONS:
                risultato = ACTIONS[route](payload)
                STATE.refresh(use_llm=False)
                risposta = {"ok": True, "version": STATE.version,
                           "errore": STATE.error}
                # nuova_transazione() torna l'id appena creato; le altre
                # azioni non tornano niente (None) e questo resta un no-op
                # per loro -- nessun cambio di comportamento.
                if isinstance(risultato, dict):
                    risposta.update(risultato)
                return self.send_json(risposta)
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
    print(f"dashboard su {LOCAL_URL}   (ctrl+c per fermare)")
    if not ACCESS:
        print("attenzione: access.json mancante o incompleto, l'app e' "
              "raggiungibile solo da 127.0.0.1 (l'accesso da fuori casa via "
              "Cloudflare Access resta spento finche' non lo scrivi)")
    webbrowser.open(LOCAL_URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nchiuso")


if __name__ == "__main__":
    main()
