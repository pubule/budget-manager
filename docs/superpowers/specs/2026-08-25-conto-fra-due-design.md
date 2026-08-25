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
| I totali della dashboard | **la cassa**: quello che è uscito dal conto, 60 € non 30 |
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

### La conseguenza che cambia numeri esistenti

Con «i totali sono la cassa», una riga **pagata da Michela** non è cassa tua:
non è mai passata dal tuo conto. Oggi quelle righe — arrivano solo da
Splitwise, `drop_covered_by` le tiene apposta — **sono contate fra le tue
uscite**. Marcandole `pagato_da = lei` escono dai totali ed entrano nel
registro. **Escono anche dalle tabelle per categoria**: "Dove vanno i soldi"
mostra la tua cassa, e una spesa che non è passata dal tuo conto lì non ci
sta. Resta visibile in Transazioni e nel registro, dove ha senso.

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
3. **`apply_shares(rows, quote, condivise)`** dopo la categorizzazione:
   aggiunge le colonne `Pagato da` e `Quota`. La sorgente è `quote.csv`; dove
   manca, la quota arriva dalle **colonne persona di Splitwise**, lette in
   `load_transactions()` e portate avanti sulla riga.
4. **`consolidato.csv` guadagna due colonne**, `Pagato da` e `Quota`, e
   `read_consolidato()` le rilegge come tutte le altre.

Le righe `pagato_da = lei` escono dai totali marcandole con una natura che
esiste già (`Non spesa`), come i giroconti: nessun meccanismo nuovo.

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
- il registro parte dalla data di `partita.csv` e non prima;
- una riga senza quota non compare nel registro.

**Sui dati veri, col server acceso:**

- il totale delle uscite **prima e dopo** l'introduzione delle quote, con
  quante righe sono uscite dai totali perché pagate da Michela: è il numero
  che dice se il modello ha cambiato il bilancio come previsto;
- inserire una transazione a mano, ricaricare, e ritrovarla con la sua
  categoria e la sua quota;
- escluderla, ricaricare, e ritrovarla fra le escluse con il motivo;
- `python audit.py` senza voci nuove, più un controllo aggiunto: le quote che
  non trovano più la loro transazione.

---
