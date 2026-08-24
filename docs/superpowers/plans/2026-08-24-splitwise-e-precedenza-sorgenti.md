# Splitwise e precedenza fra sorgenti — piano di implementazione

> **Per chi esegue:** SOTTO-SKILL RICHIESTA: usa
> `superpowers:subagent-driven-development` (consigliata) oppure
> `superpowers:executing-plans` per implementare questo piano un compito alla
> volta. I passi usano caselle (`- [ ]`) per il tracciamento.

**Obiettivo:** leggere l'export Splitwise come spese a costo pieno, migrare lo
storico MoneyWiz una volta sola e stabilire una precedenza fra sorgenti che
descrivono lo stesso acquisto.

**Architettura:** tre interventi su `bilancio.py`, che resta l'unico posto dove
le transazioni entrano. Un riconoscitore di export condivisi basato su una
firma numerica; un comando una-tantum che estrae dallo storico ciò che non
viene da Splitwise; un passo di deduplicazione fra sorgenti modellato su
`drop_internal_transfers()`, che già funziona.

**Tecnologie:** Python 3.13, pandas, sqlite3 (stdlib). Nessuna dipendenza
nuova.

**Spec:** `docs/superpowers/specs/2026-08-24-splitwise-e-precedenza-sorgenti-design.md`

## Vincoli globali

- **Non esiste pytest in questo progetto.** I test Python vivono dentro
  `selftest()` in `bilancio.py` (righe 901-1000) come asserzioni, e si
  lanciano con `python bilancio.py --selfcheck`. Il ciclo TDD è: aggiungi
  l'asserzione → lancia `--selfcheck` → vedila fallire → implementa → rilancia
  → passa → commit. **Non introdurre pytest né una cartella `tests/`.**
- I test dell'interfaccia stanno in `test_app.js` e si lanciano con
  `node test_app.js <state.json>`.
- Soglie da non far scendere: `--selfcheck` sopra il **55%**, `test_app.js`
  tutto verde.
- Commenti e messaggi in **italiano**, senza lettere accentate nel codice
  sorgente (il resto del file usa `e'`, `piu'`, `cosi'`).
- `consolidato.csv` è **derivato**: nessun codice ci scrive dentro se non
  `run()`.
- La cartella `export/` è esclusa da git. Non committare mai file di dati.
- Lo `state.json` per i test va in `$TEMP`, mai nella cartella del progetto.
- Non toccare mai il backup MoneyWiz in `backup/`: si legge, non si scrive.

---

## Struttura dei file

| File | Responsabilità | Interventi |
|---|---|---|
| `bilancio.py` | lettura, categorizzazione, deduplicazione | tutti i compiti tranne il 6 |
| `server.py` | stato, API, sorveglianza | compito 4 |
| `app.html` | interfaccia | compito 4 |
| `README.txt` | istruzioni d'uso | compiti 3, 4 |
| `HANDOFF.md` | stato del lavoro e trappole | compito 6 |

Nessun file nuovo. Tutte le funzioni nuove stanno in `bilancio.py` accanto a
quelle che estendono, seguendo l'organizzazione esistente: i lettori vicino a
`load_transactions()`, i passi di pulizia vicino a
`drop_cross_file_duplicates()`.

---

## Compito 1: riconoscere un export di spese condivise

**File:**
- Modifica: `bilancio.py` — nuova `is_shared_export()` subito dopo
  `find_columns()` (riga 767-785)
- Test: `bilancio.py` — `selftest()` (righe 901-1000)

**Interfacce:**
- Consuma: `find_columns(frame) -> (date, desc, amount, debit, credit)`, già
  esistente a riga 767.
- Produce: `is_shared_export(frame) -> bool`. Usata dai compiti 2 e 3.

- [ ] **Passo 1: scrivi l'asserzione che fallisce**

In `selftest()`, subito prima di `print("selftest: ok")`:

```python
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
```

- [ ] **Passo 2: lancia il test e verifica che fallisca**

Esegui: `python bilancio.py --selfcheck`
Atteso: `NameError: name 'is_shared_export' is not defined`

- [ ] **Passo 3: scrivi l'implementazione minima**

In `bilancio.py`, dopo `find_columns()` (cioè dopo la riga 785):

```python
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
```

- [ ] **Passo 4: lancia il test e verifica che passi**

Esegui: `python bilancio.py --selfcheck`
Atteso: `selftest: ok`, e la riga `verifica: ... corrette` sopra il 55%

- [ ] **Passo 5: verifica sul file vero**

Esegui:

```bash
python -c "
import pandas as pd, bilancio
f = pd.read_csv('export/koala_2026-08-24_export.csv', dtype=str)
print('riconosciuto:', bilancio.is_shared_export(f))
"
```

Atteso: `riconosciuto: True`

- [ ] **Passo 6: commit**

```bash
git add bilancio.py
git commit -m "Riconosce gli export di spese condivise dalla firma numerica"
```

---

## Compito 2: costo pieno, segno negativo, saldi scartati

**File:**
- Modifica: `bilancio.py` — `load_transactions()` (righe 786-826)
- Modifica: `bilancio.py` — nuova costante e nuova `is_settlement()` accanto a
  `is_shared_export()`
- Test: `bilancio.py` — `selftest()`

**Interfacce:**
- Consuma: `is_shared_export(frame) -> bool` dal compito 1.
- Produce: `is_settlement(description, category) -> bool`. `load_transactions()`
  mantiene la firma `(path, account=None) -> list[dict]`.

- [ ] **Passo 1: scrivi le asserzioni che falliscono**

In `selftest()`, dopo le asserzioni del compito 1:

```python
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
```

- [ ] **Passo 2: lancia il test e verifica che fallisca**

Esegui: `python bilancio.py --selfcheck`
Atteso: `NameError: name 'is_settlement' is not defined`

- [ ] **Passo 3: scrivi l'implementazione minima**

In `bilancio.py`, accanto a `is_shared_export()`:

```python
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
```

- [ ] **Passo 4: lancia il test e verifica che passi**

Esegui: `python bilancio.py --selfcheck`
Atteso: `selftest: ok`

- [ ] **Passo 5: aggancia a `load_transactions()`**

In `load_transactions()`, sostituisci il corpo fra
`if not date_col:` e `rows = []` e il ciclo, così:

```python
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
```

- [ ] **Passo 6: verifica sul file vero**

Esegui:

```bash
python -c "
import bilancio
from pathlib import Path
r = bilancio.load_transactions(Path('export/koala_2026-08-24_export.csv'), 'Splitwise')
print('righe:', len(r))
print('tutte negative:', all(x['Importo'] < 0 for x in r))
print('totale:', round(sum(x['Importo'] for x in r), 2))
print('saldo da 5373 presente?', any(abs(x['Importo']) > 5000 for x in r))
"
```

Atteso: `righe: 666`, `tutte negative: True`, `saldo da 5373 presente? False`,
e nell'output i due saldi scartati per 5.988,94.

- [ ] **Passo 7: commit**

```bash
git add bilancio.py
git commit -m "Splitwise: costo pieno negativo, saldi fuori dai conti"
```

---

## Compito 3: migrazione dello storico MoneyWiz

**File:**
- Modifica: `bilancio.py` — nuove `shared_shares()` e `migrate_history()` dopo
  `load_history_transactions()` (riga 705-739)
- Modifica: `bilancio.py` — `main()` (riga 1257), nuova opzione
  `--migra-storico`
- Modifica: `README.txt`
- Test: `bilancio.py` — `selftest()`

**Interfacce:**
- Consuma: `is_shared_export(frame)` (compito 1);
  `load_history_transactions(db_path) -> list[dict]` (riga 705);
  `archived_exports(folder)` (riga 1122); `EXPORT_DIR`, `ARCHIVE_DIR`.
- Produce: `migrate_history(folder, db_path) -> Path | None`, scrive
  `export/elaborati/storico-moneywiz.csv`.

- [ ] **Passo 1: scrivi l'asserzione che fallisce**

In `selftest()`:

```python
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
```

- [ ] **Passo 2: lancia il test e verifica che fallisca**

Esegui: `python bilancio.py --selfcheck`
Atteso: `NameError: name 'drop_shared_halves' is not defined`

- [ ] **Passo 3: scrivi l'implementazione minima**

In `bilancio.py`, dopo `load_history_transactions()`:

```python
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

    removed = set()
    for day, share in shares:
        if not day or not share:
            continue
        taken = 0
        for offset in (0, -1, 1):
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
```

- [ ] **Passo 4: lancia il test e verifica che passi**

Esegui: `python bilancio.py --selfcheck`
Atteso: `selftest: ok`

- [ ] **Passo 5: aggiungi l'opzione a `main()`**

In `main()`, accanto alle altre `parser.add_argument`:

```python
    parser.add_argument("--migra-storico", action="store_true",
                        help="estrae lo storico MoneyWiz in export/elaborati/ "
                             "e smette di usarlo come sorgente")
```

E subito dopo il blocco `if args.selfcheck:`:

```python
    if args.migra_storico:
        print("migrazione dello storico MoneyWiz")
        config = load_config(folder)
        migrate_history(folder, config["db_path"])
        return
```

- [ ] **Passo 6: esegui la migrazione vera e controlla i numeri**

Esegui: `python bilancio.py --migra-storico`

Atteso, secondo la misura fatta in fase di progetto:

```
1027 righe riconosciute come meta' condivise
906 righe superstiti: 797 uscite per -87.563,xx, 109 entrate
```

Uno scarto di poche unità è accettabile (dipende dall'arrotondamento delle
date). Uno scarto di decine significa che il criterio non regge: **fermati e
segnalalo** invece di proseguire.

Verifica che i superstiti siano ciò che Splitwise non ha:

```bash
python -c "
import pandas as pd
d = pd.read_csv('export/elaborati/storico-moneywiz.csv', sep=';', encoding='utf-8-sig')
print(len(d), 'righe')
print(d[d.Importo > 0].Descrizione.value_counts().head().to_string())
"
```

Atteso fra le entrate: `VOSTRI EMOLUMENTI`, `Affitti incassati`,
`Bonifici ricevuti`.

- [ ] **Passo 7: documenta in `README.txt`**

Nella sezione `DA DOVE VENGONO LE CATEGORIE`, dopo il paragrafo esistente:

```
Le TRANSAZIONI dello storico sono state migrate una volta sola in
export/elaborati/storico-moneywiz.csv con:

    python bilancio.py --migra-storico

Da quel file in poi lo storico serve SOLO come maestro delle categorie. Non
rilanciare la migrazione: creerebbe doppioni. Se serve rifarla, cancella
prima export/elaborati/storico-moneywiz.csv.
```

- [ ] **Passo 8: commit**

```bash
git add bilancio.py README.txt
git commit -m "Migrazione una-tantum dello storico MoneyWiz in export/"
```

---

## Compito 4: lo storico smette di essere sorgente

**File:**
- Modifica: `bilancio.py` — `read_sources()` (riga 1149), `run()` (riga 1174),
  `main()` (riga 1257)
- Modifica: `server.py` — `refresh()` (riga 223), `load_and_archive()` (riga 503)
- Modifica: `app.html` — la riga `sorgente:` in `render()`
- Modifica: `README.txt`

**Interfacce:**
- Consuma: `migrate_history()` dal compito 3 già eseguita.
- Produce: `read_sources(folder, config, output, include_pending=False)` e
  `run(folder, use_llm=True, output=..., make_dashboard=True, config=None,
  include_pending=False)` — **il parametro `from_history` sparisce da
  entrambe**. `State.refresh(use_llm=True, include_pending=False)` invariata
  nella firma.

- [ ] **Passo 1: togli `from_history` da `read_sources()`**

In `bilancio.py`, sostituisci le prime righe di `read_sources()`:

```python
def read_sources(folder, config, output, include_pending=False):
    """Le transazioni grezze, dagli export in export/.

    Lo storico MoneyWiz non e' piu' una sorgente: le sue transazioni sono
    state migrate una volta in export/elaborati/storico-moneywiz.csv, e
    quello che resta (i 1635 esempi etichettati) alimenta solo il motore di
    categorizzazione.
    """
    print("EXPORT")
```

Il resto del corpo resta invariato.

- [ ] **Passo 2: togli `from_history` da `run()`**

Firma:

```python
def run(folder, use_llm=True, output="consolidato.csv",
        make_dashboard=True, config=None, include_pending=False):
```

Chiamata:

```python
    rows = read_sources(folder, config, output, include_pending)
    if not rows:
        raise RuntimeError(
            f"nessuna transazione: metti gli export in {EXPORT_DIR}/ e premi "
            "Carica dati. Se e' la prima volta, esegui prima "
            "python bilancio.py --migra-storico")
```

- [ ] **Passo 3: togli `--da-storico` da `main()`**

Elimina il blocco `parser.add_argument("--da-storico", ...)` e togli
`from_history=args.da_storico` dalla chiamata a `run()`.

- [ ] **Passo 4: aggiorna `server.py`**

In `refresh()` (riga 223 circa), la chiamata diventa:

```python
                    self.frame = bilancio.run(
                        self.folder, use_llm=use_llm,
                        make_dashboard=False,
                        include_pending=include_pending)
```

`has_exports()` resta invariata: distingue ancora fra "ci sono export
caricati" e "no", e ora quel "no" significa dashboard vuota.

- [ ] **Passo 5: aggiorna il messaggio in `app.html`**

Nella funzione `render()`, sostituisci:

```javascript
    (S.has_exports ? "sorgente: export/"
                   : "sorgente: storico MoneyWiz (nessun export in export/)")
```

con:

```javascript
    (S.has_exports ? "sorgente: export/"
                   : "nessun export caricato: metti i file in export/ e premi Carica")
```

- [ ] **Passo 6: verifica**

Esegui: `python bilancio.py --no-llm --no-dashboard`

Atteso: legge `export/elaborati/storico-moneywiz.csv` e l'export Splitwise,
**circa 1.572 transazioni**, nessun riferimento allo storico come sorgente.

Poi, con la cartella vuota:

```bash
mkdir -p "$TEMP/vuota" && python bilancio.py -f "$TEMP/vuota" --no-llm --no-dashboard
```

Atteso: il messaggio "nessuna transazione: metti gli export in export/…",
non un ripiego sullo storico.

- [ ] **Passo 7: aggiorna `README.txt`**

Togli la riga `python bilancio.py --da-storico    usa MoneyWiz invece degli
export` dall'elenco dei comandi, e la frase "Finche' non ci sono export nella
cartella, la dashboard lavora sullo storico MoneyWiz" dalla sezione
`DA DOVE VENGONO LE CATEGORIE`.

- [ ] **Passo 8: commit**

```bash
git add bilancio.py server.py app.html README.txt
git commit -m "Lo storico MoneyWiz non e' piu' una sorgente di transazioni"
```

---

## Compito 5: precedenza fra sorgenti

**File:**
- Modifica: `bilancio.py` — nuove `source_rank()` e `drop_covered_by()` dopo
  `drop_cross_file_duplicates()` (riga 828-858)
- Modifica: `bilancio.py` — `load_transactions()`, aggiunge `Rango` alla riga
- Modifica: `bilancio.py` — `run()`, inserisce il passo
- Test: `bilancio.py` — `selftest()`

**Interfacce:**
- Consuma: `is_shared_export(frame)` (compito 1).
- Produce: `source_rank(path, shared) -> int` e
  `drop_covered_by(rows, days=3) -> list[dict]`. Le righe acquisiscono la
  chiave interna `"Rango"`, che **non** compare in `consolidato.csv` (le
  colonne di uscita sono già selezionate esplicitamente in `run()`).

- [ ] **Passo 1: scrivi le asserzioni che falliscono**

In `selftest()`:

```python
    # Fra sorgenti che descrivono lo stesso acquisto vince l'estratto conto:
    # se Fabio paga Esselunga con la carta, l'uscita e' gia' li'.
    righe = [
        {"Data": "2026-01-12", "Descrizione": "PAGAMENTO POS ESSELUNGA",
         "Importo": -60.0, "Conto": "Intesa", "Rango": 0},
        {"Data": "2026-01-13", "Descrizione": "Eurospin",
         "Importo": -60.0, "Conto": "Splitwise", "Rango": 1},
        {"Data": "2026-01-12", "Descrizione": "Spesa di Michela",
         "Importo": -25.0, "Conto": "Splitwise", "Rango": 1},
    ]
    resto = drop_covered_by(righe)
    assert len(resto) == 2, f"attese 2 righe, trovate {len(resto)}"
    assert not any(r["Descrizione"] == "Eurospin" for r in resto), \
        "la riga condivisa coperta dalla banca doveva sparire"
    assert any(r["Descrizione"] == "Spesa di Michela" for r in resto), \
        "cio' che la banca non vede va tenuto: e' il motivo per cui Splitwise serve"

    # La banca non viene mai scartata da una sorgente piu' bassa.
    solo_banca = [
        {"Data": "2026-01-12", "Descrizione": "A", "Importo": -60.0,
         "Conto": "Intesa", "Rango": 0},
        {"Data": "2026-01-12", "Descrizione": "B", "Importo": -60.0,
         "Conto": "Intesa", "Rango": 0},
    ]
    assert len(drop_covered_by(solo_banca)) == 2, \
        "due movimenti bancari uguali sono due spese, non un doppione"
```

- [ ] **Passo 2: lancia il test e verifica che fallisca**

Esegui: `python bilancio.py --selfcheck`
Atteso: `NameError: name 'drop_covered_by' is not defined`

- [ ] **Passo 3: scrivi l'implementazione minima**

In `bilancio.py`, dopo `drop_cross_file_duplicates()`:

```python
# Chi vince quando due sorgenti descrivono lo stesso acquisto. Numero basso
# significa priorita' alta.
RANK_BANK, RANK_SHARED, RANK_HISTORY = 0, 1, 2


def source_rank(path, shared):
    """Il rango di una sorgente, dedotto dal file."""
    if Path(path).name == "storico-moneywiz.csv":
        return RANK_HISTORY
    return RANK_SHARED if shared else RANK_BANK


def drop_covered_by(rows, days=3):
    """Scarta le righe gia' coperte da una sorgente di rango superiore.

    Due casi, un meccanismo solo:
    - Splitwise contro banca: se Fabio paga con la carta l'uscita e' gia'
      nell'estratto conto; se paga Michela non compare mai sul suo conto e la
      riga condivisa va tenuta.
    - Storico migrato contro banca: lo storico copre 2022-2026 e si
      sovrappone a qualunque estratto conto scaricato per quel periodo.

    Accoppiamento uno-a-uno, preferendo la data piu' vicina, come in
    drop_internal_transfers().
    """
    def as_date(value):
        try:
            return datetime.strptime(str(value), "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    higher = defaultdict(list)
    for position, row in enumerate(rows):
        higher[round(abs(row.get("Importo", 0)), 2)].append(position)

    dropped, used, ambiguous = set(), set(), 0
    counts = Counter()
    # Prima le sorgenti piu' basse: cosi' lo storico non "consuma" una riga
    # bancaria che serviva a coprire una riga condivisa.
    order = sorted(range(len(rows)),
                   key=lambda i: -rows[i].get("Rango", RANK_BANK))
    for position in order:
        row = rows[position]
        rank = row.get("Rango", RANK_BANK)
        if rank == RANK_BANK:
            continue
        day = as_date(row.get("Data"))
        candidates = []
        for other in higher.get(round(abs(row.get("Importo", 0)), 2), ()):
            if other == position or other in used or other in dropped:
                continue
            if rows[other].get("Rango", RANK_BANK) >= rank:
                continue
            when = as_date(rows[other].get("Data"))
            distance = abs((day - when).days) if day and when else 99
            if distance > days:
                continue
            candidates.append((distance, other))
        if not candidates:
            continue
        if len(candidates) > 1:
            ambiguous += 1
        candidates.sort()
        # Uno-a-uno: la riga che copre non puo' coprirne una seconda.
        used.add(candidates[0][1])
        dropped.add(position)
        counts[row.get("Conto", "?")] += 1

    if dropped:
        print(f"  {len(dropped)} righe scartate: gia' coperte da una sorgente "
              "di rango superiore")
        for conto, count in counts.most_common():
            print(f"    {count:>5} da {conto}")
        if ambiguous:
            print(f"    {ambiguous} avevano piu' di un candidato: se questo "
                  "numero cresce, serve una coda di revisione")
    return [row for position, row in enumerate(rows) if position not in dropped]
```

- [ ] **Passo 4: lancia il test e verifica che passi**

Esegui: `python bilancio.py --selfcheck`
Atteso: `selftest: ok`

- [ ] **Passo 5: assegna il rango in `load_transactions()`**

Nel dizionario che `load_transactions()` accoda, aggiungi come ultima chiave:

```python
            "Rango": source_rank(path, shared),
```

- [ ] **Passo 6: inserisci il passo in `run()`**

Fra `drop_cross_file_duplicates` e `drop_internal_transfers`:

```python
    rows = drop_cross_file_duplicates(rows)
    rows = drop_covered_by(rows)
    rows = drop_internal_transfers(rows)
```

- [ ] **Passo 7: verifica con un estratto conto finto sovrapposto**

```bash
python - <<'EOF'
import pandas as pd
sw = pd.read_csv('export/koala_2026-08-24_export.csv')
sw = sw[sw.Descrizione.notna()].head(3)
pd.DataFrame({
    "Data": pd.to_datetime(sw.Data).dt.strftime("%d/%m/%Y"),
    "Descrizione": "PAGAMENTO POS " + sw.Descrizione.str.upper(),
    "Importo": (-pd.to_numeric(sw.Costo)).map(lambda v: f"{v:.2f}".replace(".", ",")),
}).to_csv('export/intesa-prova.csv', sep=';', index=False, encoding='utf-8-sig')
EOF
python bilancio.py --no-llm --no-dashboard
```

Atteso: il log dichiara **3 righe scartate** perché coperte dalla banca, e il
totale non raddoppia. Poi rimuovi il file di prova:

```bash
rm export/intesa-prova.csv
```

- [ ] **Passo 8: verifica che il rango non finisca nell'output**

```bash
python -c "
import pandas as pd
print(list(pd.read_csv('consolidato.csv', sep=';', encoding='utf-8-sig').columns))"
```

Atteso: nessuna colonna `Rango` né `Origine file`.

- [ ] **Passo 9: commit**

```bash
git add bilancio.py
git commit -m "Precedenza fra sorgenti: la banca copre Splitwise e lo storico"
```

---

## Compito 6: aggiornare la documentazione e verificare tutto

**File:**
- Modifica: `HANDOFF.md`
- Modifica: `README.txt`

**Interfacce:** nessuna. Chiude il lavoro allineando la documentazione ai
comportamenti nuovi.

- [ ] **Passo 1: correggi l'avvertimento sul 2022-2023**

In `README.txt` e in `HANDOFF.md` c'è scritto che il 2022 e il 2023 non
valgono per i trend perché lo storico aveva solo 111 e 166 transazioni.
**Non è più vero**: Splitwise distribuisce 669 righe su tutto il periodo.
Sostituisci con:

```
- Il 2022 e il 2023 erano poveri finche' la sorgente era lo storico MoneyWiz.
  Con Splitwise (669 righe dal 2022-01 al 2026-08) sono coperti. Restano
  incompleti per le spese NON condivise di quegli anni, che nessuna sorgente
  ha mai registrato.
```

- [ ] **Passo 2: aggiungi la sezione al `HANDOFF.md`**

In cima, come nuova sezione `## IN CORSO`, spostando quella precedente sotto:

```markdown
## IN CORSO (24/08/2026) — Splitwise e precedenza fra sorgenti

**Lo storico conteneva gia' Splitwise, dimezzato.** MoneyWiz importava ogni
spesa condivisa come le due quote: "Eurospin -30,00" due volte per una spesa
da 60. 1027 righe su 1933. Migrate una volta sola con `--migra-storico`, che
ha tenuto le 906 superstiti (stipendi, affitti, spese non condivise).

**Splitwise si riconosce dalla firma numerica**, non dal nome del file (il suo
si chiama `koala_...`) ne' dai nomi delle persone: le colonne quota si
annullano riga per riga, su 669 righe su 669.

**Precedenza:** estratto conto > export condiviso > storico migrato. Un solo
passo, `drop_covered_by()`, copre sia Splitwise contro banca sia storico
contro banca.
```

- [ ] **Passo 3: aggiungi la trappola alle "Cose sapute che il codice non dice"**

```markdown
- **Un saldo Splitwise non si riconosce dall'importo.** Il primo criterio
  ("il Costo vale una quota intera") colpiva 32 righe, ma 31 erano spese vere
  pagate al 100% da uno e attribuite al 100% all'altro: `farmacia per Fabio`,
  `Netflix Michela`, `Colliri post operazione`. Le avrebbe cancellate in
  silenzio. Si riconosce da `Categorie = Pagamento` piu' la descrizione
  (`ha pagato`, `pareggia i bilanci`), perche' Splitwise non marca tutto.
```

- [ ] **Passo 4: verifica finale completa**

```bash
python bilancio.py --selfcheck
```
Atteso: `selftest: ok` e `verifica:` sopra il 55%.

```bash
python bilancio.py --no-llm
```
Atteso: circa 1.572 transazioni, consolidato e dashboard riscritti.

```bash
python -c "
import pandas as pd
d = pd.read_csv('consolidato.csv', sep=';', encoding='utf-8-sig')
reali = d[d.Natura != 'Non spesa']
per_natura = reali[reali.Importo < 0].groupby('Natura').Importo.sum().sum()
uscite = reali[reali.Importo < 0].Importo.sum()
print('quadratura:', abs(per_natura - uscite) < 0.01)
print('doppioni fra Splitwise e storico:', int(d.duplicated(['Data','Importo','Descrizione']).sum()))
"
```
Atteso: `quadratura: True`.

```bash
curl -s http://127.0.0.1:8770/api/state -o "$TEMP/state.json"
node test_app.js "$TEMP/state.json"
```
Atteso: `Tutti i controlli passati`. (Avvia prima `python server.py`.)

- [ ] **Passo 5: commit**

```bash
git add README.txt HANDOFF.md
git commit -m "Documentazione allineata a Splitwise e alla precedenza fra sorgenti"
```
