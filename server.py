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
import io
import json
import os
import re
import threading
import time
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
    "regole.csv": ["pattern", "categoria"],
    "merchant.csv": ["pattern", "merchant"],
    "natura.csv": ["categoria", "natura"],
    "override.csv": ["id", "data", "importo", "categoria", "nota"],
    "correzioni.csv": ["id", "campo", "valore", "nota"],
    "categorie_merge.csv": ["categoria_attuale", "transazioni",
                            "categoria_finale"],
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
        self.log = []
        self.frame = pd.DataFrame()
        self.again = False        # una modifica e' arrivata durante un giro

    def has_exports(self):
        """Ci sono estratti conto veri, o si lavora solo sullo storico?"""
        skip = bilancio.GENERATED
        return any(
            path.suffix.lower() in (".csv", ".xlsx", ".xls")
            and path.name.lower() not in skip
            and "report" not in path.name.lower()
            for path in self.folder.glob("*"))

    def signature(self):
        """Impronta degli export: cambia quando ne arriva o cambia uno."""
        skip = bilancio.GENERATED
        marks = {}
        for path in sorted(self.folder.glob("*")):
            if path.suffix.lower() not in (".csv", ".xlsx", ".xls"):
                continue
            if path.name.lower() in skip or "report" in path.name.lower():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            marks[path.name] = (int(stat.st_mtime), stat.st_size)
        return marks

    def refresh(self, use_llm=True):
        """Riesegue la pipeline. Un giro alla volta, gli altri si accodano."""
        if self.running:
            self.again = True
            return
        with self.lock:
            self.running, self.error = True, None
            buffer = io.StringIO()
            try:
                # Senza export si lavora sullo storico MoneyWiz, cosi'
                # l'interfaccia ha qualcosa da mostrare fin dal primo avvio.
                with redirect_stdout(buffer), redirect_stderr(buffer):
                    self.frame = bilancio.run(
                        self.folder, use_llm=use_llm,
                        from_history=not self.has_exports(),
                        make_dashboard=False)
            except Exception as exc:                      # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                buffer.write("\n" + traceback.format_exc())
            finally:
                self.log = buffer.getvalue().strip().split("\n")[-40:]
                self.version += 1
                self.running = False
        if self.again:
            self.again = False
            self.refresh(use_llm=use_llm)

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

        natura = {r["categoria"]: r["natura"] for r in read_rows("natura.csv")}
        totals = {}
        if not frame.empty:
            grouped = frame.groupby("Categoria").Importo.agg(["sum", "count"])
            totals = {str(k): {"totale": round(r["sum"], 2),
                               "n": int(r["count"])}
                      for k, r in grouped.iterrows()}

        names = sorted(set(natura) | set(totals))
        categories = [{"nome": n, "natura": natura.get(n, "Da classificare"),
                       "totale": totals.get(n, {}).get("totale", 0),
                       "n": totals.get(n, {}).get("n", 0)} for n in names]

        return {
            "version": self.version,
            "running": self.running,
            "error": self.error,
            "log": self.log,
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
            rows.append({"id": key, "data": payload.get("data", ""),
                         "importo": payload.get("importo", ""),
                         "categoria": category,
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

    if action == "natura":
        rows = [r for r in read_rows("natura.csv")
                if (r.get("categoria") or "").strip() != name]
        kind = (payload.get("natura") or "").strip()
        if kind:
            rows.append({"categoria": name, "natura": kind})
        write_rows("natura.csv", rows)
        return

    if action in ("rinomina", "unisci", "elimina"):
        target = (payload.get("destinazione") or "").strip()
        if action == "elimina":
            target = bilancio.IGNORE
        if not target:
            raise ValueError("destinazione mancante")
        rows = read_rows("categorie_merge.csv")
        found = False
        for row in rows:
            if (row.get("categoria_attuale") or "").strip() == name:
                row["categoria_finale"] = target
                found = True
            # Una categoria gia' rediretta su questa deve seguirla, altrimenti
            # dopo due rinomini si torna a puntare a un nome che non esiste.
            elif (row.get("categoria_finale") or "").strip() == name:
                row["categoria_finale"] = target
        if not found:
            rows.append({"categoria_attuale": name, "transazioni": "",
                         "categoria_finale": target})
        write_rows("categorie_merge.csv", rows)

        # Le regole che producevano il vecchio nome devono produrre il nuovo.
        rules = read_rows("regole.csv")
        for rule in rules:
            if (rule.get("categoria") or "").strip() == name:
                rule["categoria"] = target
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
        target = "merchant" if name == "merchant.csv" else "categoria"
        entry = {"pattern": pattern, target: value}
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
    target = (payload.get("valore") or "").strip()
    changed = hit[hit["Categoria"] != target] if target else hit
    return {
        "n": int(len(hit)),
        "cambierebbero": int(len(changed)),
        "totale": round(float(hit["Importo"].sum()), 2),
        "esempi": hit.head(8)[["Data", "Descrizione", "Importo", "Categoria"]]
        .to_dict("records"),
    }


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
                return self.send_json({"version": STATE.version,
                                       "running": STATE.running,
                                       "error": STATE.error})
            return self.send_json(STATE.payload())
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
            if route == "/api/run":
                STATE.refresh(use_llm=payload.get("llm", True))
                return self.send_json({"ok": True, "version": STATE.version,
                                       "errore": STATE.error})
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
        print(f"cartella cambiata{': ' + ', '.join(added) if added else ''}, "
              "rielaboro")
        # Qui l'LLM serve: un export nuovo porta descrizioni mai viste. Gira in
        # questo thread, l'interfaccia intanto mostra "elaboro".
        STATE.refresh(use_llm=True)


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
