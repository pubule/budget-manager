# Handoff — Bilancio familiare

Dove siamo, cosa è stato deciso e perché, e cosa il codice da solo non dice.
Va aggiornato a ogni sessione di lavoro, prima di chiudere.

---

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

**Pannello a destra.** Si apre col pulsante "Log" e da solo premendo
"Rielabora". `LiveLog` in `server.py` sostituisce lo `StringIO`: raccoglie
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

**Lo stato dei dati.** Non ci sono ancora export bancari veri nella cartella.
Finché non ci sono, tutto lavora sullo storico MoneyWiz (1933 transazioni,
2022-01 → 2026-01), così l'interfaccia ha qualcosa da mostrare fin da subito.
Appena arriva un export, `has_exports()` diventa vera e la sorgente cambia da
sola. **Non è ancora mai stato provato su un export bancario reale**: i test
usano file finti nei tre formati (CSV `;`, CSV dare/avere, Excel).

**Quello che i numeri dicono adesso** (2024-2025, i due soli anni densi):

```
uscite reali      -101.176      -4.216/mese
spesa ordinaria                 -3.401/mese   (senza le straordinarie)

  Discrezionali   -1.103/mese   26%   <- la leva più grande, e la più immediata
  Vincolate       -1.070/mese   25%
  Straordinarie     -814/mese   19%
  Ricorrenti        -632/mese   15%
  Quotidiane        -549/mese   13%
```

---

## Come si riprende

```bash
python server.py                   # l'applicazione, http://127.0.0.1:8770
python bilancio.py --selfcheck     # motore di categorizzazione + selftest
python bilancio.py --no-llm        # elabora da riga di comando, senza LLM

# l'interfaccia, senza aprire un browser (51 controlli)
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
leggi export → assegna ID → applica correzioni → togli giroconti → categorizza
```

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

### 2. Il 2022 e il 2023 sono coperti, ma solo per le spese condivise

- Il 2022 e il 2023 erano poveri finche' la sorgente era lo storico MoneyWiz.
  Con Splitwise (669 righe dal 2022-01 al 2026-08) sono coperti. Restano
  incompleti per le spese NON condivise di quegli anni, che nessuna sorgente
  ha mai registrato.

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
solo col pulsante "Rielabora" e quando la cartella cambia.

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

- **Provare su export bancari veri.** È il buco più grande: i formati sono
  stati provati solo su file costruiti a mano. Serve anche per riempire
  `conti.csv` coi nomi che usano davvero le tue banche.
- **`Utenze domiciliate` e `Mandato` finiscono in `Casa > Ipoteca/Affitto`**
  (10.655 €/anno insieme). Vengono da `storico-esatto`, cioè da come erano
  etichettate in MoneyWiz. Se sono bollette, servono due righe in
  `override.csv`.
- **`override.csv` ha 5 righe con categoria vuota**: sono i promemoria dei
  bonifici, ora risolti dalle regole giroconto. Si possono togliere.
- **Il passo LLM non è mai girato su tutto il residuo** (~130 descrizioni,
  ~7 minuti). Le 4 risposte provate erano tutte categorie valide, ma Decathlon
  è finito in `Shopping > Tecnologia` invece di `Vestiti`.
- **`transaction_processor.py`** è la versione precedente, completamente
  superata da `bilancio.py`. Tenuta solo perché non c'è git: se non serve più
  a niente, si cancella.
- **Il repo git non ha un remoto.** La copia di sicurezza è iCloud. Se serve
  un backup vero, va aggiunto un remoto privato.
