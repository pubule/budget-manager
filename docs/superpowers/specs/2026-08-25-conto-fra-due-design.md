# Il conto fra Fabio e Michela

Data: 2026-08-25

## Contesto

L'app sa cosa hai speso, non **per chi**. Paghi tu la cena, metà è di Michela,
e il bilancio la conta tutta tua; lei paga la spesa e quella riga — se arriva
da Splitwise — finisce comunque fra le tue uscite, pur non essendo mai passata
dal tuo conto.

Splitwise è già una sorgente dell'app (`koala_*.csv`, rango 1), ma la spec del
24 agosto decise di usarne **solo la colonna `Costo`** e di **buttare le
colonne persona**. Sono proprio quelle a contenere chi ha pagato e quanto deve
l'altro. Nel frattempo manca anche il gesto più semplice: **scrivere una
transazione a mano** — una spesa in contanti, una che Michela ha pagato e non
compare da nessuna parte.

Serve quindi: transazioni inserite a mano, una dimensione «chi ha pagato /
quanto deve l'altro» su **qualunque** riga, e un registro dei debiti e crediti
fra voi due.

### Deciso con te

| domanda | risposta |
|---|---|
| I totali della dashboard | **quanto è costato** (60 €), con un interruttore per leggere **quanto è tuo** (30 €) |
| Una spesa condivisa | **una riga sola** con le quote dentro, non due righe su due conti |
| A quali righe si applica la quota | **a tutte**, anche a quelle di banca |
| Splitwise | **convive**: resta sorgente, e le colonne persona smettono di essere buttate |
| Cancellare | **sempre "escludi"**: niente sparisce, tutto resta ripescabile |
| I rimborsi di Michela | **abbassano il debito e non sono entrate** |
| Da quando conta il saldo | **da una data che scegli tu**, con un saldo di partenza |
| La vista deve rispondere a | **da cosa nasce quel numero**: l'elenco delle righe, correggibile |

---

## Il modello

Una sola frase per riga: **chi ha pagato**, e **quanto ne deve l'altro**.

```
quota  = quanto deve chi NON ha pagato
segno  = +1 se hai pagato tu, -1 se ha pagato lei

effetto sul saldo = segno × quota × |importo|
saldo positivo = Michela deve a te
```

`quota` ha tre valori e basta:

| valore | significato |
|---|---|
| `meta` | l'altro deve la metà |
| `tutto` | l'altro deve l'intero importo (la farmacia per lei, Netflix per te) |
| `saldo` | **questa riga è il rimborso**: non è una spesa, muove il saldo dell'intero importo |

Assente = non condivisa. È il caso normale e non richiede nessun gesto.

### Due letture, non due conti

L'idea di partenza era **due conti**, uno tuo e uno di Michela, il suo
alimentato dalla sua quota Splitwise. La misura l'ha resa impraticabile per
com'era: spaccare ogni riga condivisa in due porterebbe le 657 righe Splitwise
a 1.314, e con loro i conteggi `tx` e la coda di revisione — esattamente il
raddoppio che la migrazione del 24 agosto aveva tolto dallo storico MoneyWiz.

Ma la domanda dietro l'idea è giusta, e si può rispondere senza raddoppiare
niente: **una riga sola, letta in due modi**.

| lettura | cosa somma | a cosa risponde |
|---|---|---|
| **tutto** (predefinita) | il costo pieno di ogni riga | quanto è costato vivere, per la coppia |
| **la mia quota** | ogni riga × la tua quota | quanto di quella spesa è tuo |

L'interruttore sta accanto ai filtri rapidi e vale per tutti i riquadri della
dashboard. Il conto "Michela" che avevi in mente diventa la differenza fra le
due letture, e il registro dei debiti la racconta riga per riga.

Nessun numero di oggi cambia nella lettura predefinita: è quella che l'app
mostra già.

### Le colonne persona bastano da sole

La spec del 24 agosto buttava le colonne persona dell'export Splitwise. Non
servivano allora; adesso contengono tutto quello che serve, per tutte e 669 le
righe, senza che tu marchi niente a mano.

Il valore nella colonna di una persona è il suo **saldo su quella riga**,
cioè `pagato − dovuto`, e la somma delle due fa zero (verificato su tutte e
669 nella spec precedente). Con due persone e un pagatore solo:

```
Costo 60,00    Michela +30,00    Fabio -30,00

chi ha pagato  = quello col saldo positivo          -> Michela
quota di chi non ha pagato = -saldo                 -> Fabio 30,00
quota del pagatore = Costo - quota dell'altro       -> Michela 30,00
```

Quindi `pagato_da` e `quota` si ricavano dal file. `quote.csv` serve per le
righe che Splitwise non conosce — quelle di banca e quelle a mano — e per
correggere quando la derivazione sbaglia.

La derivazione va **verificata sui dati veri al primo giro**, non data per
buona: se qualche riga non torna (tre persone in una spesa, un arrotondamento,
un saldo a zero da entrambe le parti) il numero va stampato, non nascosto.

Quante siano si misura al primo giro: il numero va stampato e confrontato
con i totali di prima, non stimato adesso.

---

## I file

Tre file nuovi, nello stile degli altri: CSV con `;`, leggibili a occhio,
sono loro il database.

**`quote.csv`** — la quota di una riga qualsiasi, agganciata all'ID come già fa
`override.csv`.

```
id;pagato_da;quota;nota
a3f9c1#1;io;meta;cena da Alcide
7b21e0#1;lei;tutto;farmacia per me
c40d88#1;lei;saldo;bonifico di pareggio
```

**`transazioni.csv`** — le righe scritte a mano.

```
id;data;descrizione;importo;conto;nota
man-20260825-01;2026-08-24;Cena Alcide;-84,00;Contanti;
```

L'`id` si genera alla creazione e **non si ricalcola mai**: gli altri ID
nascono dal contenuto, e modificando l'importo di una riga manuale l'ID
cambierebbe staccandola dalla sua quota e dalla sua categoria.

Perché regga, `assign_ids()` deve **rispettare un ID già presente** invece di
sovrascriverlo. Oggi lo assegna sempre: è una riga di codice, ma senza di
quella le righe manuali perdono la loro identità al primo giro.

**`partita.csv`** — una riga sola, il punto di partenza.

```
dal;saldo;controparte;nota
2026-01-01;0,00;Michela;pareggiato su Splitwise
```

**`escluse.csv`** — quello che non deve comparire, con il motivo.

```
id;motivo
9f2a11#1;doppione dell'export di luglio
```

Cancellare è sempre questo: una riga in più qui. Vale sia per le righe di
banca (che l'export riporterebbe comunque al giro dopo) sia per quelle
manuali, con un meccanismo solo.

---

## Dove si innesta, in `bilancio.py`

`run()` cambia in quattro punti, tutti piccoli:

1. **`read_manual(folder)`** accanto a `read_sources()`: legge
   `transazioni.csv` e ne fa righe di **rango `RANK_BANK`**, così non vengono
   mai scartate da `drop_covered_by` e possono coprire una riga condivisa.
2. **`apply_exclusions(rows, escluse, scartate)`** subito dopo
   `apply_corrections()`: sposta in `scartate` le righe elencate, col motivo.
   Riusa il registro di scarto che già stampa il rapporto per conto.
3. **`apply_shares(rows, quote)`** dopo la categorizzazione: aggiunge le
   colonne `Pagato da` e `Quota`. `quote.csv` vince; dove manca, valgono i
   valori derivati dalle colonne persona di Splitwise, lette in
   `load_transactions()` e portate avanti sulla riga.
4. **`consolidato.csv` guadagna due colonne**, `Pagato da` e `Quota`, e
   `read_consolidato()` le rilegge come tutte le altre.

Nessuna riga esce dai totali per via della quota: la lettura predefinita è il
costo pieno, ed è quella di oggi. La seconda lettura è un moltiplicatore
applicato **nel browser**, dove già vivono filtri e aggregazioni — non tocca
la pipeline e non riscrive niente.

### Una contraddizione da segnalare, non da risolvere in silenzio

Una riga arrivata dall'**estratto conto** dice già chi ha pagato: sei stato tu,
è il tuo conto. Marcarla `pagato_da = lei` è una contraddizione — la spesa
sarebbe sua ma i soldi sono usciti dai tuoi. Succede per un motivo vero (carta
tua, spesa sua) e per uno sbagliato (un clic di troppo), e l'app non può
distinguerli.

Quindi non la vieta e non la corregge: la **segnala** in `audit.py`, contata e
con qualche esempio. Se sono tre, è un errore di battitura; se sono trenta, è
un modo di usare l'app che il modello deve imparare a descrivere.

### Tre trappole già trovate leggendo il codice

- **`drop_covered_by` butta la riga condivisa e tiene quella di banca** — con
  dentro la quota che Splitwise conosceva. Il passo deve **travasare la quota
  sulla riga che sopravvive** prima di scartare l'altra, altrimenti la
  divisione si perde proprio sulle spese fatte con la carta, che sono la
  maggioranza.
- **Il ripiego su `consolidato.csv` scatta quando `read_sources()` non torna
  niente.** Con `transazioni.csv` fra le sorgenti non tornerebbe mai vuoto, e
  al riavvio senza export l'app mostrerebbe tre righe manuali al posto di 56
  mesi di storia. Il ripiego deve guardare **solo gli export**.
- **`transfer_out()` trasforma i giroconti in uscita spaiati in spese.** Un
  rimborso che *tu* mandi a Michela (`quota = saldo`, importo negativo) è
  esattamente quella forma e finirebbe in "Da identificare". La quota `saldo`
  deve vincere, come vince un override.

---

## L'interfaccia

**Nella scheda Transazioni**: due colonne nuove, `pagato da` e `quota`, con
menu che salvano subito — lo stesso schema di `data-fix` e del menu delle
categorie. Più un pulsante **"+ transazione"** in cima che apre il modale già
esistente (`modale()`) con data, descrizione, importo, conto.

**L'interruttore delle due letture**, accanto ai filtri rapidi del periodo:
`tutto` / `la mia quota`. Cambia solo il moltiplicatore con cui i riquadri
sommano le righe condivise, e sta in `F` come gli altri filtri.

**Una scheda nuova, "Con Michela"**, che risponde alla domanda che hai scelto —
*da cosa nasce quel numero*:

```
Michela ti deve 340 €          dal 01/01/2026, saldo di partenza 0 €

data        descrizione              importo   chi   quota   effetto   saldo
2026-01-12  Eurospin                  60,00    io    meta      +30,00   30,00
2026-01-20  Farmacia                  24,00    lei   tutto     -24,00    6,00
2026-02-03  Bonifico da Michela      500,00    lei   saldo   -500,00  -494,00
```

Ogni riga è correggibile sul posto: sbagli il "chi", lo cambi lì e il saldo si
rifà. In cima il totale, perché un elenco senza il suo totale costringe a
sommare a mano.

**Escludere** una riga: un pulsante nella scheda Transazioni, che chiede il
motivo con il modale e scrive in `escluse.csv`. Le righe escluse si rivedono
in fondo alla stessa scheda, con un "ripesca".

---

## Cosa NON si fa

- Niente terze persone: la partita è fra due, e `partita.csv` ne nomina una.
- Niente quote arbitrarie (30/70): metà o tutto, come hai chiesto.
- Niente sincronizzazione con Splitwise: si legge il suo export, non gli si
  scrive.
- Niente saldo ricostruito dal 2022: parte dalla data che scegli.

---

## Un ordine che regge da solo

Tre pezzi, ognuno utile anche senza i successivi — è il modo di spezzarlo se
il piano dovesse venire troppo lungo:

1. **Escludere e scrivere a mano** (`escluse.csv`, `transazioni.csv`,
   `assign_ids` che rispetta gli ID): da qui l'app sa già fare CRUD.
2. **Le quote** (`quote.csv`, colonne persona di Splitwise, il travaso in
   `drop_covered_by`, le due colonne nel consolidato).
3. **Il registro** (`partita.csv` e la scheda "Con Michela").

## Verifica

**Asserzioni in `selftest()` (`python bilancio.py --selfcheck`, sopra il 55%):**

- l'effetto sul saldo ha il segno giusto nei quattro casi (io/lei × meta/tutto);
- una riga `quota = saldo` non diventa mai una spesa, nemmeno passando da
  `transfer_out()`;
- una riga esclusa non compare nel consolidato e compare in `scartate.csv`;
- l'ID di una riga manuale non cambia modificandone l'importo;
- la quota sopravvive quando `drop_covered_by` scarta la riga condivisa.

**Asserzioni in `test_app.js`:**

- il saldo progressivo dell'ultima riga è uguale al totale in cima;
- con l'interruttore su `tutto` i totali sono **identici a quelli di oggi**:
  è la prova che la lettura predefinita non ha spostato niente;
- con l'interruttore su `la mia quota`, una riga a metà pesa la metà e una non
  condivisa pesa uguale;
- il registro parte dalla data di `partita.csv` e non prima;
- una riga senza quota non compare nel registro.

**Sui dati veri, col server acceso:**

- quante delle 669 righe Splitwise danno un pagatore e due quote coerenti con
  la derivazione, e quante no: è il numero che dice se le colonne persona
  bastano davvero da sole;
- la differenza fra le due letture sui 56 mesi, cioè quanto della spesa
  storica è quota di Michela;
- inserire una transazione a mano, ricaricare, e ritrovarla con la sua
  categoria e la sua quota;
- escluderla, ricaricare, e ritrovarla fra le escluse con il motivo;
- `python audit.py` senza voci nuove, più un controllo aggiunto: le quote che
  non trovano più la loro transazione.

---
