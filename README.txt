BILANCIO
========

    python server.py

Apre la dashboard nel browser. Da li' si fa tutto: correggere le categorie,
gestire la tassonomia, scrivere le regole, filtrare i grafici.

DOVE VANNO GLI EXPORT
---------------------

    export/                  ci depositi gli estratti conto
      elaborati/2026-08/     dove finiscono dopo il caricamento
      anonimi/               copie senza IBAN
      sostituiti/            rimpiazzati da uno scarico piu' recente

Depositi i file in export/. La dashboard se ne accorge entro pochi secondi e
compare il pulsante "Carica N export". Premi quando hai finito di copiare: la
pipeline gira e il log a destra mostra cosa fa.

Finche' non premi, quei file NON entrano nei conti. E' voluto: il sorveglia-
mento guarda data e dimensione, e un file ancora in copia le cambia entrambe,
quindi elaborare da solo poteva leggere meta' di un .xlsx.

A giro riuscito gli originali si spostano in export/elaborati/AAAA-MM/ e
accanto, in export/anonimi/, compare la copia ripulita. Un file da cui non si
legge nessuna transazione NON viene archiviato: resta in export/ con l'errore
nel log.

GIROCONTI. Due righe di importo opposto su conti diversi entro pochi giorni
si annullano SOLO SE almeno una delle due dice di essere un giroconto, cioe'
combacia con una regola di regole.csv che ha categoria Giroconto.

Senza quella condizione bastavano importo e data, e le collisioni casuali
arrivavano subito: quattro pagamenti F24 dello stesso giorno (-12, -114,
-116, -452) sparivano annullati da entrate qualsiasi di pari importo su un
altro conto. E qui non si scarta una riga sola: se ne perdono DUE, una per
parte, e i soldi svaniscono da entrambi i conti.

Le coppie che combaciano per importo e data ma che nessuna regola riconosce
NON vengono annullate, e il log le elenca. Se fra quelle c'e' un giroconto
vero, gli manca una regola: aggiungila in regole.csv con categoria Giroconto,
nominando il conto o la persona.

QUALE COLONNA. Un estratto conto puo' avere PIU' colonne buone per lo stesso
ruolo, e la scelta va per preferenza, non per ordine nel file:

    data          data contabile/registrazione/operazione, poi "data"
    descrizione   descrizione, poi operazione, beneficiario, memo, causale
    importo       importo, poi amount, costo

UniCredit ne ha due per la descrizione: "Causale" e "Descrizione". La causale
e' l'etichetta generica del movimento ("PAGAMENTO POS"), la descrizione dice
cosa hai comprato davvero. Prendendo la prima che capitava NEL FILE vinceva la
causale, e il consolidato si riempiva di righe tutte uguali.

Una colonna non puo' fare due mestieri: in un file con "Data operazione" e
nessuna descrizione, quella colonna non diventa la descrizione solo perche'
contiene la parola "operazione".

Se aggiungi una banca con nomi di colonna diversi, si aggiunge una riga a
CANDIDATE in bilancio.py. Le asserzioni in selftest() coprono i formati gia'
in uso, cosi' una preferenza nuova non puo' cambiarli di nascosto.

PREAMBOLI. Diverse banche mettono numero di conto, periodo e filtri PRIMA
della tabella vera. La pipeline cerca da sola dove comincia l'intestazione,
entro le prime trenta righe, e lo dice nel log:

    lista_global_20260825.xlsx: intestazione alla riga 19, sopra c'e' un
    preambolo

Senza questo il file sembra vuoto: le colonne escono tutte "Unnamed" e non si
legge nessuna transazione, senza che niente lo segnali come errore.

RISCARICHI. Se depositi un file con lo STESSO NOME di uno gia' archiviato, e'
un riscarico dello stesso conto e dello stesso periodo: vince il piu' recente.
Il vecchio non si cancella, va in export/sostituiti/ e il log dice quale ha
sostituito quale. Senza questa regola l'archivio accumulerebbe copie quasi
identiche, e una transazione che la banca ha stornato resterebbe nel bilancio
per sempre.

Il gemello si cerca in TUTTI i mesi, non solo in quello corrente: un export di
agosto ricaricato a settembre finirebbe in elaborati/AAAA-09/ e i due non si
incontrerebbero mai.

export/sostituiti/ sta di proposito FUORI da export/elaborati/: gli elaborati
si leggono ricorsivamente, quindi una cartella li' dentro riporterebbe nel
consolidato le righe appena sostituite.

Gli archiviati continuano a essere letti a ogni elaborazione: spostarli e'
organizzare, non escludere. Togliere un file da elaborati/ significa togliere
quelle transazioni dal consolidato.

ATTENZIONE a export/anonimi/: "anonimo" vuol dire senza IBAN, numeri di carta
e codici tecnici. Importi, date, negozi e saldi restano tutti. Non e' un file
da mandare in giro alla leggera.

Tutta la cartella export/ e' esclusa da git, anonimi/ compresa.

Il server ascolta SOLO su 127.0.0.1. Sono dati bancari e non devono essere
raggiungibili dalle altre macchine di casa. I dati non escono mai dal PC:
MoneyWiz e' letto in locale, Ollama gira in locale, la pagina non carica
niente dalla rete.

Serve ancora la riga di comando? C'e':

    python bilancio.py                 elabora e rigenera dashboard.html/.xlsx
    python bilancio.py --no-llm        salta Ollama
    python bilancio.py --selfcheck     verifica che il motore non sia rotto
    python bilancio.py --migra-storico riscrive export/elaborati/storico-
                                       moneywiz.csv dal backup MoneyWiz


CATEGORIE A DUE LIVELLI
-----------------------

Ogni voce di spesa ha una CATEGORIA e una SOTTOCATEGORIA: l'area larga e il
dettaglio. "Casa" e' l'area, "Arredamento" il dettaglio.

    Casa                  Ipoteca/Affitto, Condominio, Luce e gas, Acqua,
                          Telefono e internet, Assicurazione, Sicurezza,
                          Arredamento, Fai da te, Giardino, Manutenzione,
                          Ristrutturazione
    Cibo & Mangiare       Alimentari, Ristoranti, Pranzo Lavoro,
                          Cantina e Specialita
    Auto                  Bollo, Assicurazione, Manutenzione
    Trasporto             Carburante, Pedaggi, Parcheggio, Mezzi pubblici
    Shopping              Vestiti, Tecnologia, Regali, Amazon
    Personale             Abbonamenti, Benessere, Intrattenimento
    Assistenza Sanitaria  Farmacia, Medico
    Viaggi                Vacanze
    Comunita'             Tasse

DENTRO CASA le voci si dividono per CHI FA IL LAVORO: "Arredamento" e' cio'
che compri e metti dentro, "Fai da te" il materiale che monti tu, e
"Manutenzione" e' quando chiami qualcuno. "Giardino" e "Sicurezza" sono voci
loro perche' hanno un ritmo di spesa tutto suo.

LE UTENZE sono divise per tipo (luce e gas, acqua, telefono e internet)
perche' sono tre contratti che si cambiano in tre modi diversi: tenerle
insieme direbbe solo "diciottomila euro di bollette" senza dire su quale
conviene agire.

"AUTO" tiene i costi del possederla (bollo, assicurazione, officina),
"Trasporto" quelli del muoversi (carburante, pedaggi, parcheggi, mezzi
pubblici). Sono due decisioni diverse: la prima si affronta cambiando auto,
la seconda cambiando abitudini.

"SHOPPING > AMAZON" e' una resa dichiarata: la banca scrive solo "AMZN Mktp
IT" e cosa sia stato comprato non e' scritto da nessuna parte. Tenerlo a
parte evita di sporcare le altre voci con novanta ipotesi.

Nei file di configurazione sono DUE COLONNE:

    natura.csv     categoria;sottocategoria;natura
    regole.csv     pattern;categoria;sottocategoria
    override.csv   id;data;importo;categoria;sottocategoria;nota

LA NATURA STA SULLA COPPIA, non sull'area. Dentro "Casa" le bollette sono
Ricorrenti e una ristrutturazione e' Straordinaria: appiattire i due livelli
perderebbe proprio l'informazione che serve a capire dove si puo' risparmiare.

Quattro voci restano a UN LIVELLO SOLO perche' non sono spese vere, e la
natura le descrive gia':

    Stipendio          natura Entrate
    Affitti incassati  natura Entrate
    Giroconto          natura Non spesa
    Da identificare    natura Da chiarire

Nessun nome di categoria coincide con un nome di natura: la stessa parola in
due menu diversi della barra dei filtri sembrerebbe un errore.

Nella dashboard i livelli si navigano in due modi, che restano allineati fra
loro: i menu "categoria" e "sotto" nella barra dei filtri (il secondo mostra
solo le sottocategorie dell'area scelta), e il riquadro "Voci per costo annuo",
che parte dalle aree e scende nel dettaglio quando ne clicchi una. Il link
"< tutte le categorie" risale.


COM'E' DISPOSTA LA PAGINA
-------------------------

A sinistra una ROTAIA che non scorre via: titolo, pulsanti e tutti i filtri.
Cambiare periodo non richiede piu' di risalire in cima.

A destra l'area di lavoro, che prende tutta la larghezza che c'e'. I riquadri
della dashboard stanno su una griglia a 12 colonne, e ognuno dichiara quante
colonne occupa nella mappa SPAN dentro app.html:

    indicatori 12    natura 5     andamento 7    aree 6
    voci 6           ricorrenti 6 merchant 6     revisione 12

Un riquadro nuovo che non compare in SPAN prende tutta la riga. C'e' un
controllo in test_app.js che lo segnala.

Restringendo la finestra si sfilano prima i due grafici (sotto i 1250px CSS),
poi tutto torna in colonna (1050px), e sotto i 1100px la rotaia diventa una
fascia in cima coi filtri di nuovo in orizzontale.

ATTENZIONE alle soglie: sono in pixel CSS. Su Windows con lo zoom di sistema
al 125% uno schermo da 1720 pixel veri ne dichiara 1375, quindi una soglia che
sembra generosa taglia fuori proprio gli schermi larghi.


DA RIVEDERE: NON MANCA NIENTE
-----------------------------

Le righe qui dentro HANNO GIA' UNA CATEGORIA e contano gia' nei totali. Sono
in coda perche' la confidenza e' sotto 1, cioe' perche' il motore le ha
dedotte invece di saperle: non perche' manchi un dato.

Se non fai niente, non succede niente: la categoria proposta resta in vigore.
La coda e' una lista di lavoro, non una fila di errori.

Tre azioni:

    confermo   fissa la categoria che c'e' gia'. La riga esce dalla coda e nei
               totali non cambia nulla, perche' quella categoria era gia' in
               vigore. Scrive override.csv.

    regola     vale per tutte le transazioni di quel negozio, comprese quelle
               che devono ancora arrivare. Se il negozio si ripete conviene
               questa.

    il menu    corregge la categoria di quella riga sola.


RIFINITURA DELL'INTERFACCIA
--------------------------

Poche cose, quelle che si notano solo quando mancano:

    cifre tabulari      i numeri hanno tutti la stessa larghezza, cosi' le
                        colonne non ballano a ogni aggiornamento. In un'app di
                        soldi cambiano di continuo.
    anello di fuoco     col colore d'accento su :focus-visible. Il contorno di
                        sistema su fondo scuro e' quasi invisibile, e chi
                        naviga da tastiera non sa dove si trova.
    transizioni         160ms su colori e bordi. Un cambio istantaneo si legge
                        come uno sfarfallio.
    pressione           il pulsante scende di un pixel al clic
    text-wrap: pretty   niente parole sole in fondo alle spiegazioni
    favicon             SVG in linea, nessun file da servire

Il movimento rispetta prefers-reduced-motion: chi ha chiesto meno animazioni
non vede pulsare il pallino di "elaboro", che e' l'unica animazione continua.


I RIQUADRI DI DIALOGO
---------------------

Le domande e gli avvisi non usano i popup del browser: sono riquadri
dell'applicazione, che seguono il tema e stanno dietro a un messaggio di piu'
di una riga. Tre forme, tutte da attendere perche' tornano una promessa:

    avvisa(testo)             solo OK
    chiedi(testo)             OK / Annulla, torna vero o falso
    domanda(testo, valore)    con un campo precompilato, torna il testo o null

ATTENZIONE se ci metti mano: la risposta NON si prende dall'evento "close" del
<dialog>. Su alcuni browser quell'evento non arriva mai - verificato, e non
arriva nemmeno "cancel" - e la promessa resterebbe appesa per sempre con la
pagina bloccata dietro a un riquadro che non si chiude. Si prende dai tre modi
in cui si puo' rispondere: submit del modulo, clic su Annulla, tasto Esc
ascoltato direttamente.


I PULSANTI
----------

Caricare i dati e farli analizzare dal modello sono due cose diverse, e i
pulsanti sono separati apposta.

Nella rotaia a sinistra:

    Carica N export      compare solo quando ci sono file in attesa in
                         export/. Legge, archivia, ricostruisce. Secondi.

    Ricarica             rilegge da zero TUTTI gli originali archiviati e
                         ricostruisce il consolidato. Secondi.

    Analizza con LLM     interroga qwen3:8b, e SOLO sulle descrizioni che
    (N)                  nessun livello deterministico ha saputo risolvere.
                         Il numero fra parentesi dice quante sono: a zero il
                         pulsante e' spento. Minuti.

In fondo alla barra delle schede, a destra: "Esporta HTML + Excel" e "Log".

I primi due non chiamano mai il modello: a parita' di file danno sempre lo
stesso consolidato, byte per byte. E' la proprieta' che rende il caricamento
qualcosa di cui ci si puo' fidare.

Non perdono nulla di cio' che il modello ha gia' imparato: categorie_cache.json
viene consultata anche senza Ollama acceso. Le descrizioni MAI VISTE restano
senza categoria finche' non premi "Analizza con LLM" - le trovi nella scheda
Revisione, e spesso conviene scriverci una regola invece di chiedere al
modello.

Il consolidato e' DERIVATO: si riscrive da capo a ogni giro leggendo gli
originali archiviati. Il database vero sono quelli piu' i file di
configurazione (regole.csv, merchant.csv, override.csv, correzioni.csv).


LE SCHEDE
---------

Dashboard          Indicatori e grafici. La barra in alto filtra per periodo,
                   conto, natura, categoria e testo, e i grafici si aggiornano
                   con lei. Cliccare una barra filtra tutto su quella voce.

                   I filtri rapidi seguono il CALENDARIO: "mese in corso" e' il
                   mese di oggi. Se e' vuoto vuol dire che non hai ancora
                   caricato gli export di questo mese, e il messaggio te lo
                   dice indicando dov'e' l'ultimo movimento che hai.

                   "Ultimo mese con dati" e' il pulsante per saltare
                   direttamente li'.

                   Fanno da interruttore: premere di nuovo lo stesso pulsante
                   toglie il filtro.

                   Filtrando su una natura o una categoria, risparmio e tasso
                   di risparmio spariscono: si calcolano sul bilancio intero,
                   e su una fetta di sole uscite darebbero numeri senza senso.
                   Al loro posto compare il peso di quella fetta.

Da rivedere        Le righe con confidenza sotto 1. Scegli la categoria dal
                   menu. Se il negozio si ripete, usa "regola" invece di
                   correggere la singola riga: una regola vale per sempre.

Transazioni        Tutte le righe, e QUASI TUTTI I CAMPI SI CORREGGONO:
                   data, descrizione, merchant, importo, conto e categoria.
                   Ogni modifica si salva da sola e la pipeline rigira
                   subito, non c'e' nessun pulsante "salva".

                   La NATURA no: discende dalla categoria attraverso
                   natura.csv. Cambiarla per riga vorrebbe dire avere due
                   transazioni della stessa voce con nature diverse. Si
                   cambia nella scheda Categorie, e vale per tutte.

                   Dove finisce cosa: la categoria in override.csv, perche'
                   e' un'interpretazione; tutto il resto in correzioni.csv,
                   perche' cambia i fatti. Restano separati apposta, cosi'
                   ricategorizzare non puo' alterare un importo per sbaglio.

                   L'ID della transazione NON cambia mai, nemmeno correggendo
                   data, importo o conto: si corregge quella riga, non se ne
                   crea un'altra.

                   "annulla" toglie sia la categoria forzata sia le correzioni.

Categorie          Rinomina, unisci, cambia natura. "ignora" non cancella le
                   transazioni: dice al motore di non imparare da quella
                   categoria, e le righe vengono ricategorizzate dalle regole.

Regole e merchant  Con anteprima: "prova" dice quante transazioni colpirebbe
                   una regola prima di salvarla.

Componi            Quali riquadri mostrare e in che ordine.

Andamento          Avvicinando il mouse compare una RIGA VERTICALE
                   tratteggiata sul mese e un riquadro coi valori di tutte e
                   tre le serie. La riga serve a spiegare il riquadro: il
                   tooltip legge la COLONNA del mese, non il punto piu'
                   vicino, ed e' per questo che riporta entrate, uscite e
                   uscite ordinarie insieme.

                   La zona sensibile e' una fascia larga quanto il passo fra
                   due mesi, non il pallino: basta avvicinarsi, e funziona
                   anche a cinquantasei mesi dove i pallini non si disegnano.

                   Una linea per entrate, uscite e uscite ordinarie. Con
                   POCHI MESI si vedono anche i pallini sui valori: serve
                   quando il mese e' UNO SOLO, perche' una polilinea di un
                   punto non disegna niente e il riquadro sembrava vuoto pur
                   avendo i dati sotto. Con un mese solo il punto e' centrato
                   e una nota dice perche' non c'e' una curva.

Log                Pannello a destra, si apre col pulsante "Log" e da solo
                   a ogni elaborazione. Mostra la pipeline riga per riga
                   MENTRE gira, non alla fine.

                   Del modello si vede: la descrizione che gli e' stata data,
                   quanto ci ha messo, il suo ragionamento se lo produce, e la
                   risposta esatta. Verde = categoria accettata, rosso =
                   scartata perche' non esiste o perche' si e' astenuto.

                   Scorrere verso l'alto ferma l'inseguimento della coda, cosi'
                   puoi leggere in pace mentre continua a scrivere; tornare in
                   fondo lo riattiva.


ENTRATE CONTRO USCITE
---------------------

Il REDDITO e' solo quello di natura "Entrate": stipendio e affitti incassati.
Tutto il resto col segno positivo - rimborsi, storni, resi Amazon - non e'
guadagno: scala dalla spesa a cui appartiene, dove riduce quella voce.

Sono 11.396 euro su 38 righe. Dividendo per segno i due numeri in cima
dicevano che guadagni 155.067 quando ne guadagni 143.649.

IL RISPARMIO NON CAMBIA di un centesimo: inflow() e outflow() si spartiscono
le stesse righe, senza sovrapporsi e senza perderne. C'e' un controllo in
test_app.js che lo verifica, perche' se una riga finisse in tutte e due, o in
nessuna, il risparmio mentirebbe senza che niente lo dica.


COME DECIDE LA CATEGORIA
------------------------

Sette livelli in cascata. Il primo che risponde vince.

  1. override        la riga e' elencata a mano in override.csv
  2. giroconto       una regola con categoria "Giroconto" riconosce uno
                     spostamento fra conti propri: non e' una spesa
  3. regola          combacia con una riga di regole.csv
  4. storico-esatto  la descrizione esiste identica nello storico MoneyWiz
  5. storico-simile  somiglia a una gia' categorizzata
  6. voto-token      le singole parole votano la categoria (65% corretto)
  7. cache           il modello aveva gia' risposto su questa descrizione
  8. llm             qwen3:8b via Ollama, solo per cio' che resta

LE REGOLE STANNO SOPRA LO STORICO, ed e' una scelta. Lo storico e' quello che
MoneyWiz aveva etichettato negli anni, a volte male: ventinove bollette del
gas erano archiviate come rata del mutuo, e un bar come "Casa > Mobili".
Con l'ordine inverso lo storico decideva l'82% delle righe e una regola
scritta apposta non spostava niente, quindi cambiare la tassonomia era
impossibile se non a colpi di override.

Il prezzo: una regola larga adesso puo' coprire centinaia di righe gia'
etichettate bene. Se scrivi una regola generica, guarda "cambierebbero N"
nell'anteprima prima di salvarla.

Il passo LLM costa circa 3 secondi a descrizione (14 il primo, a modello
freddo). Per questo e' SEPARATO dal caricamento e non parte mai da solo: lo
chiami col pulsante "Analizza con LLM". Le risposte finiscono in
categorie_cache.json, quindi ogni descrizione si chiede una volta sola.

    ollama pull qwen3:8b


I DUE ASSI
----------

  Categoria   cosa hai comprato        Casa > Bollette
  Natura      quanto puoi intervenire  Ricorrenti

  Vincolate        mutuo, assicurazioni, tasse    -> si cambia una volta l'anno
  Ricorrenti       bollette, abbonamenti          -> cambio fornitore, disdette
  Quotidiane       spesa, benzina, farmacia       -> punto vendita, frequenza
  Discrezionali    ristoranti, vestiti, regali    -> decisione singola
  Straordinarie    mobili, ristrutturazioni       -> non comprimibile
  Non spesa        giroconti                      -> esclusa dai totali

Le straordinarie vanno guardate a parte: sono episodiche e, lasciate nella
media mensile, fanno sembrare cattivo un mese normale. La dashboard mostra sia
la spesa totale sia la "spesa ordinaria" che le esclude.


I FILE
------

consolidato.csv   E' DERIVATO, non modificarlo: viene riscritto da zero a ogni
                  elaborazione e le modifiche fatte li' andrebbero perse. Tutto
                  quello che correggi finisce nei file qui sotto.

scartate.csv      Anch'esso DERIVATO: le righe che la pipeline ha tolto, con la
                  colonna "Scartata da" che dice quale passo le ha prese
                  (saldo, doppione fra file, artefatto di importazione,
                  coperta da sorgente superiore, giroconto). Serve a
                  controllare che non stia mangiando transazioni vere: i
                  conteggi stampati nel log dicono quante, questo file dice
                  quali.

override.csv      La categoria decisa a mano per una singola transazione.
                  Identificata dall'ID, oppure da data+importo se preferisci
                  scriverla a mano senza cercare l'ID.

correzioni.csv    id;campo;valore - corregge importo, data o descrizione.
                  Separato da override.csv perche' cambia i FATTI della
                  transazione, non la sua interpretazione.

regole.csv        pattern;categoria - il pattern e' una regex. Vince la prima
                  riga che combacia, quindi le regole specifiche vanno SOPRA
                  le generiche.

merchant.csv      pattern;merchant - unifica le grafie dello stesso negozio.
                  Serve anche a dire che DUE DESCRIZIONI SONO LO STESSO POSTO
                  quando non si somigliano affatto: "Rata condominio" e
                  "BONIFICO A BORGO MANGANO" finiscono entrambe su
                  "Borgo Mangano", e cosi' la stessa spesa vista dalla banca e
                  da Splitwise non viene contata due volte. Quando due righe
                  hanno stesso importo e stessa data ma descrizioni diverse, il
                  log le elenca sotto "coppie non fuse": e' li' che si vede
                  dove aggiungere una riga.
                  Iper Rossetto compariva con 56 scritture diverse per 7.828
                  euro: senza questo file non lo vedi in nessuna classifica.

natura.csv        categoria;natura - il secondo asse.

conti.csv         pattern;conto - da quale conto viene un export, dal nome del
                  file. Senza, il "conto" e' il nome del file e due export
                  dello stesso conto in mesi diversi diventano due conti. Non
                  e' estetica: il riconoscimento dei giroconti guarda proprio
                  che i conti siano diversi.

categorie_merge.csv  Unifica le categorie duplicate di MoneyWiz. Colonna 1 com'e'
                  adesso, colonna 3 come deve diventare. Scrivi IGNORA nella
                  colonna 3 per non far imparare quella categoria: serve per i
                  cassetti dei rifiuti tipo "Casa > Altro", che insegnavano al
                  motore a sbagliare.

L'ID di una transazione e' calcolato dai suoi valori ORIGINALI (conto, data,
importo, descrizione) piu' un contatore per i duplicati esatti. Serve perche'
resti lo stesso anche dopo che ne hai corretto l'importo: altrimenti la
correzione si staccherebbe dalla sua riga alla prima rielaborazione.


DA DOVE VENGONO LE CATEGORIE
----------------------------

Da backup/iMoneyWiz-Data-Backup-*.zip, dal database SQLite: 1635 transazioni
gia' categorizzate a mano, con la gerarchia padre/figlio. Viene usato il backup
piu' recente. Per aggiornare lo storico basta metterne uno piu' nuovo in
backup/.

Le TRANSAZIONI dello storico sono state migrate una volta sola in
export/elaborati/storico-moneywiz.csv con:

    python bilancio.py --migra-storico

Da quel file in poi lo storico serve SOLO come maestro delle categorie.

Rilanciare la migrazione e' sicuro: scrive sempre lo stesso percorso, quindi
sovrascrive e non duplica. Serve rifarla quando metti in backup/ uno zip
MoneyWiz piu' recente. Per annullarla basta cancellare il file prodotto: il
backup non viene mai toccato.


VERIFICHE
---------

    python bilancio.py --selfcheck     motore di categorizzazione
    node test_app.js state.json        interfaccia, senza aprire un browser

Per il secondo serve lo stato del server. Scaricalo in una cartella
temporanea, NON qui: sono 534 KB di movimenti bancari e finirebbero su iCloud
senza motivo.

    curl -s http://127.0.0.1:8770/api/state -o "$TEMP/state.json"
    node test_app.js "$TEMP/state.json"


NOTE
----

- ATTENZIONE al 2022 e al 2023: NON SONO ANNI COMPLETI, e Splitwise non li ha
  sistemati. Misurato sul consolidato di adesso:

        anno   righe   uscite      entrate
        2022     101    -6.708            0
        2023     131   -12.385      19.769
        2024     462   -48.607      49.796
        2025     672   -55.980      53.236

  Il 2022 ha ZERO entrate e il 2023 quasi solo spese condivise: di quegli anni
  non esiste ne' un estratto conto ne' uno stipendio registrato. Ogni grafico
  pluriennale del risparmio li disegna quindi come una catastrofe che non e'
  mai avvenuta. I confronti fra anni vanno fatti dal 2024 in poi.
- transaction_processor.py e' la versione precedente, superata da bilancio.py.
- HANDOFF.md racconta lo stato del lavoro, le decisioni prese e le trappole
  gia' incontrate. Va letto prima di rimetterci mano, e aggiornato dopo.
