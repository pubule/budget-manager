# Il conto fra Fabio e Michela — piano di realizzazione

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dare all'app le transazioni scritte a mano, l'esclusione di una riga qualsiasi, la dimensione «chi ha pagato / quanto deve l'altro» su ogni transazione, e un registro dei debiti fra Fabio e Michela.

**Architecture:** ogni spesa condivisa resta **una riga sola** che porta due colonne nuove, `Pagato da` e `Quota`. Per le righe Splitwise i due valori si **derivano dalle colonne persona** dell'export; per tutte le altre si dichiarano in `quote.csv`, agganciate all'ID come già fa `override.csv`. La dashboard le legge in due modi — `tutto` (il costo pieno, la lettura di oggi) e `la mia quota` — con un moltiplicatore che vive nel browser e non tocca la pipeline.

**Tech Stack:** Python 3.13 + pandas (`bilancio.py`), `http.server` stdlib (`server.py`), una SPA in un file solo senza dipendenze (`app.html`). Nessun framework di test: asserzioni dentro `selftest()` e controlli in `test_app.js`.

**Spec:** `docs/superpowers/specs/2026-08-25-conto-fra-due-design.md`

## Global Constraints

- **Italiano nei commenti e nell'interfaccia. Niente lettere accentate nei sorgenti** `.py`, `.js`, `.html`: si scrive `e'`, `piu'`, `cosi'`. Gli accenti veri stanno solo nei `.md` e nei dati.
- **I file di configurazione sono il database**: CSV con separatore `;`, encoding `utf-8-sig`, leggibili e modificabili a mano.
- **`consolidato.csv` resta un derivato**: lo riscrive `run()` a ogni giro e nessun altro lo scrive.
- **Nessun framework di test.** Le asserzioni Python stanno in `selftest()` e girano con `python bilancio.py --selfcheck`, che deve restare **sopra il 55%**. Il frontend si prova con `node test_app.js "$TEMP/bil/state.json"`, che deve restare **tutto verde**.
- **Non aprire mai gli export bancari originali** né le loro copie in `export/anonimi/`. Si lavora sui nomi delle colonne.
- **La lettura predefinita non deve spostare nessun numero di oggi.** Con l'interruttore su `tutto` i totali devono restare identici: è la prova che il modello non ha rotto niente.
- **La shell mangia le barre rovesce nelle heredoc.** Per modificare i sorgenti si usa lo strumento di modifica o uno script `.py` nello scratchpad, mai un heredoc con dentro `\n` o `\b`.
- Il server ascolta solo su `127.0.0.1`.

---

## Struttura dei file

| File | Responsabilità |
|---|---|
| `escluse.csv` *(nuovo)* | `id;motivo` — le righe che non devono comparire, ripescabili |
| `transazioni.csv` *(nuovo)* | `id;data;descrizione;importo;conto;nota` — le righe scritte a mano |
| `quote.csv` *(nuovo)* | `id;pagato_da;quota;nota` — chi ha pagato e quanto deve l'altro |
| `partita.csv` *(nuovo)* | `dal;saldo;controparte;nota` — una riga, il punto di partenza del registro |
| `bilancio.py` | i quattro caricatori, `apply_exclusions`, `apply_shares`, la derivazione da Splitwise, il travaso in `drop_covered_by`, due colonne in più nel consolidato |
| `server.py` | `SCHEMA` per i nuovi file, gli endpoint `/api/transaction` estesi, `/api/nuova`, `/api/esclude`, `/api/quota` |
| `app.html` | l'interruttore delle due letture, due colonne in Transazioni, il pulsante «+ transazione», la scheda «Con Michela» |
| `audit.py` | due controlli nuovi: quote orfane, e righe di banca marcate «pagata da lei» |
| `test_app.js` | i controlli sull'interruttore e sul registro |

---

## Task 1: Escludere una riga, e ripescarla

**Files:**
- Create: `escluse.csv`
- Modify: `bilancio.py` (accanto a `load_corrections`, e dentro `run()` dopo `apply_corrections`), `server.py` (`SCHEMA`, `set_transaction`), `app.html` (`VIEWS.transazioni`)

**Interfaces:**
- Produces: `load_exclusions(path) -> dict[str, str]` (id → motivo); `apply_exclusions(rows, exclusions, discarded) -> list[dict]`

- [ ] **Step 1: Scrivi il controllo che fallisce**

In `bilancio.py`, dentro `selftest()`, prima del blocco `# Un giroconto fra conti propri`:

```python
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
    assert buttate[0]["Scartata da"] == "esclusa a mano: doppione di luglio", \
        f"motivo perso: {buttate[0]['Scartata da']!r}"
    assert apply_exclusions(righe, {}, []) == righe, \
        "senza esclusioni non deve cambiare niente"
```

- [ ] **Step 2: Fallo girare per vedere che fallisce**

Run: `python bilancio.py --selfcheck`
Expected: `NameError: name 'apply_exclusions' is not defined`

- [ ] **Step 3: Scrivi il caricatore e il passo**

In `bilancio.py`, subito dopo `apply_corrections`:

```python
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
```

In `load_config()`, dentro il dizionario `config`, dopo la riga `"corrections": ...`:

```python
        "exclusions": load_exclusions(folder / "escluse.csv"),
```

In `run()`, subito dopo `apply_corrections(rows, config["corrections"])`:

```python
    # Prima di ogni altro scarto: una riga esclusa non deve nemmeno partecipare
    # agli appaiamenti, o consumerebbe la copertura di una riga buona.
    rows = apply_exclusions(rows, config["exclusions"], scartate)
```

- [ ] **Step 4: Fallo girare per vedere che passa**

Run: `python bilancio.py --selfcheck`
Expected: nessun `AssertionError`, e l'ultima riga resta `verifica: ... (58%)`

- [ ] **Step 5: Aggiungi il file al server**

In `server.py`, dentro `SCHEMA`:

```python
    "escluse.csv": ["id", "motivo"],
```

In `set_transaction()`, dopo il blocco `if payload.get("azzera"):`:

```python
    if "escludi" in payload:
        # Un motivo vuoto vuol dire "ripesca": la riga torna nel consolidato.
        rows = [r for r in read_rows("escluse.csv")
                if (r.get("id") or "").strip() != key]
        motivo = (payload.get("escludi") or "").strip()
        if motivo:
            rows.append({"id": key, "motivo": motivo})
        write_rows("escluse.csv", rows)
        return
```

- [ ] **Step 6: Il pulsante nell'interfaccia**

In `app.html`, in `VIEWS.transazioni`, aggiungi `"escludi"` in coda alla riga dell'ultima colonna, dopo il pulsante `annulla`:

```javascript
          `<button class="link" data-reset-tx="${esc(t.ID)}">annulla</button>`
          + ` <button class="link" data-escludi="${esc(t.ID)}">escludi</button>`]),
```

Nel selettore del gestore dei clic aggiungi `,[data-escludi]`, e prima di `if(d.resetTx)`:

```javascript
  if(d.escludi){
    const motivo = await domanda("Perche' questa riga non deve comparire?", "");
    if(!motivo) return;
    await post("/api/transaction", {id:d.escludi, escludi:motivo});
    return reload();
  }
```

- [ ] **Step 7: Prova sui dati veri**

Run: riavvia il server, apri Transazioni, escludi una riga qualsiasi, poi:
```
python -c "import csv;print(list(csv.DictReader(open('escluse.csv',encoding='utf-8-sig'),delimiter=';')))"
```
Expected: la riga con il suo motivo; la transazione sparita dall'elenco; `scartate.csv` la contiene con `esclusa a mano: <motivo>`. Togliendo la riga dal file e ricaricando, la transazione torna.

- [ ] **Step 8: Commit**

```bash
git add escluse.csv bilancio.py server.py app.html
git commit -m "feat: escludere una riga qualsiasi, e ripescarla"
```

---

## Task 2: Transazioni scritte a mano

**Files:**
- Create: `transazioni.csv`
- Modify: `bilancio.py` (`transaction_id`/`assign_ids`, `read_manual`, `run`), `server.py` (`SCHEMA`, `nuova_transazione`, `ACTIONS`), `app.html` (`VIEWS.transazioni`)

**Interfaces:**
- Consumes: `apply_exclusions` (Task 1)
- Produces: `read_manual(folder) -> list[dict]`; le righe manuali hanno `Rango = RANK_BANK` e un `ID` che comincia per `man-`

- [ ] **Step 1: Scrivi i controlli che falliscono**

In `selftest()`, subito dopo il blocco delle esclusioni:

```python
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

    # Le altre righe continuano a prendere l'ID dal contenuto.
    banca = [{"Data": "2026-01-15", "Descrizione": "spesa", "Importo": -12.0,
              "Conto": "Koala"}]
    assign_ids(banca)
    assert banca[0]["ID"] and "#" in banca[0]["ID"], "l'ID automatico non c'e' piu'"
```

- [ ] **Step 2: Fallo girare per vedere che fallisce**

Run: `python bilancio.py --selfcheck`
Expected: `AssertionError: l'ID scritto a mano e' stato sovrascritto`

- [ ] **Step 3: `assign_ids` rispetta un ID gia' presente**

In `bilancio.py`, sostituisci il corpo di `assign_ids`:

```python
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
```

- [ ] **Step 4: Fallo girare per vedere che passa**

Run: `python bilancio.py --selfcheck`
Expected: nessun `AssertionError`

- [ ] **Step 5: Leggi `transazioni.csv` come sorgente**

In `bilancio.py`, subito prima di `read_consolidato`:

```python
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
```

- [ ] **Step 6: Innestala senza disattivare il ripiego**

In `run()`, sostituisci il blocco che legge le sorgenti:

```python
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
```

E subito dopo `apply_corrections(rows, config["corrections"])`, prima di `apply_exclusions`:

```python
    # Le manuali entrano dopo l'assegnazione degli ID: il loro ID viene dal
    # file e assign_ids lo rispetta, ma non c'e' motivo di farle passare di li'.
    rows = rows + manuali
```

- [ ] **Step 7: L'endpoint che scrive**

In `server.py`, dentro `SCHEMA`:

```python
    "transazioni.csv": ["id", "data", "descrizione", "importo", "conto", "nota"],
```

Poi, prima del dizionario `ACTIONS`:

```python
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
    righe.append({
        "id": f"man-{data.replace('-', '')}-{progressivo:02d}",
        "data": data,
        "descrizione": descrizione,
        "importo": (payload.get("importo") or "0").strip(),
        "conto": (payload.get("conto") or "A mano").strip(),
        "nota": (payload.get("nota") or "").strip(),
    })
    write_rows("transazioni.csv", righe)
```

E dentro `ACTIONS`:

```python
    "/api/nuova": nuova_transazione,
```

- [ ] **Step 8: Il pulsante «+ transazione»**

In `app.html`, in `VIEWS.transazioni`, dentro `<div class="head">` dopo lo `<span class="mini">`:

```javascript
    + '<button class="link" data-nuova="1">+ transazione</button>'
```

Nel selettore dei clic aggiungi `,[data-nuova]`, e nel gestore:

```javascript
  if(d.nuova){
    // Quattro domande in fila invece di un modulo: il modale sa fare una
    // domanda per volta, e un modulo nuovo sarebbe l'unico dell'app.
    const data = await domanda("Data (aaaa-mm-gg):", today());
    if(!data) return;
    const descrizione = await domanda("Descrizione:", "");
    if(!descrizione) return;
    const importo = await domanda("Importo (negativo se e' una spesa):", "-");
    if(!importo) return;
    const conto = await domanda("Conto:", "A mano");
    if(!conto) return;
    await post("/api/nuova", {data, descrizione, importo, conto});
    return reload();
  }
```

- [ ] **Step 9: Prova sui dati veri**

Run: riavvia il server, premi «+ transazione», inserisci `2026-08-24 / Cena Alcide / -84 / Contanti`.
Expected: compare in Transazioni con una categoria proposta; `transazioni.csv` ha una riga con `id` che comincia per `man-`; correggendone l'importo dall'interfaccia l'ID resta lo stesso; escludendola sparisce e togliendola da `escluse.csv` torna.

- [ ] **Step 10: Commit**

```bash
git add transazioni.csv bilancio.py server.py app.html
git commit -m "feat: transazioni scritte a mano, con un ID che non cambia"
```

---

## Task 3: Dedurre chi ha pagato dalle colonne di Splitwise

**Files:**
- Modify: `bilancio.py` (accanto a `quota_columns`, dentro `load_transactions`)

**Interfaces:**
- Produces: `shares_from_quotas(costo, saldi) -> dict | None`, che torna `{"Pagato da": "io"|"lei", "Quota": "meta"|"tutto"}` oppure `None` se la riga non torna

- [ ] **Step 1: Scrivi i controlli che falliscono**

In `selftest()`, dopo il blocco delle transazioni a mano:

```python
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
```

- [ ] **Step 2: Fallo girare per vedere che fallisce**

Run: `python bilancio.py --selfcheck`
Expected: `NameError: name 'shares_from_quotas' is not defined`

- [ ] **Step 3: Scrivi la derivazione**

In `bilancio.py`, subito dopo `quota_columns`:

```python
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
    if abs(dovuta - abs(costo) / 2) < 0.51:
        return {"Pagato da": pagante, "Quota": "meta"}
    return None
```

- [ ] **Step 4: Fallo girare per vedere che passa**

Run: `python bilancio.py --selfcheck`
Expected: nessun `AssertionError`

- [ ] **Step 5: Portala nelle righe caricate**

In `load_transactions`, dopo la riga `shared = is_shared_export(frame)`:

```python
    # Le colonne persona: fino a oggi si buttavano. Contengono chi ha pagato e
    # quanto deve l'altro, per ogni riga condivisa.
    quote = quota_columns(frame) if shared else pd.DataFrame()
```

E dentro il ciclo, sostituisci il dizionario appeso a `rows` con:

```python
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
```

- [ ] **Step 6: Misura sui dati veri**

Aggiungi in coda a `load_transactions`, prima di `return rows`:

```python
    if shared:
        dedotte = sum(1 for r in rows if r.get("Quota"))
        print(f"    {dedotte} righe su {len(rows)} con pagatore e quota "
              f"dedotti dalle colonne persona")
```

Run: `python bilancio.py 2>&1 | grep "colonne persona"`
Expected: una riga con quante delle 657 righe Koala sono derivabili. **Se sono meno del 90%, fermati e riporta il numero**: vuol dire che il modello a due persone non descrive quel file, e il piano va rivisto prima di andare avanti.

- [ ] **Step 7: Commit**

```bash
git add bilancio.py
git commit -m "feat: pagatore e quota dedotti dalle colonne persona di Splitwise"
```

---

## Task 4: `quote.csv`, le due colonne, e il travaso

**Files:**
- Create: `quote.csv`
- Modify: `bilancio.py` (`load_shares`, `apply_shares`, `drop_covered_by`, `load_config`, `run`, `read_consolidato`, `columns`), `server.py` (`SCHEMA`, `set_transaction`)

**Interfaces:**
- Consumes: `shares_from_quotas` (Task 3)
- Produces: ogni riga del consolidato ha `Pagato da` (`""`, `io`, `lei`) e `Quota` (`""`, `meta`, `tutto`, `saldo`)

- [ ] **Step 1: Scrivi i controlli che falliscono**

In `selftest()`, dopo i controlli di Task 3:

```python
    # quote.csv vince sulla deduzione: e' una decisione presa da una persona
    # guardando la riga, la deduzione e' un'inferenza su un file.
    righe = [{"ID": "x#1", "Pagato da": "lei", "Quota": "meta"},
             {"ID": "y#1"}]
    apply_shares(righe, {"x#1": {"Pagato da": "io", "Quota": "tutto"}})
    assert righe[0]["Quota"] == "tutto", "quote.csv non ha vinto sulla deduzione"
    assert righe[0]["Pagato da"] == "io", "il pagatore di quote.csv non ha vinto"
    assert righe[1]["Quota"] == "", "una riga senza quota deve avere le colonne vuote"
    assert righe[1]["Pagato da"] == "", "il pagatore vuoto deve esserci comunque"

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
```

- [ ] **Step 2: Fallo girare per vedere che fallisce**

Run: `python bilancio.py --selfcheck`
Expected: `NameError: name 'apply_shares' is not defined`

- [ ] **Step 3: Il caricatore e il passo**

In `bilancio.py`, dopo `load_exclusions`:

```python
# I valori ammessi per la quota. "saldo" non e' una divisione: dice che QUELLA
# riga e' il rimborso, e muove il conto per intero.
QUOTE = ("meta", "tutto", "saldo")


def load_shares(path):
    """quote.csv: id -> {"Pagato da": ..., "Quota": ...}."""
    if not path.exists():
        return {}
    quote = {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter=";"):
            key = (row.get("id") or "").strip()
            quota = (row.get("quota") or "").strip().lower()
            pagante = (row.get("pagato_da") or "").strip().lower()
            if not key or quota not in QUOTE or pagante not in ("io", "lei"):
                continue
            quote[key] = {"Pagato da": pagante, "Quota": quota}
    if quote:
        print(f"  {len(quote)} quote dichiarate a mano")
    return quote


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
```

- [ ] **Step 4: Il travaso dentro `drop_covered_by`**

In `drop_covered_by`, sostituisci le due righe `used.add(...)` / `dropped.add(...)`:

```python
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
```

- [ ] **Step 5: Fallo girare per vedere che passa**

Run: `python bilancio.py --selfcheck`
Expected: nessun `AssertionError`

- [ ] **Step 6: Innesta nel giro e nel file**

In `load_config()`, dopo `"exclusions": ...`:

```python
        "shares": load_shares(folder / "quote.csv"),
```

In `run()`, dopo il ciclo di categorizzazione e prima di `categorizer.save_cache()`:

```python
    apply_shares(rows, config["shares"])
```

Sempre in `run()`, nella lista `columns`:

```python
    columns = ["ID", "Data", "Descrizione", "Merchant", "Importo", "Conto",
               "Natura", "Categoria", "Sottocategoria", "Pagato da", "Quota",
               "Origine", "Confidenza"]
```

E in `read_consolidato`, dentro il dizionario appeso a `rows`:

```python
            "Pagato da": _text(riga.get("Pagato da")),
            "Quota": _text(riga.get("Quota")),
```

- [ ] **Step 7: Un rimborso non diventa una spesa**

In `run()`, nel blocco che chiama `transfer_out`, sostituisci la condizione:

```python
        # Un rimborso in uscita ha la stessa forma di un giroconto spaiato, e
        # senza questa guardia finirebbe in "Da identificare".
        if (row.get("Quota") != "saldo"
                and transfer_out(category, row["Importo"], source) != category):
```

Aggiungi il controllo in `selftest()`:

```python
    assert transfer_out(TRANSFER, -500.0, "regola") == TRANSFER_OUT, \
        "senza quota un giroconto in uscita spaiato resta una spesa"
```

- [ ] **Step 8: L'endpoint per cambiare la quota**

In `server.py`, dentro `SCHEMA`:

```python
    "quote.csv": ["id", "pagato_da", "quota", "nota"],
```

In `set_transaction()`, dopo il blocco `if "escludi" in payload:`:

```python
    if "quota" in payload or "pagato_da" in payload:
        righe = [r for r in read_rows("quote.csv")
                 if (r.get("id") or "").strip() != key]
        quota = (payload.get("quota") or "").strip().lower()
        pagante = (payload.get("pagato_da") or "").strip().lower()
        # Servono tutti e due: "meta'" senza sapere chi ha pagato non dice da
        # che parte va il debito.
        if quota and pagante:
            righe.append({"id": key, "pagato_da": pagante, "quota": quota,
                          "nota": (payload.get("nota") or "").strip()})
        write_rows("quote.csv", righe)
        return
```

- [ ] **Step 9: Prova sui dati veri**

Run:
```
python bilancio.py 2>&1 | tail -8
python -c "import csv;r=list(csv.DictReader(open('consolidato.csv',encoding='utf-8-sig'),delimiter=';'));print(sum(1 for x in r if x['Quota']),'righe con quota su',len(r))"
```
Expected: i totali `entrate / uscite / risparmio` **identici a prima del task** (la quota non tocca ancora nessuna somma), e un conteggio di righe con quota vicino a quello misurato al Task 3.

- [ ] **Step 10: Commit**

```bash
git add quote.csv bilancio.py server.py
git commit -m "feat: quote.csv, due colonne nel consolidato e il travaso nella copertura"
```

---

## Task 5: L'interruttore delle due letture

**Files:**
- Modify: `app.html` (`F`, `quotaDi`, `spending`, la barra dei filtri, `render`), `test_app.js`

**Interfaces:**
- Consumes: le colonne `Pagato da` e `Quota` (Task 4)
- Produces: `quotaDi(t) -> number` (il moltiplicatore, 1 o 0.5); `F.lettura` (`""` = tutto, `"mia"` = la mia quota)

- [ ] **Step 0: Esponi le funzioni nuove al collaudo**

In `test_app.js`, nella lista che `eval()` ritorna, aggiungi `quotaDi` e
`registro` accanto a `indicatori, spesa, scarto, VISTE`. Senza, i controlli dei
passi seguenti non hanno niente da chiamare.

- [ ] **Step 1: Scrivi i controlli che falliscono**

In `test_app.js`, prima di `console.log("=== filtri ===")`:

```javascript
console.log("=== le due letture ===");
{
  const F6 = api.getF();
  const quota = (pagante, q) => api.quotaDi({"Pagato da":pagante, Quota:q});
  F6.lettura = "";
  check("nella lettura predefinita ogni riga pesa per intero",
        quota("io","meta") === 1 && quota("","") === 1);
  F6.lettura = "mia";
  check("a meta' la riga pesa la meta'", quota("io","meta") === 0.5);
  check("una riga non condivisa pesa uguale in tutte e due le letture",
        quota("","") === 1);
  // Ho pagato io e la quota e' tutta sua: di quella spesa non e' mio niente.
  check("quota intera a carico suo: non e' mia", quota("io","tutto") === 0);
  // Ha pagato lei e la quota e' tutta mia: e' mia per intero.
  check("quota intera a carico mio: e' tutta mia", quota("lei","tutto") === 1);
  check("un rimborso non e' una spesa e non pesa", quota("lei","saldo") === 0);
  F6.lettura = "";
}
```

- [ ] **Step 2: Fallo girare per vedere che fallisce**

Run: `node test_app.js "$TEMP/bil/state.json"`
Expected: `TypeError: api.quotaDi is not a function`

- [ ] **Step 3: Scrivi il moltiplicatore**

In `app.html`, subito dopo la definizione di `spesa`:

```javascript
// Quanto di una riga e' tuo. Nella lettura predefinita tutto: e' il costo
// pieno, ed e' quello che l'app ha sempre mostrato. Nella seconda lettura
// dipende da chi ha pagato e da quanto ne deve l'altro.
//
//   ho pagato io + meta' a lei    -> meta' e' mia
//   ho pagato io + tutto a lei    -> niente e' mio, l'ho solo anticipato
//   ha pagato lei + meta' a me    -> meta' e' mia
//   ha pagato lei + tutto a me    -> e' tutta mia
//   un saldo                      -> non e' una spesa, non pesa
function quotaDi(t){
  if(F.lettura !== "mia") return 1;
  const quota = t.Quota || "";
  if(!quota) return 1;
  if(quota === "saldo") return 0;
  const mia = t["Pagato da"] === "io"
    ? (quota === "tutto" ? 0 : 0.5)
    : (quota === "tutto" ? 1 : 0.5);
  return mia;
}
```

In `F`, aggiungi `lettura:""`.

- [ ] **Step 4: Fallo girare per vedere che passa**

Run: `node test_app.js "$TEMP/bil/state.json"`
Expected: `Tutti i controlli passati`

- [ ] **Step 5: Applica il moltiplicatore alle somme**

In `app.html`, sostituisci `sum`:

```javascript
// La somma passa dal moltiplicatore: e' l'unico punto da cui passano tutti i
// riquadri, quindi cambiarlo qui li cambia tutti insieme e nessuno puo'
// dimenticarsene.
const sum = rows => rows.reduce((s,t) => s + t.Importo * quotaDi(t), 0);
```

- [ ] **Step 6: I due pulsanti**

In `app.html`, dentro `#periodo`, dopo il blocco `f-confronto`:

```html
        <label id="f-lettura-eti">leggi</label>
        <div class="quick" id="f-lettura"></div>
```

E in `render()`, dopo `renderConfronti()`:

```javascript
  // Due letture degli stessi euro: quanto e' costato, e quanto e' tuo.
  el("f-lettura").innerHTML = [["", "tutto"], ["mia", "la mia quota"]]
    .map(([k, eti]) => `<button data-lettura="${k}" `
      + `class="${(F.lettura||"") === k ? "on" : ""}">${esc(eti)}</button>`)
    .join("");
```

Nel selettore dei clic aggiungi `,[data-lettura]`, e nel gestore:

```javascript
  if(d.lettura !== undefined){ F.lettura = d.lettura; render(); return; }
```

- [ ] **Step 7: La prova che conta**

Run: riavvia il server, apri la dashboard con l'interruttore su `tutto`, annota `uscite / mese`; poi confronta con
```
python bilancio.py 2>&1 | grep uscite
```
Expected: **lo stesso numero di prima del piano**. Poi passa a `la mia quota`: le uscite devono scendere, e la differenza e' la quota di Michela. Annota le due cifre: sono il risultato che il piano doveva produrre.

- [ ] **Step 8: Commit**

```bash
git add app.html test_app.js
git commit -m "feat: due letture degli stessi euro, tutto o la mia quota"
```

---

## Task 6: La scheda «Con Michela»

**Files:**
- Create: `partita.csv`
- Modify: `bilancio.py` (`load_partita`, dentro `load_config`), `server.py` (`SCHEMA`, `payload`), `app.html` (`TABS`, `VIEWS.partita`), `test_app.js`

**Interfaces:**
- Consumes: `quotaDi` (Task 5), le colonne `Pagato da` e `Quota` (Task 4)
- Produces: `registro(rows) -> {righe: [...], saldo: number, dal: string, controparte: string}`

- [ ] **Step 1: Scrivi i controlli che falliscono**

In `test_app.js`, dopo il blocco delle due letture:

```javascript
console.log("=== il registro con Michela ===");
{
  const finte = [
    // Prima della data di partenza: non deve entrare.
    {ID:"a", Data:"2025-12-31", Descrizione:"vecchia", Importo:-100,
     "Pagato da":"io", Quota:"meta"},
    {ID:"b", Data:"2026-01-12", Descrizione:"Eurospin", Importo:-60,
     "Pagato da":"io", Quota:"meta"},
    {ID:"c", Data:"2026-01-20", Descrizione:"Farmacia", Importo:-24,
     "Pagato da":"lei", Quota:"tutto"},
    {ID:"d", Data:"2026-02-03", Descrizione:"Bonifico", Importo:500,
     "Pagato da":"lei", Quota:"saldo"},
    {ID:"e", Data:"2026-02-04", Descrizione:"Spesa mia", Importo:-30},
  ];
  const r = api.registro(finte, {dal:"2026-01-01", saldo:0});
  check("il registro parte dalla data di partita.csv",
        !r.righe.some(x => x.ID === "a"), "c'e' una riga di prima");
  check("una riga senza quota non entra nel registro",
        !r.righe.some(x => x.ID === "e"));
  check("pago io la meta' sua: lei mi deve 30",
        r.righe.find(x => x.ID === "b").effetto === 30);
  check("paga lei una cosa tutta mia: le devo 24",
        r.righe.find(x => x.ID === "c").effetto === -24);
  check("il rimborso abbassa il debito di tutto l'importo",
        r.righe.find(x => x.ID === "d").effetto === -500);
  check("il saldo finale e' la somma degli effetti", r.saldo === 30 - 24 - 500,
        String(r.saldo));
  const ultima = r.righe[r.righe.length - 1];
  check("il saldo progressivo dell'ultima riga e' il totale",
        ultima.saldo === r.saldo, `${ultima.saldo} contro ${r.saldo}`);
}
```

- [ ] **Step 2: Fallo girare per vedere che fallisce**

Run: `node test_app.js "$TEMP/bil/state.json"`
Expected: `TypeError: api.registro is not a function`

- [ ] **Step 3: Scrivi il registro**

In `app.html`, prima di `VIEWS.dashboard`:

```javascript
// Il conto fra due persone. Positivo vuol dire che la controparte deve a te.
//
//   effetto = segno × quota × |importo|      segno = +1 se hai pagato tu
//
// Un "saldo" non e' una divisione: e' il rimborso stesso, e muove il conto per
// tutto l'importo nel verso opposto a chi lo manda.
function registro(rows, partita){
  const dal = (partita && partita.dal) || "";
  const dentro = rows.filter(t => t.Quota && (!dal || (t.Data||"") >= dal))
    .sort((a,b) => (a.Data||"").localeCompare(b.Data||""));
  let saldo = (partita && partita.saldo) || 0;
  const righe = dentro.map(t => {
    const pieno = Math.abs(t.Importo);
    const segno = t["Pagato da"] === "io" ? 1 : -1;
    const parte = t.Quota === "saldo" ? 1 : (t.Quota === "tutto" ? 1 : 0.5);
    const effetto = t.Quota === "saldo"
      ? -segno * pieno * (t.Importo > 0 ? 1 : -1) * (t["Pagato da"] === "lei" ? 1 : -1)
      : segno * parte * pieno;
    saldo = Math.round((saldo + effetto) * 100) / 100;
    return {...t, effetto, saldo};
  });
  return {righe, saldo, dal,
          controparte: (partita && partita.controparte) || "la controparte"};
}
```

> Nota per chi implementa: la formula del `saldo` sopra e' scritta a passi
> perche' i controlli dicano cosa vuole. Semplificala **solo** se i controlli
> restano verdi: il caso `d` del test (rimborso da lei, importo positivo,
> effetto −500) e' quello che tiene insieme i segni.

- [ ] **Step 4: Fallo girare per vedere che passa**

Run: `node test_app.js "$TEMP/bil/state.json"`
Expected: `Tutti i controlli passati`

- [ ] **Step 5: Il punto di partenza**

Crea `partita.csv`:

```
dal;saldo;controparte;nota
2026-01-01;0,00;Michela;
```

In `bilancio.py`, dopo `load_shares`:

```python
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
```

In `load_config()`, dopo `"shares": ...`:

```python
        "partita": load_partita(folder / "partita.csv"),
```

In `server.py`, dentro `SCHEMA`:

```python
    "partita.csv": ["dal", "saldo", "controparte", "nota"],
```

E dentro `payload()`, accanto a `"layout": read_layout(),`:

```python
            "partita": bilancio.load_partita(FOLDER / "partita.csv"),
```

- [ ] **Step 6: La scheda**

In `app.html`, in `TABS`, dopo `["transazioni", "Transazioni"]`:

```javascript
  ["partita", "Con Michela"],
```

E la vista, dopo `VIEWS.transazioni`:

```javascript
VIEWS.partita = rows => {
  const r = registro(rows, S.partita);
  if(!r.righe.length)
    return '<div class="card"><div class="head"><h2>Con '
      + esc(r.controparte) + '</h2></div><p class="note">Nessuna spesa '
      + 'divisa da ' + esc(r.dal || "sempre") + '. Segna chi ha pagato e la '
      + 'quota nella scheda <b>Transazioni</b>.</p></div>';
  const verso = r.saldo >= 0
    ? `${esc(r.controparte)} ti deve` : `devi a ${esc(r.controparte)}`;
  const CHI = {io:"io", lei:esc(r.controparte)};
  return '<div class="card"><div class="head"><h2>Con '
    + esc(r.controparte) + '</h2>'
    + `<span class="mini">dal ${esc(r.dal)}, saldo di partenza `
    + `${euro(S.partita.saldo)}</span></div>`
    + `<p class="verdetto-partita">${verso} <b>${euro(Math.abs(r.saldo))}</b></p>`
    + tableHTML(["data","descrizione","importo","chi ha pagato","quota",
                 "effetto","saldo"],
        r.righe.map(t => [
          esc(t.Data), esc((t.Descrizione||"").slice(0,52)),
          spesa(t.Importo), CHI[t["Pagato da"]] || "—", esc(t.Quota),
          `<span class="delta ${t.effetto >= 0 ? "meglio" : "peggio"}">`
            + `${t.effetto >= 0 ? "+" : ""}${euro(t.effetto)}</span>`,
          euro(t.saldo)]),
        [2,5,6])
    + '<p class="note">Positivo vuol dire che '
    + esc(r.controparte) + ' deve a te. Un <b>saldo</b> e\' il rimborso '
    + 'stesso: non e\' una spesa, e muove il conto per intero.</p></div>';
};
```

Aggiungi lo stile, accanto a `.avviso`:

```css
.verdetto-partita{font-size:26px;margin:0 0 14px;letter-spacing:-.01em}
.verdetto-partita b{font-variant-numeric:tabular-nums}
```

- [ ] **Step 7: I due menu in Transazioni**

In `VIEWS.transazioni`, aggiungi due voci alle intestazioni dopo `"categoria"` — `"pagato da"`, `"quota"` — e due celle corrispondenti:

```javascript
          `<select data-quota-chi="${esc(t.ID)}">`
            + ["", "io", "lei"].map(v =>
                `<option value="${v}" ${v===(t["Pagato da"]||"")?"selected":""}>`
                + `${v || "—"}</option>`).join("") + "</select>",
          `<select data-quota="${esc(t.ID)}">`
            + ["", "meta", "tutto", "saldo"].map(v =>
                `<option value="${v}" ${v===(t.Quota||"")?"selected":""}>`
                + `${v || "—"}</option>`).join("") + "</select>",
```

E nel gestore dei `change`, prima di `if(d.fix)`:

```javascript
  if(d.quota !== undefined || d.quotaChi !== undefined){
    // Servono tutti e due: "meta'" senza sapere chi ha pagato non dice da che
    // parte va il debito, quindi si mandano sempre insieme.
    const id = d.quota || d.quotaChi;
    const chi = document.querySelector(`[data-quota-chi="${id}"]`).value;
    const q = document.querySelector(`[data-quota="${id}"]`).value;
    await post("/api/transaction", {id, pagato_da:chi, quota:q});
    return reload();
  }
```

- [ ] **Step 8: Prova sui dati veri**

Run: riavvia il server, apri «Con Michela».
Expected: il saldo compare in cima, l'elenco parte dal 1° gennaio 2026, e il saldo dell'ultima riga e' uguale al totale. Cambiando «pagato da» su una riga in Transazioni, il saldo si muove del doppio della quota.

- [ ] **Step 9: Commit**

```bash
git add partita.csv bilancio.py server.py app.html test_app.js
git commit -m "feat: la scheda Con Michela, il registro dei debiti fra due persone"
```

---

## Task 7: I due controlli in `audit.py`

**Files:**
- Modify: `audit.py`

**Interfaces:**
- Consumes: le colonne `Pagato da` e `Quota` del consolidato (Task 4)

- [ ] **Step 1: Scrivi i due controlli**

In `audit.py`, dopo `controlla_giroconti`:

```python
def controlla_quote(righe, r):
    """Le quote che non tornano: orfane, incomplete, o in contraddizione."""
    quote = [x for x in righe if (x.get("Quota") or "")]
    r.aggiungi("nota", "righe con una quota", len(quote),
               [f"{sum(1 for x in quote if x.get('Quota') == q)} {q}"
                for q in ("meta", "tutto", "saldo")])

    # Meta' di che, pagata da chi? Una quota senza pagatore non dice da che
    # parte va il debito, e nel registro sparisce in silenzio.
    monche = [x for x in quote if not (x.get("Pagato da") or "")]
    r.aggiungi("errore", "quote senza chi ha pagato", len(monche),
               [f"{x.get('Data')} {(x.get('Descrizione') or '')[:40]}"
                for x in monche])

    # Una riga arrivata dall'estratto conto dice gia' chi ha pagato: sei stato
    # tu, e' il tuo conto. Marcarla "pagata da lei" e' una contraddizione, ma
    # ha una spiegazione vera (carta tua, spesa sua) e una sbagliata (un clic
    # di troppo). Non si vieta e non si corregge: si conta.
    banca = {"UniCredit", "Hype", "Fideuram", "Intesa Sanpaolo", "Fineco"}
    contrarie = [x for x in quote
                 if x.get("Pagato da") == "lei" and x.get("Conto") in banca]
    r.aggiungi("sospetto", "righe di banca marcate 'pagata da lei'",
               len(contrarie),
               [f"{x.get('Data')} {x.get('Conto')} "
                f"{(x.get('Descrizione') or '')[:34]}" for x in contrarie],
               "se sono tre e' un refuso, se sono trenta e' un modo di usare "
               "l'app che il modello deve descrivere")
```

E aggiungila alla tupla dei controlli dentro `main()`.

- [ ] **Step 2: Fallo girare**

Run: `python audit.py`
Expected: le tre voci nuove, e nessun errore fra quelli vecchi.

- [ ] **Step 3: Commit**

```bash
git add audit.py
git commit -m "feat: audit delle quote, orfane e in contraddizione"
```

---

## Chiusura

- [ ] **Regressione completa**

```bash
python bilancio.py --selfcheck    # sopra il 55%
node test_app.js "$TEMP/bil/state.json"   # tutto verde
python audit.py                   # nessun errore nuovo
```

- [ ] **Aggiorna `HANDOFF.md`** con una sezione sul modello a due letture, la derivazione dalle colonne persona, e le tre trappole (travaso nella copertura, ripiego legato ai soli export, `saldo` che non deve diventare una spesa).

- [ ] **Annota i due numeri** che il piano doveva produrre: quante righe Splitwise sono derivabili, e di quanto scendono le uscite passando da `tutto` a `la mia quota`.
