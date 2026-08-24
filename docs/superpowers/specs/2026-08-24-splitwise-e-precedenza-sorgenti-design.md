# Splitwise e precedenza fra sorgenti

Data: 2026-08-24

## Contesto

La pipeline legge gli estratti conto da `export/` e usa lo storico MoneyWiz
(1933 transazioni) come sorgente quando non ci sono export. L'utente usa anche
**Splitwise** per le spese condivise, e il suo export non veniva gestito.

Indagando è emerso un problema più grande dell'aggiunta di un parser.

### Cosa contiene l'export Splitwise

`export/koala_2026-08-24_export.csv`, 669 righe, 2022-01-12 → 2026-08-24:

```
Data,Descrizione,Categorie,Costo,Valuta,Mikela bogoni,Fabio Stocco
2022-01-12,Eurospin,Generali,60.00,EUR,30.00,-30.00
```

- `Costo` è **sempre positivo** (zero negativi su 669) ed è il costo pieno.
- Le colonne persona sono il **saldo** di ciascuno per quella riga, non la
  spesa. La loro somma fa **zero su tutte e 669 le righe**, senza eccezioni.
- `find_columns()` sceglie già correttamente `Data`, `Descrizione`, `Costo`.
- Il file si chiama `koala_...`: riconoscerlo dal nome, come faceva il vecchio
  `transaction_processor.py`, non avrebbe funzionato.

### Il problema vero: lo storico contiene già Splitwise, dimezzato

```
Splitwise   2022-01-12  Eurospin   Costo 60,00   quota Fabio 30,00
MoneyWiz    2022-01-12  Eurospin   -30,00
            2022-01-12  Eurospin   -30,00        <- due righe
```

Misurato: **560 righe Splitwise su 667 (84%) trovano corrispondenza nello
storico sulla quota**, contro il 12% sul costo pieno. Nello storico ci sono
**315 coppie `-X / -X`** e **145 coppie `+X / -X`** (11.290 € di valore
assoluto) con stessa data e descrizione.

MoneyWiz ha importato ogni spesa Splitwise come le due metà. La somma resta
corretta (−30 −30 = −60), quindi i totali mostrati finora non sono sbagliati,
ma ogni spesa condivisa esiste come due transazioni: falsa i conteggi, le
classifiche merchant e la coda di revisione. Spiega anche anomalie archiviate
come rumore: `Spesa hamburgers +11 / −11`, le quattro "entrate negative" in
`Affitti incassati`.

### Decisioni prese

1. Lo storico MoneyWiz **si migra una volta**, poi smette di essere sorgente di
   transazioni e resta il maestro delle categorie (1635 esempi etichettati).
2. Splitwise si riconosce **dalle colonne persona**, non dal nome del file.
3. Da Splitwise si usa **solo `Costo`** per l'importo. Colonne persona
   ignorate. `Categorie` non entra nella categorizzazione — la decide il motore
   dalla descrizione — ma serve a riconoscere i saldi (§3).
4. I saldi fra le due persone **non compaiono affatto**: bilanciano spese già
   tracciate, non sono transazioni.
5. Fra sorgenti che descrivono lo stesso acquisto vince l'estratto conto.

---

## Disegno

### 1. Migrazione dello storico

Comando una-tantum:

```
python bilancio.py --migra-storico
```

Scrive `export/elaborati/storico-moneywiz.csv` con le righe dello storico che
**non** derivano da Splitwise.

Criterio di riconoscimento, approvato dall'utente: per ogni riga Splitwise si
cercano righe storiche con **importo pari al valore assoluto di una quota**
(in una divisione fra due persone le due coincidono) e **data entro ±1
giorno**, accoppiando uno-a-uno e togliendone **al
massimo due** per riga Splitwise (le due metà).

Risultato misurato: **1027 righe riconosciute come Splitwise, 906 superstiti**
(797 uscite per −87.563 €, 109 entrate). I superstiti sono esattamente ciò che
Splitwise non ha mai avuto: `VOSTRI EMOLUMENTI`, `Affitti incassati`,
`Bonifici ricevuti`, spese non condivise.

Il comando stampa quante righe ha tolto, quante tenute e con quale valore.
L'operazione è reversibile: si cancella il file e si rilancia. Il backup
MoneyWiz resta intatto e non viene mai scritto.

Il `Conto` di ogni riga viene dal nome del conto in MoneyWiz
(`ZSYNCOBJECT.ZNAME` via `ZACCOUNT2`), come già fa
`load_history_transactions()`.

Dopo la migrazione:

- `load_history()` resta invariata: alimenta il motore di categorizzazione.
- `load_history_transactions()` non viene più chiamata da `read_sources()`.
- `--da-storico` e il parametro `from_history` spariscono.
- Con `export/` vuota la dashboard è vuota e lo dichiara, invece di ripiegare
  sullo storico.

### 2. Riconoscere un export di spese condivise

`is_shared_export(frame)`: vero se, oltre alle colonne standard riconosciute da
`find_columns()`, esistono altre colonne numeriche **la cui somma per riga è
zero** su almeno il 95% delle righe.

È la firma di una divisione fra persone: chi anticipa ha un credito, gli altri
un debito pari e contrario. Regge al cambio dei nomi, all'ingresso di una terza
persona e a qualunque nome di file.

Quando è vero, in `load_transactions()`:

- importo = **`-Costo`** (positivo nel file, ma sono uscite);
- colonne persona e `Categorie` ignorate.

**Rete di sicurezza generale**, indipendente da Splitwise: se un export produce
solo importi positivi, il log lo segnala. Un estratto conto ha entrambi i
segni; se non li ha, o è una lista di spese o il parser ha sbagliato colonna.

### 3. Scartare i saldi

Un saldo si riconosce da **due segnali, nessuno dei quali basta da solo**:

1. `Categorie == "Pagamento"` — il valore con cui Splitwise marca i saldi.
2. La descrizione combacia con `ha pagato` oppure `pareggia.*bilanci`.

Serve il secondo perché Splitwise non marca tutto: `2023-12-08 · Pareggia
tutti i bilanci · 615,59 €` ha `Categorie = Generali`. Serve il primo perché
la descrizione può cambiare formula.

È l'**unico** uso della colonna `Categorie`: per la categorizzazione resta
ignorata, come deciso.

**Criterio scartato, e perché.** La prima stesura riconosceva un saldo da
"`Costo` uguale al valore assoluto di una quota intera". Sbagliato: sono 32
righe, ma **31 sono spese vere** pagate interamente da uno e attribuite
interamente all'altro — `Colliri post operazione 68,00`, `farmacia per Fabio
22,90`, `Netflix Michela 120,00`. Quel criterio le avrebbe cancellate in
silenzio. Va tenuto fuori: uno split 100/0 è normale, non è un saldo.

Le righe di saldo **non entrano** nel consolidato. Il log dice quante e per
quanto. Oggi sono due, per 5.988,94 € complessivi.

Non vengono marcate `Giroconto`: un giroconto compare nell'elenco ed è escluso
dai totali, mentre un saldo non è proprio una transazione di questo bilancio.

Righe con `Descrizione` o `Costo` vuoti (la `Bilancio totale` in coda
all'export) sono già scartate dai controlli esistenti in
`load_transactions()`.

### 4. Precedenza fra sorgenti

```
estratto conto  >  export condiviso  >  storico migrato
```

Nuovo passo `drop_covered_by(rows)`, dopo `drop_cross_file_duplicates()` e
prima di `drop_internal_transfers()`.

Ogni riga cerca, fra le righe di sorgente **più alta**, un movimento con stesso
importo entro **±3 giorni**; se lo trova viene scartata. Accoppiamento
uno-a-uno, preferendo la data più vicina: stessa forma di
`drop_internal_transfers()`, che già regge.

Motivazione delle due direzioni:

- **Splitwise contro banca**: se Fabio paga Esselunga con la carta, l'uscita è
  già nell'estratto conto. Se paga Michela, sul conto di Fabio non compare mai
  e la riga Splitwise va tenuta — è la ragione per cui Splitwise serve.
- **Storico migrato contro banca**: lo storico copre 2022-2026 e si
  sovrapporrà a qualunque estratto conto scaricato per quel periodo. Senza
  questa regola il problema si ripresenterebbe identico.

Il log riporta quante righe sono state scartate **e quante avevano più di un
candidato**. È la misura che dirà se l'accoppiamento automatico basta o se
serve una coda di revisione manuale: finché le ambigue sono poche, basta.

Il rango della sorgente si deriva dal percorso del file: `storico-moneywiz.csv`
è storico migrato, un file riconosciuto da `is_shared_export()` è condiviso,
tutto il resto è estratto conto.

---

## Effetti attesi

Circa **1.572 transazioni** (906 migrate + 666 Splitwise, cioè 669 meno due
saldi e la riga `Bilancio totale`) contro le 1933 di oggi. Non è una perdita: le mancanti erano le seconde metà. Ogni spesa condivisa
diventa **una riga col costo pieno** invece di due mezze.

Il 2022-2023, finora dichiarato inutilizzabile per i trend perché lo storico
aveva solo 111 e 166 transazioni, torna coperto: Splitwise distribuisce 669
righe su tutto il periodo.

Va aggiornato l'avvertimento in `README.txt` e `HANDOFF.md` che dichiara quegli
anni inaffidabili.

---

## Verifica

- **Migrazione**: `storico-moneywiz.csv` ha 906 righe; nessuna di esse compare
  in Splitwise per data+quota; il totale delle uscite superstiti è −87.563 €.
- **Segno**: le 666 righe Splitwise entrano tutte negative; nessun importo
  positivo salvo quelli che vengono davvero dalla banca.
- **Saldi**: le due righe (5.373,35 € e 615,59 €) non sono nel consolidato e
  il log le nomina. Le 31 righe con split 100/0 — `farmacia per Fabio`,
  `Netflix Michela` — ci sono **tutte**: sono spese, non saldi.
- **Riconoscimento**: un file con colonne persona rinominate viene comunque
  riconosciuto; un estratto conto normale **non** viene scambiato per condiviso.
- **Precedenza**: un estratto conto finto con tre movimenti che duplicano tre
  righe Splitwise deve lasciare 666 righe da Splitwise, non 669, e il log deve dire
  quante ne ha scartate e quante erano ambigue.
- **Stabilità**: gli ID delle transazioni già presenti non cambiano dopo un
  caricamento nuovo, e un override messo prima regge.
- **Regressione**: `python bilancio.py --selfcheck` sopra il 55%,
  `node test_app.js` tutto verde.
- **Quadratura**: somma per natura uguale al totale di `consolidato.csv`.
