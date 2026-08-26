"""Audit dei dati: cerca gli errori che il caricamento non puo' vedere.

    python audit.py

Il motore controlla che ogni riga sia ben formata. Questo controlla che
l'INSIEME abbia senso: doppioni fra export sovrapposti, mesi mancanti dentro
al periodo di un conto, coppie che dovrebbero annullarsi e non lo fanno,
la stessa voce classificata in due modi diversi.

Non tocca i file originali della banca: legge solo consolidato.csv, che il
motore riscrive da capo a ogni giro, e i file di configurazione.
"""
import csv
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date

CONSOLIDATO = "consolidato.csv"
CONTI = "conti.csv"
SEP = ";"


def leggi(percorso):
    with open(percorso, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=SEP))


def importo(riga):
    testo = (riga.get("Importo") or "").replace(",", ".").strip()
    try:
        return float(testo)
    except ValueError:
        return float("nan")


def giorni_fra(a, b):
    """Quanti giorni separano due date ISO. Senza date, infinito."""
    if not a or not b:
        return 10**6
    try:
        return (date.fromisoformat(a) - date.fromisoformat(b)).days
    except ValueError:
        return 10**6


def mese(riga):
    return (riga.get("Data") or "")[:7]


def conti_bancari():
    """I nomi dei conti bancari, presi da conti.csv, non scritti qui a mano.

    conti.csv e' il file che decide come si chiama un conto: una lista fissa
    copiata qui scadrebbe, in silenzio, alla prima banca aggiunta o rinominata
    (continuerebbe a girare, e a non segnalare piu' niente). Splitwise vive
    nello stesso file ma non e' una banca: e' la fonte delle quote stesse, non
    un estratto conto, quindi resta fuori a mano.
    """
    try:
        righe = leggi(CONTI)
    except FileNotFoundError:
        return set()
    return {(x.get("conto") or "").strip() for x in righe} - {"", "Splitwise"}


class Referto:
    """Le voci trovate, ognuna con un titolo, un peso e qualche esempio."""

    def __init__(self):
        self.voci = []

    def aggiungi(self, gravita, titolo, quante, esempi=(), spiega=""):
        if not quante:
            return
        self.voci.append((gravita, titolo, quante, list(esempi)[:5], spiega))

    def stampa(self):
        ordine = {"errore": 0, "sospetto": 1, "nota": 2}
        self.voci.sort(key=lambda v: (ordine.get(v[0], 3), -v[2]))
        if not self.voci:
            print("nessun problema trovato")
            return 0
        segno = {"errore": "!!", "sospetto": " ?", "nota": "  "}
        for gravita, titolo, quante, esempi, spiega in self.voci:
            print(f"{segno.get(gravita, '  ')} {titolo}: {quante}")
            if spiega:
                print(f"     {spiega}")
            for e in esempi:
                print(f"     - {e}")
        return sum(1 for v in self.voci if v[0] == "errore")


# --------------------------------------------------------------------------
# i controlli
# --------------------------------------------------------------------------

def controlla_forma(righe, r):
    """Campi mancanti o impossibili. Il motore li lascia passare: qui pesano."""
    senza_data = [x for x in righe if not (x.get("Data") or "").strip()]
    r.aggiungi("errore", "righe senza data", len(senza_data),
               [x.get("Descrizione", "")[:50] for x in senza_data])

    senza_conto = [x for x in righe if not (x.get("Conto") or "").strip()]
    r.aggiungi("errore", "righe senza conto", len(senza_conto),
               [f"{x.get('Data')} {x.get('Descrizione','')[:40]}" for x in senza_conto])

    rotte = [x for x in righe if math.isnan(importo(x))]
    r.aggiungi("errore", "importi illeggibili", len(rotte),
               [f"{x.get('Data')} {x.get('Importo')!r}" for x in rotte])

    zeri = [x for x in righe if not math.isnan(importo(x)) and importo(x) == 0]
    r.aggiungi("sospetto", "importi a zero", len(zeri),
               [f"{x.get('Data')} {x.get('Descrizione','')[:40]}" for x in zeri],
               "una riga da zero euro non e' una spesa: di solito e' un residuo")

    oggi = date.today().isoformat()
    future = [x for x in righe if (x.get("Data") or "") > oggi]
    r.aggiungi("errore", "date nel futuro", len(future),
               [f"{x.get('Data')} {x.get('Descrizione','')[:40]}" for x in future],
               f"oggi e' {oggi}")

    ids = Counter(x.get("ID") for x in righe)
    doppi = [k for k, n in ids.items() if n > 1]
    r.aggiungi("errore", "ID ripetuti", len(doppi), doppi,
               "l'interfaccia corregge per ID: due righe con lo stesso ID si "
               "sovrascrivono a vicenda")


def controlla_doppioni(righe, r):
    """Due export che si sovrappongono lasciano la stessa spesa due volte."""
    visti = defaultdict(list)
    for x in righe:
        chiave = (x.get("Data"), x.get("Conto"), round(importo(x), 2),
                  (x.get("Descrizione") or "").strip().lower())
        visti[chiave].append(x)
    gruppi = [v for v in visti.values() if len(v) > 1]
    # Stessa data, stesso conto, stesso importo, stessa descrizione: puo'
    # succedere davvero (due caffe' uguali), ma tre volte di fila no.
    quasi_certi = [g for g in gruppi if len(g) > 2]
    r.aggiungi("errore", "stessa riga tre o piu' volte", len(quasi_certi),
               [f"{g[0].get('Data')} {g[0].get('Descrizione','')[:35]} "
                f"x{len(g)} ({g[0].get('Importo')})" for g in quasi_certi])
    coppie = [g for g in gruppi if len(g) == 2]
    r.aggiungi("sospetto", "righe identiche in coppia", len(coppie),
               [f"{g[0].get('Data')} {g[0].get('Descrizione','')[:35]} "
                f"({g[0].get('Importo')})" for g in coppie],
               "puo' essere legittimo, ma controlla se due export si "
               "sovrappongono")


def controlla_buchi(righe, r):
    """Un mese vuoto dentro al periodo attivo di un conto = export mancante."""
    per_conto = defaultdict(set)
    for x in righe:
        m = mese(x)
        if m:
            per_conto[x.get("Conto") or "?"].add(m)
    # Solo per i conti che si usano davvero: su un conto da due movimenti al
    # mese, un mese vuoto e' una vacanza, non un export mancante.
    densita = Counter(f"{x.get('Conto')}|{mese(x)}" for x in righe if mese(x))
    for conto, mesi in sorted(per_conto.items()):
        if len(mesi) < 3:
            continue
        conteggi = sorted(densita[f"{conto}|{m}"] for m in mesi)
        if conteggi[len(conteggi)//2] < 8:
            continue
        primo, ultimo = min(mesi), max(mesi)
        attesi = []
        y, m = int(primo[:4]), int(primo[5:7])
        while f"{y:04d}-{m:02d}" <= ultimo:
            attesi.append(f"{y:04d}-{m:02d}")
            m += 1
            if m == 13:
                y, m = y + 1, 1
        # E si guardano i mesi VICINI, non la media di sempre: un conto molto
        # usato nel 2022 e quasi fermo nel 2025 non ha un buco nel 2025, ha
        # smesso di essere il conto principale.
        def vivo(indice):
            intorno = [densita[f"{conto}|{attesi[j]}"]
                       for j in (indice-2, indice-1, indice+1, indice+2)
                       if 0 <= j < len(attesi)]
            return sum(intorno) >= 8 * len(intorno) if intorno else False

        buchi = [a for i, a in enumerate(attesi) if a not in mesi and vivo(i)]
        r.aggiungi("sospetto", f"mesi senza movimenti su {conto}", len(buchi),
                   buchi, f"il conto va da {primo} a {ultimo}")


def controlla_tassonomia(righe, r):
    """La stessa voce non puo' avere due nature: la natura sta nella coppia."""
    nature = defaultdict(set)
    for x in righe:
        voce = (x.get("Categoria") or "", x.get("Sottocategoria") or "")
        if voce[0]:
            nature[voce].add(x.get("Natura") or "")
    incoerenti = {v: n for v, n in nature.items() if len(n) > 1}
    r.aggiungi("errore", "voci con piu' di una natura", len(incoerenti),
               [f"{v[0]} > {v[1]}: {', '.join(sorted(n))}"
                for v, n in incoerenti.items()],
               "la natura appartiene alla coppia categoria/sottocategoria")

    orfane = [x for x in righe
              if (x.get("Categoria") or "") and not (x.get("Sottocategoria") or "")
              and (x.get("Natura") or "") not in ("Entrate", "Giroconto", "")]
    conteggio = Counter((x.get("Categoria"), x.get("Natura")) for x in orfane)
    r.aggiungi("nota", "righe di spesa senza sottocategoria", len(orfane),
               [f"{c} ({n}): {q}" for (c, n), q in conteggio.most_common()])

    scoperte = [x for x in righe if not (x.get("Categoria") or "").strip()]
    r.aggiungi("sospetto", "righe senza categoria", len(scoperte),
               [f"{x.get('Data')} {x.get('Descrizione','')[:40]}" for x in scoperte])


def controlla_segni(righe, r):
    """Una categoria di entrate piena di uscite (o viceversa) e' un errore."""
    per_voce = defaultdict(lambda: [0, 0, 0.0])
    for x in righe:
        v = importo(x)
        if math.isnan(v) or not (x.get("Categoria") or ""):
            continue
        voce = (x.get("Natura") or "", x.get("Categoria") or "")
        per_voce[voce][0 if v >= 0 else 1] += 1
        per_voce[voce][2] += v
    sospette = []
    for (natura, cat), (pos, neg, tot) in sorted(per_voce.items()):
        if natura == "Entrate" and neg and neg >= pos:
            sospette.append(f"{cat} (Entrate): {neg} righe negative su {neg+pos}")
        # I giroconti sono per meta' positivi per costruzione: e' il loro
        # mestiere. Li guarda controlla_giroconti, con la domanda giusta.
        if cat == "Giroconto":
            continue
        if natura not in ("Entrate", "Giroconto") and pos > neg and pos > 2:
            sospette.append(f"{cat} ({natura}): {pos} righe positive su {neg+pos}")
    r.aggiungi("sospetto", "voci col segno rovesciato", len(sospette), sospette,
               "qualche rimborso e' normale; una maggioranza no")


def controlla_giroconti(righe, r):
    """I giroconti dovrebbero annullarsi: quel che resta e' spesa nascosta."""
    giri = [x for x in righe if (x.get("Natura") or "") == "Giroconto"
            or (x.get("Categoria") or "") == "Giroconto"]
    if not giri:
        return
    # Ogni giroconto ha due gambe, una che esce e una che entra. Si cercano a
    # coppie: quel che resta spaiato e' denaro che sparisce dal quadro, ne'
    # speso ne' risparmiato.
    usati = set()
    spaiati = []
    for uscita in giri:
        if id(uscita) in usati:
            continue
        gemella = None
        for entrata in giri:
            if entrata is uscita or id(entrata) in usati:
                continue
            if abs(importo(entrata) + importo(uscita)) > 0.02:
                continue
            if abs(giorni_fra(entrata.get("Data"), uscita.get("Data"))) > 4:
                continue
            gemella = entrata
            break
        if gemella is None:
            spaiati.append(uscita)
        else:
            usati.add(id(uscita))
            usati.add(id(gemella))
    if not spaiati:
        return
    # Le due direzioni non pesano uguale. Un'USCITA spaiata e' denaro che se
    # ne va senza comparire da nessuna parte: e' un errore. Un'ENTRATA spaiata
    # e' l'altra meta' di un bonifico partito da un conto caricato piu' tardi:
    # e' innocua, purche' resti fuori dal reddito.
    esce = sorted([x for x in spaiati if importo(x) < 0], key=importo)
    entra = [x for x in spaiati if importo(x) > 0]
    uscito = sum(importo(x) for x in esce)
    r.aggiungi("errore", "uscite marcate giroconto che nessuno riceve",
               len(esce),
               [f"{x.get('Data')} {x.get('Conto')} {importo(x):+,.0f}: "
                f"{(x.get('Descrizione') or '')[:44]}" for x in esce],
               f"{uscito:+,.0f} EUR fuori dal quadro: ne' spesi ne' "
               f"risparmiati")
    r.aggiungi("nota", "entrate marcate giroconto senza la gamba che parte",
               len(entra),
               [f"{sum(importo(x) for x in entra):+,.0f} EUR, dal conto che "
                f"le manda prima che fosse caricato"])


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
    banca = conti_bancari()
    contrarie = [x for x in quote
                 if x.get("Pagato da") == "lei" and x.get("Conto") in banca]
    r.aggiungi("sospetto", "righe di banca marcate 'pagata da lei'",
               len(contrarie),
               [f"{x.get('Data')} {x.get('Conto')} "
                f"{(x.get('Descrizione') or '')[:34]}" for x in contrarie],
               "se sono tre e' un refuso, se sono trenta e' un modo di usare "
               "l'app che il modello deve descrivere")


def controlla_entrate_rovesciate(righe, r):
    """Righe che si DICHIARANO entrate ma hanno il segno di un'uscita.

    Un estratto conto scrive "bonifico a vostro favore DA:" quando i soldi
    arrivano. Se quella riga e' negativa, o il segno e' sbagliato o la
    descrizione mente, e in tutti e due i casi il totale della sua categoria
    e' falso del doppio dell'importo.

    Trovato sui dati veri: otto bonifici in entrata da un familiare, fra
    dicembre 2023 e giugno 2024, registrati come uscite dallo storico MoneyWiz.
    Le stesse identiche righe prima e dopo quel periodo erano positive.
    """
    dice_entrata = re.compile(r"(?i)a vostro favore|accredito|vs favore")
    sospette = [x for x in righe
                if dice_entrata.search(x.get("Descrizione") or "")
                and importo(x) < 0]
    r.aggiungi("errore", "entrate col segno di un'uscita", len(sospette),
               [f"{x.get('Data')} {importo(x):+,.0f} "
                f"{(x.get('Descrizione') or '')[:44]}" for x in sospette],
               "la descrizione dice che i soldi sono entrati: il totale della "
               "categoria sbaglia del doppio")


def controlla_merchant(righe, r):
    """Lo stesso negozio scritto in due modi si spezza in due voci."""
    forme = defaultdict(set)
    for x in righe:
        nome = (x.get("Merchant") or "").strip()
        if nome:
            forme[nome.lower().replace(" ", "")].add(nome)
    doppie = {k: v for k, v in forme.items() if len(v) > 1}
    r.aggiungi("nota", "negozi scritti in piu' modi", len(doppie),
               [" / ".join(sorted(v)) for v in doppie.values()])


def controlla_estremi(righe, r):
    """Gli importi fuori scala: spesso e' una virgola nel posto sbagliato."""
    valori = sorted(abs(importo(x)) for x in righe
                    if not math.isnan(importo(x)) and importo(x))
    if len(valori) < 20:
        return
    mediana = valori[len(valori)//2]
    soglia = max(mediana * 200, 20000)
    fuori = [x for x in righe if not math.isnan(importo(x))
             and abs(importo(x)) > soglia]
    r.aggiungi("nota", "importi fuori scala", len(fuori),
               [f"{x.get('Data')} {x.get('Descrizione','')[:35]} "
                f"{importo(x):+,.0f}" for x in fuori],
               f"oltre {soglia:,.0f} EUR, con una mediana di {mediana:,.0f} EUR")


def main():
    try:
        righe = leggi(CONSOLIDATO)
    except FileNotFoundError:
        print(f"manca {CONSOLIDATO}: fai girare bilancio.py prima")
        return 2
    print(f"audit su {len(righe)} righe di {CONSOLIDATO}\n")
    r = Referto()
    for controllo in (controlla_forma, controlla_doppioni, controlla_buchi,
                      controlla_tassonomia, controlla_segni,
                      controlla_giroconti, controlla_quote,
                      controlla_entrate_rovesciate, controlla_merchant,
                      controlla_estremi):
        controllo(righe, r)
    errori = r.stampa()
    print(f"\n{len(r.voci)} voci, di cui {errori} da correggere")
    return 1 if errori else 0


if __name__ == "__main__":
    sys.exit(main())
