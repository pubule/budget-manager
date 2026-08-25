# Handoff — Bilancio familiare

Dove siamo, cosa è stato deciso e perché, e cosa il codice da solo non dice.
Va aggiornato a ogni sessione di lavoro, prima di chiudere.

---

## IN CORSO (25/08/2026) — categorie a due livelli

`Casa > Casalinghi` era una stringa sola. Ora sono due campi: **Categoria**
`Casa` e **Sottocategoria** `Casalinghi`, in due colonne nel consolidato e in
due colonne nei file di configurazione.

### Il vincolo che ha deciso il disegno

**La natura sta sulla coppia, non sull'area.** `Casa` da sola contiene quattro
nature: le bollette sono Ricorrenti, una ristrutturazione e' Straordinaria.
Appiattire i livelli avrebbe perso proprio l'informazione che serve a capire
dove si puo' comprimere. Quindi `natura.csv` continua a mappare la coppia
intera.

### Dentro al motore la stringa resta unita

Lo storico, il voto per token e la cache sono tutti indicizzati sul nome
intero. Dividerli li' dentro voleva dire riscriverli senza guadagnarci niente,
quindi la coppia viaggia unita nel motore e si divide **quando esce**: nei
file, nel consolidato, nell'interfaccia. Il ponte sono due funzioni in
`bilancio.py`:

```python
split_category("Casa > Casalinghi")   ->  ("Casa", "Casalinghi")
join_category("Casa", "Casalinghi")   ->  "Casa > Casalinghi"
```

Entrambe reggono il NaN di pandas, che arriva dalle colonne del frame: senza
quel controllo `str()` ne faceva la stringa `"nan"` e nasceva una
sottocategoria inesistente. C'e' un'asserzione in `selftest()` a guardia.

Il nome intero resta la **chiave con cui browser e server si parlano**: il
payload manda `nome`, `categoria` e `sottocategoria` insieme.

### Le quattro a un livello solo

`Stipendio`, `Affitti incassati`, `Giroconto` e `Da identificare` restano
senza sottocategoria: non sono spese vere e la natura le descrive gia'.

Erano state promosse a `Entrate > Stipendio`, `Non spesa > Giroconto` e simili.
Sbagliato: faceva comparire `Entrate`, `Non spesa` e `Da chiarire` come nomi di
**categoria** oltre che di **natura**, cioe' la stessa parola in due menu
diversi della barra dei filtri, dove sembra un errore. Il livello sotto non
aggiungeva niente, ripeteva.

C'e' un controllo in `test_app.js` che presidia entrambe le cose: nessuna
categoria si chiama come una natura, e nessuna riga di SPESA sta a un livello
solo (le quattro sopra sono l'elenco chiuso delle eccezioni).

### La trappola della migrazione

`categorie_merge.csv` ha due colonne di categoria e **non vanno trattate allo
stesso modo**. La colonna `categoria_attuale` e' il nome GREZZO come lo scrive
MoneyWiz: promuoverlo l'ha scollegato dallo storico, che quel nome lo scrive
ancora a un livello solo. Sintomo: 16 righe `Affitti incassati` rimaste senza
sottocategoria e con natura `Da classificare`.

Stessa cosa per `categorie_cache.json`, che tiene nomi di categoria e va
migrato anche lui: 2 voci erano rimaste indietro.

Regola generale: **le colonne che contengono nomi provenienti da fuori non si
migrano**, solo quelle che contengono nomi nostri.

### Dashboard

Due filtri a cascata (`categoria` e `sotto`, il secondo mostra solo le
sottocategorie dell'area scelta) **e** il riquadro "Voci per costo annuo" che
parte dalle aree e scende al clic, con "< tutte le categorie" per risalire. I
due modi restano allineati: scendere nel grafico riempie i filtri, e cambiare
area azzera la sottocategoria, che altrimenti apparterrebbe a un'altra area e
lascerebbe la tabella vuota.

In `dashboard.py` c'era una colonna `Voce` da introdurre: i riquadri di
livello 3 raggruppavano su `Categoria` quando quella conteneva il nome intero,
e dopo il cambio avrebbero mostrato le 11 aree duplicando il riquadro sopra.

**Totali invariati**: 1845 righe, entrate 120.095,24 €, uscite −148.688,88 €.
11 aree, 36 voci, zero righe con categoria ma senza sottocategoria.
`--selfcheck` 58%, `test_app.js` 64 controlli.

---

## IN CORSO (25/08/2026) — svuotata la coda di revisione

Da **157 righe da rivedere a 118**, e da **90 senza categoria a 21**.

**Tre pattern in `regole.csv` non agganciavano niente**, e nessuno se n'era
accorto perché una regola che non matcha non fa rumore:

| pattern | non prendeva | perché |
|---|---|---|
| `eni` | `ENI03816 SONA VR` | fra lettere e cifre non c'è confine di parola |
| `obi italia` | `Obi del 4/11/24` | in banca e su Splitwise il negozio è solo "Obi" |
| `visanto` (merchant) | `VINSANTO CAFE'` | il locale ha una N: era un errore di battitura |

**Errori grossi corretti.** Due `PRELIEVO SMART` da 1.000 € stavano in
`Shopping > Vestiti`: sono prelievi di contante, ora `Da identificare`. Un
concerto di Baglioni al Filarmonico stava fra i ristoranti. `Sifone bagno` e
`Top lavabo` pure. Vivino era un abbonamento invece che vino.

**Categoria nuova `Viaggi > Vacanze`** (Discrezionali): alberghi, camere e il
visto per la Tanzania non avevano dove stare e finivano su `Casa >
Manutenzione` e `Shopping > Vestiti`.

**Le 12 `Transazione senza nome` da +200 €** (agosto-novembre 2025, 2.500 €)
erano ricariche fra conti propri, confermato dall'utente. Ora sono giroconti:
**le entrate scendono da 122.595 a 120.095 €**. Le uscite non cambiano.

### Il difetto che resta: una regola esplicita perde contro un'ipotesi

La cascata è `override → giroconto → storico-esatto → storico-simile → regola
→ voto-token → cache → llm`. Quindi **`regole.csv` sta sotto al match sfumato
sullo storico**: una regola scritta apposta viene battuta da una somiglianza
allo 0,80 con una vecchia riga MoneyWiz.

Successo tre volte in una sola sessione:

```
ALBERGO CA' DEI MAGHI FUMANE VR  -> Shopping > Vestiti          [storico-simile]
Dalla Piazza srl - materiale elettrico -> Personale > Intrattenimento [storico-esatto]
TUTTO STOCK BUSSOLENGO           -> Cibo & Mangiare > Ristoranti [storico-esatto]
```

Ogni volta l'unico rimedio è stato un override per singola transazione, che
non insegna niente al motore. Lo storico MoneyWiz contiene etichette sbagliate
e oggi non c'è modo di correggerne una: `categorie_merge.csv` con `IGNORA`
lavora per categoria intera, non per descrizione.

**Proposta da valutare**: spostare `regola` sopra `storico-simile`. Se scrivo
una regola voglio che vinca su un'ipotesi. Il rischio è che una regola larga
copra centinaia di righe già etichettate bene, quindi va misurato prima con
`--selfcheck` e con un confronto categoria per categoria.

### L'LLM sulle righe opache non è servito

Interrogato sulle 21 rimaste (cache separata, niente è entrato nel bilancio):
**si è astenuto su 17**, e delle 4 risposte 3 erano sbagliate — `Ing. Savoia
Valentino` letto come un medico, quando `Ing.` è ingegnere. Su nomi di
paesi e persone il modello non ha appigli: quelle righe le può chiudere solo
chi c'era.

`--selfcheck` è salito da 57% a **58%**.

---

## IN CORSO (24/08/2026) — caricamento deterministico, LLM a parte

**Il problema.** I due pulsanti passavano entrambi `llm:true`, quindi ogni
caricamento di un estratto conto si portava dietro una passata del modello:
minuti di attesa per vedere dei dati. Ma **l'LLM tocca 52 righe su 1845
(2,8%)**: le altre escono da livelli deterministici. Le 1793 pagavano il costo
delle 52.

**La separazione è quasi gratis** perché nella cascata di
`Categorizer.categorize()` la cache sta **fuori** dal controllo su `use_llm`:

```python
if key in self.cache:              # <- fuori dal guard
    return self.cache[key], "cache", 0.5
if self.use_llm:
    category = self._ask_llm(description)
```

Un giro senza modello non perde nulla di già imparato — le 51 descrizioni in
`categorie_cache.json` restano risolte — semplicemente non fa domande nuove.

**Tre pulsanti.** `Carica N export` e `Ricarica` (era "Rielabora") girano con
`llm:false`; `Analizza con LLM (N)` è nuovo e gira con `llm:true`. Il numero è
il conteggio delle righe senza categoria, calcolato nel browser da
`uncategorized()`: nessun campo nuovo nel payload. A zero il pulsante resta
visibile ma spento — sparire e ricomparire confonderebbe.

Il server non è stato toccato: `payload.get("llm", True)` accettava già il
flag. Erano solo i due `post(..., {llm:true})` in `app.html`.

**Misurato:** un giro senza modello dura **1 secondo** e due giri di fila danno
un `consolidato.csv` **identico byte per byte**. Totali invariati (entrate
122.595,24 €, uscite −148.688,88 €), le 52 righe a confidenza 0,50 sopravvivono
tutte.

**Riscarichi.** `archive_exports()` affiancava il nuovo file al vecchio con un
suffisso a timestamp: l'archivio accumulava copie quasi identiche, e una
transazione stornata dalla banca restava nel bilancio per sempre. Ora stesso
nome = stesso conto e periodo, vince il più recente, il vecchio va in
`export/sostituiti/`.

Due trappole trovate mentre lo scrivevo:

1. Il controllo era `destination.exists()`, cioè solo `elaborati/AAAA-MM/` del
   mese corrente. Un export di agosto ricaricato a settembre **non** veniva
   riconosciuto come riscarico. Ora il gemello si cerca in
   `bilancio.archived_exports()`, che li restituisce tutti.
2. `export/sostituiti/` deve stare **fuori** da `export/elaborati/`:
   `archived_exports()` fa `rglob()` sugli elaborati, quindi una cartella lì
   dentro riporterebbe nel consolidato le righe appena sostituite — l'opposto
   dello scopo. C'è un'asserzione in `selftest()` a guardia di questa scelta,
   perché è un errore che non farebbe rumore.

Il gemello si tocca **solo dopo** che il nuovo è verificato in archivio: se
occupa già la casella buona, il nuovo si posa accanto con un nome provvisorio e
ci trasloca alla fine. Un'archiviazione fallita non lascia il bilancio senza
nessuna delle due copie.

---

## IN CORSO (24/08/2026) — Splitwise e precedenza fra sorgenti

**Lo storico conteneva gia' Splitwise, dimezzato.** MoneyWiz importava ogni
spesa condivisa come le due quote: "Eurospin -30,00" due volte per una spesa
da 60. 1027 righe su 1933. Migrate una volta sola con `--migra-storico`, che
ha tenuto le 906 superstiti (stipendi, affitti, spese non condivise).

**Splitwise si riconosce dalla firma numerica**, non dal nome del file (il suo
si chiama `koala_...`) ne' dai nomi delle persone: le colonne quota si
annullano riga per riga, su 669 righe su 669.

**Precedenza:** estratto conto > export condiviso > storico migrato.
`drop_covered_by()` copre sia Splitwise contro banca sia storico contro banca,
e accoppia **a parità di segno**: sul valore assoluto un accredito di +60,00
cancellava una spesa condivisa di -60,00.

**`drop_import_artifacts()`** toglie i 7 residui positivi (396,93) che
MoneyWiz si era lasciato dietro col costo pieno invece che con la quota.
Prima sparivano per effetto collaterale della cecità al segno di
`drop_covered_by()`; adesso hanno un passo che li toglie per la ragione
giusta.

**`scartate.csv`** elenca ogni riga tolta con il passo che l'ha presa. I
conteggi nel log dicono quante, quel file dice quali.

---

## IN CORSO (24/08/2026) — cartella export/, caricamento esplicito

**La falla piu' grave era in git.** `.gitignore` copriva i derivati ma non un
estratto conto vero: `git check-ignore conto_intesa.csv` diceva "non ignorato".
Il `git add -A` usato per ogni commit avrebbe messo IBAN e movimenti in
cronologia. Chiuso per primo, commit `920c2c2`.

**Il flusso adesso.** Depositi in `export/`, la dashboard mostra "Carica N
export", premi tu. Il sorvegliante avvisa e basta: guardando data e dimensione
poteva far partire l'elaborazione su un file ancora in copia.

**I file in attesa non entrano nei conti finche' non premi.** `all_exports()`
li include solo con `include_pending=True`, che passa solo
`load_and_archive()`. Senza questo il pulsante non caricherebbe niente: si
limiterebbe a spostare file gia' dentro.

**Il conto non e' piu' il nome del file.** `conti.csv` mappa il nome
dell'export sul conto vero. Serviva davvero: `drop_internal_transfers`
riconosce i giroconti guardando che i conti siano diversi, e con conti fasulli
sbagliava.

**Doppioni fra export diversi scartati**, conservati dentro lo stesso file.
Due caffe' uguali lo stesso giorno sono due spese; le stesse righe in due
scarichi sovrapposti no.

---

## Il log del modello, in diretta (23/08/2026)

**Pannello a destra.** Si apre col pulsante "Log" e da solo a ogni
elaborazione, qualunque pulsante l'abbia avviata. `LiveLog` in `server.py` sostituisce lo `StringIO`: raccoglie
l'output riga per riga mentre la pipeline gira, e `/api/log?from=N` lo serve a
pezzi — il browser tiene il segno e non riscarica quello che ha già.

**`/api/run` ora parte in un thread e torna subito.** Prima teneva la
richiesta appesa per tutta la durata: col modello sono minuti, e il browser
mollava proprio mentre il log serviva. La fine la scopre `poll()`, che
riabilita il pulsante quando `running` torna falso.

**`_ask_llm` racconta cosa fa**: descrizione, secondi impiegati, il contenuto
di `<think>` se c'è, la risposta esatta, e se è stata accettata o scartata.

**Quello che il log ha fatto vedere subito.** Il modello risponde `NESSUNA` a
gran parte del residuo, e ha ragione: le descrizioni sono `Transazione senza
nome`, `AMO`, `Berfis 25.02`. Sono ~3 secondi ciascuna per farsi dire "non lo
so". Un filtro che non chieda nulla al modello quando la descrizione non ha
token significativi taglierebbe minuti a ogni giro.

---

## Sotto git, e i periodi seguono il calendario (23/08/2026)

**C'è git.** `git init` fatto, primo commit `56b738b`. I derivati
(`consolidato.csv`, le dashboard, la cache LLM, il backup MoneyWiz) sono
gitignorati: si rigenerano e riempirebbero la cronologia di rumore. È un repo
**locale**, senza remoto: la sincronizzazione è quella di iCloud, non di un
server. Ogni pulizia futura è reversibile con `git checkout -- <file>`.

**Scritture atomiche.** `write_rows` e la cache LLM scrivono su un file `.tmp`
e poi rinominano. Prima aprivano il file vero in scrittura, che lo tronca
subito: un arresto a metà lasciava un CSV di configurazione mutilato.

**I filtri rapidi seguono il calendario, non i dati.** Erano ancorati
all'ultima transazione: "Mese in corso" mostrava gennaio essendo agosto. Era
una bugia, e l'etichetta "riferimento" che avevo aggiunto era una pezza su una
scelta sbagliata. Ora "Mese in corso" dà agosto anche se è vuoto — e quando è
vuoto lo dice indicando dove sono gli ultimi dati. Il caso ancorato ai dati ha
un pulsante suo, **"Ultimo mese con dati"**, con un nome che dice quello che fa.

---

## La dashboard è diventata un'applicazione

`python server.py` apre l'interfaccia nel browser. Da lì si corregge tutto:
categorie, tassonomia, regole, merchant, importi. La cartella è sorvegliata:
appena copi dentro un export, rielabora da sola entro tre secondi.

**Lo stato dei dati.** Lo scambio automatico di sorgente non esiste più: lo
storico MoneyWiz è stato **migrato una volta sola** in
`export/elaborati/storico-moneywiz.csv` e da lì in poi è una sorgente come le
altre, la meno prioritaria (`RANK_HISTORY`). Quello che resta del backup
alimenta soltanto il motore di categorizzazione.

Oggi in `export/` ci sono due sorgenti: l'export Splitwise (`RANK_SHARED`) e
lo storico migrato (`RANK_HISTORY`). **Estratti conto bancari veri
(`RANK_BANK`) non ce ne sono ancora**: i test usano file finti nei tre formati
(CSV `;`, CSV dare/avere, Excel). `has_exports()` non distingue più le
sorgenti — con lo storico migrato lì dentro è di fatto sempre vera.

**Quello che i numeri dicono adesso** (2024-2025, i due soli anni densi,
ricalcolati sul consolidato post-migrazione — 24 mesi, giroconti esclusi):

```
uscite reali      -104.587      -4.358/mese
spesa ordinaria                 -3.569/mese   (senza le straordinarie)

  Discrezionali   -1.150/mese   26%   <- la leva più grande, e la più immediata
  Vincolate       -1.106/mese   25%
  Straordinarie     -789/mese   18%
  Ricorrenti        -727/mese   17%
  Quotidiane        -570/mese   13%
```

---

## Come si riprende

```bash
python server.py                   # l'applicazione, http://127.0.0.1:8770
python bilancio.py --selfcheck     # motore di categorizzazione + selftest
python bilancio.py --no-llm        # elabora da riga di comando, senza LLM
python bilancio.py --migra-storico # riscrive export/elaborati/storico-moneywiz.csv
                                   # dal backup MoneyWiz. Idempotente: stesso
                                   # percorso, sovrascrive, non duplica

# l'interfaccia, senza aprire un browser (64 controlli)
curl -s http://127.0.0.1:8770/api/state -o "$TEMP/state.json"
node test_app.js "$TEMP/state.json"
```

**Lo `state.json` va in una cartella temporanea, non qui.** Sono 534 KB di
movimenti bancari: lasciarlo nella cartella lo fa sincronizzare su iCloud senza
motivo.

Soglie da non far scendere: `--selfcheck` sopra il **55%** (ora 57%, baseline
25%), `test_app.js` tutto verde.

---

## Com'è fatto

```
server.py       server locale + API JSON + sorveglianza della cartella
app.html        interfaccia: JS e CSS inline, nessuna libreria, nessuna CDN
bilancio.py     la pipeline. run() è chiamabile, main() è solo argparse
dashboard.py    export statico HTML + Excel
test_app.js     controlli sull'interfaccia con un DOM finto
```

**L'invariante da non violare:** `consolidato.csv` è **derivato**. Viene
riscritto da zero a ogni elaborazione. L'interfaccia non ci scrive mai: ogni
modifica finisce in uno dei CSV di configurazione, che sono il vero database.

**L'ordine della pipeline conta:**

```
leggi export → assegna ID → applica correzioni → doppioni fra file →
giroconti → artefatti di importazione → coperture fra sorgenti → categorizza
```

**I giroconti vanno tolti PRIMA delle coperture.** Da quando
`drop_internal_transfers()` tocca solo il rango 0, l'ordine inverso faceva
sparire spese vere: una spesa condivisa "coperta" da una gamba di giroconto
che il passo successivo annullava. Tre righe dentro, zero fuori. C'è
l'asserzione in `selftest()`.

Gli ID si calcolano sui valori **originali**. Al contrario, correggere un
importo cambierebbe l'ID di quella riga e la correzione si staccherebbe dalla
sua transazione alla prima rielaborazione.

**La cascata di categorizzazione**, sette livelli, vince il primo che risponde:
override → giroconto → storico-esatto → storico-simile → regola → voto-token →
LLM.

---

## Le decisioni prese, e perché

### 1. I giroconti vanno riconosciuti PRIMA dello storico

Tutte le altre regole stanno dopo lo storico, queste no. Motivo: in MoneyWiz
quattro ricariche HYPE erano etichettate a mano come `Affitto`, cioè come
**entrate**. Se lo storico vincesse, il totale delle uscite mentirebbe e non
ci sarebbe modo di accorgersene.

### 2. Il 2022 e il 2023 NON sono coperti: non hanno entrate

Splitwise non li ha sistemati, e su questo mi ero sbagliato. Misurato sul
consolidato di adesso (giroconti esclusi):

```
anno   righe   uscite      entrate
2022     101    -6.708            0
2023     131   -12.385      19.769
2024     462   -48.607      49.796
2025     672   -55.980      53.236
```

Il 2022 ha oggi **meno righe di prima** (101 contro 111) e **zero entrate**:
contiene solo spese condivise. Di quegli anni non esiste né un estratto conto
né uno stipendio registrato.

**La conseguenza decisiva:** un anno senza entrate ma con uscite produce un
risparmio pari all'intera spesa, col segno meno. Ogni grafico pluriennale di
risparmio disegna il 2022 e il 2023 come una catastrofe **mai avvenuta**. I
confronti fra anni vanno fatti dal 2024 in poi, e prima o poi la dashboard
dovrà rifiutarsi di disegnare un anno senza entrate.

### 3. Le straordinarie stanno fuori dalla media

Un divano da 2.000 € fa sembrare disastroso un mese normale. La dashboard
mostra **due** numeri: spesa totale e "spesa ordinaria" che le esclude. Il
secondo è quello che dice quanto costa vivere.

### 4. Il confronto anno su anno usa gli stessi mesi

Confrontare il 2025 intero contro un 2026 di un mese produceva
`Casa +20.900 € (97%)`, che si legge come un crollo delle spese. Ora confronta
gennaio con gennaio e lo dichiara nell'intestazione della tabella.

### 5. Gli indicatori cambiano quando filtri

Filtrando su una natura, "tasso di risparmio" mostrava `-760%`. Il risparmio
si calcola sul bilancio intero: su una fetta di sole uscite è un numero che
sembra una conclusione e non vuol dire niente. Sotto filtro compaiono invece
quota, totale, transazioni e mesi coperti.

### 6. I filtri rapidi seguono il calendario (corretto il 23/08/2026)

Prima erano ancorati all'ultima transazione, perché così i pulsanti erano
sempre utili. Sbagliato: un pulsante che dice "Mese in corso" e mostra gennaio
essendo agosto mente, e un periodo vuoto è **informazione vera** — vuol dire
che i dati non sono aggiornati. Ora seguono il calendario, e "Ultimo mese con
dati" è un pulsante separato con un nome onesto.

### 7. Il periodo chiesto e quello coperto sono due cose diverse

"Anno in corso" copre `2026-01-01 – 2026-12-31` ma i dati arrivano a gennaio.
L'intestazione ora dice entrambe le cose e avverte che le medie mensili usano
1 mese, non 12. Prima diceva solo `da 2026-01 a 2026-01` e sembrava rotto.

### 8. Il server non usa l'LLM all'avvio

Ogni chiamata a `qwen3:8b` costa ~3 secondi (14 la prima, a freddo). Con 130
descrizioni nuove il primo avvio bloccava tutto per sette minuti. L'LLM entra
**solo** col pulsante "Analizza con LLM": non all'avvio, non al caricamento,
non quando la cartella cambia. Caricare è deterministico e dura un secondo;
il modello è un'operazione a parte che si chiede quando serve.

### 9. `IGNORA` nella colonna 3 di `categorie_merge.csv`

`Casa > Altro` e `Trasporto > Altro` erano cassetti dei rifiuti: 171 righe di
roba scorrelata. Imparare da loro insegnava al motore a buttarci dentro altra
roba. Marcandole `IGNORA` le righe restano ma le ricategorizzano le regole.

---

## Cose sapute che il codice non dice

- **I 7.929 € di "bonifici senza causale" non erano senza causale.** Erano
  troncati a 70 caratteri nella mia visualizzazione. Sono tutti giroconti:
  `A: HYPE`, `A: Fideuram Fabio`, `A: MICHELA BOGONI`. Nessuno è una spesa.
  Contarli gonfiava il totale di 330 €/mese. Lezione: guardare la stringa
  intera prima di concludere che manca un dato.

- **`Affitto` è un'ENTRATA** (+9.118 €), va con `Affitti incassati`, non con
  `Casa > Ipoteca/Affitto`. Il nome inganna, il segno no.

- **`Iper Rossetto` compariva con 56 grafie diverse** per 7.828 €. Senza
  `merchant.csv` la voce di spesa comprimibile più grande è invisibile in ogni
  classifica. Le decisioni di risparmio si prendono sul merchant, non sulla
  categoria.

- **Amazon è un canale, non una categoria.** 58 transazioni sparse su quattro
  categorie diverse. Ora c'è `Shopping > Online` per quando non si sa cosa sia
  stato comprato, ma resta un compromesso.

- **La regola `\bbar\b` catturava un bonifico da 2.500 €** con causale
  "Open bar". Le regole corte su parole comuni sono mine: vanno messe in fondo,
  dopo tutte le specifiche.

- **`toLocaleString("it-IT")` non raggruppa le migliaia in Node.** In Chrome sì.
  Il formato ora è calcolato a mano, così non dipende dall'ICU. L'ha trovato
  `test_app.js`, non il browser.

- **`redirect_stdout` sostituisce `sys.stdout` globalmente.** Serve a catturare
  il log della pipeline, ma se provi il server in-process ti mangia anche i
  `print` del test. Provalo come processo vero.

- **Ollama risponde 404 quando manca il modello**, non un errore di rete: il
  messaggio distingue i due casi perché la cura è diversa (`ollama pull` contro
  `ollama serve`).

- **Il `-wal` del backup MoneyWiz va estratto insieme al `.sqlite`.** Contiene
  le scritture non ancora consolidate: senza, si perdono le transazioni più
  recenti.

- **Le righe di commento in `regole.csv` sopravvivono** alla riscrittura fatta
  dall'interfaccia (diventano righe con categoria vuota, che `load_rules`
  salta), ma sono filtrate dall'elenco mostrato: altrimenti comparivano come
  regole senza categoria e "salva" dava errore.

- **Dopo un ridisegno i nodi del DOM sono altri.** Tenere un riferimento ai
  pulsanti e cliccarli dopo che `render()` è passato non fa niente: l'evento
  parte da un nodo staccato e non arriva al listener sul documento. Nei test
  via JS bisogna riprendere gli elementi ogni volta.

- **Un processo server vecchio rimasto vivo confonde le prove.** Un
  caricamento sembrava archiviare i file lasciandoli al loro posto: il log
  diceva "archiviati", il disco no. `archive_exports` chiamata a mano
  funzionava. Era una versione precedente del server ancora in ascolto sulla
  stessa porta. Prima di dubitare del codice, contare i processi python.

- **`load_transactions` non solleva errori.** Su un file illeggibile stampa
  "saltato" e torna vuoto, quindi il giro risulta riuscito. Senza un controllo
  esplicito l'export veniva archiviato lo stesso e spariva dalla vista senza
  essere mai entrato nei conti. Ora si archivia solo cio' che ha prodotto
  righe.

- **Il primo clic dopo `navigate` con l'automazione del browser va a vuoto.**
  Non è un difetto dell'app: verificato con `document.elementFromPoint` e un
  `.click()` da codice, che funziona. Nei test bisogna cliccare due volte o
  usare JS.

- **`dashboard.xlsx` non era nella lista `skip`**: con export veri nella
  cartella, la pipeline avrebbe letto la propria dashboard come estratto conto.
  Ora c'è `GENERATED` che raccoglie tutti i file prodotti.

- **Un saldo Splitwise non si riconosce dall'importo.** Il primo criterio
  ("il Costo vale una quota intera") colpiva 32 righe, ma 31 erano spese vere
  pagate al 100% da uno e attribuite al 100% all'altro: `farmacia per Fabio`,
  `Netflix Michela`, `Colliri post operazione`. Le avrebbe cancellate in
  silenzio. Si riconosce da `Categorie = Pagamento` piu' la descrizione
  (`ha pagato`, `pareggia i bilanci`), perche' Splitwise non marca tutto.

- **`Path.replace()` non sposta i file dentro iCloud Drive su Windows.**
  L'archiviazione degli export in `server.py` dice "archiviati" e i file
  restano dove sono; un `mv` di shell funziona. Riguarda `archive_exports()`,
  gia' presente su master, fuori dallo scopo di questo lavoro. Da sistemare.

- **`drop_covered_by()` puo' lasciare un doppione residuo.** Quando la stessa
  spesa esiste in tutte e tre le sorgenti, l'accoppiamento uno-a-uno consuma
  la copertura bancaria una volta sola e una riga di troppo sopravvive. Il log
  ora lo conta: nei dati veri e' 1 caso su 1572. Se quel numero cresce, serve
  una coda di revisione manuale.

---

## Quello che resta

Nell'ordine in cui conviene affrontarli.

1. **Caricare il primo estratto conto vero.** E' il buco piu' grande e sblocca
   tutto il resto: finora i formati sono stati provati solo su file costruiti a
   mano, e il rango 0 non e' mai esistito nei dati. Quando lo carichi:
   - riempi `conti.csv` coi nomi che usano davvero le tue banche;
   - **leggi `scartate.csv` riga per riga**: e' il primo giro in cui la
     precedenza fra sorgenti lavora sul serio, e li' vedi cosa ha tolto;
   - guarda quante righe finiscono in "copertura gia' consumata": oggi e' 1 su
     1572, se cresce serve una coda di revisione manuale.

2. **Il risparmio e' negativo (-14.976 EUR) e non e' la realta'.** Gli stipendi
   stanno negli estratti conto, Splitwise porta solo spese. Si risolve da solo
   al punto 1: non inseguire il numero prima.

3. **`Utenze domiciliate` e `Mandato` finiscono in `Casa > Ipoteca/Affitto`**
   (10.655 EUR/anno insieme). Vengono da `storico-esatto`, cioe' da come erano
   etichettate in MoneyWiz. Se sono bollette, servono due righe in
   `override.csv`.

4. **Il passo LLM non e' mai girato su tutto il residuo.** Sui dati di oggi
   aveva reso zero (41 chiamate, 41 astensioni) perche' il residuo era testo
   non classificabile. Con gli estratti conto veri il quadro cambia: le
   descrizioni diventano stringhe merchant, su cui il modello aveva risposto
   4 su 4.

5. **`override.csv` ha 5 righe con categoria vuota**: promemoria dei bonifici,
   ora risolti dalle regole giroconto. Si possono togliere.

6. **`transaction_processor.py`** e' la versione precedente, superata da
   `bilancio.py`. Ora che c'e' git si puo' cancellare senza perdere niente.

7. **Il repo git non ha un remoto.** La copia di sicurezza e' iCloud. Prima di
   aggiungerne uno: `merchant.csv`, `regole.csv`, `override.csv` e questo file
   contengono nomi propri, importi e la ripartizione delle spese di casa.
