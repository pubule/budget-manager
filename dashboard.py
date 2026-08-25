#!/usr/bin/env python3
"""
Dashboard delle spese familiari.

Genera dashboard.html e dashboard.xlsx da consolidato.csv, a quattro livelli di
granularita': natura, area, voce, merchant.

I grafici sono SVG generati qui dentro: nessuna libreria, nessuna CDN, la
pagina si apre offline e i dati non escono dal PC.
"""

import html
from collections import Counter

import pandas as pd

import bilancio

# I giroconti non sono spesa: spostare soldi fra conti propri non impoverisce.
NOT_SPENDING = "Non spesa"

# Ordine e colore delle nature, dalla meno alla piu' comprimibile.
NATURE = [
    ("Vincolate", "#7c6a9c", "surroga, cambio polizza", "annuale"),
    ("Ricorrenti", "#4a7fb5", "cambio fornitore, disdette", "trimestrale"),
    ("Quotidiane", "#3f9e8c", "punto vendita, frequenza", "settimanale"),
    ("Discrezionali", "#d99442", "decisione singola", "immediato"),
    ("Straordinarie", "#b5654a", "non comprimibile, va isolata", "-"),
    ("Da classificare", "#8a8a8a", "da etichettare", "-"),
]
COLORS = {name: color for name, color, _, _ in NATURE}


def euro(value):
    """Importo con separatore delle migliaia all'italiana."""
    return f"{value:,.0f}".replace(",", ".") + " EUR"


# --------------------------------------------------------------------------
# grafici SVG
# --------------------------------------------------------------------------

def bar_chart(items, width=760, row_height=30):
    """Barre orizzontali. items = [(etichetta, valore, colore)]."""
    if not items:
        return "<p>nessun dato</p>"
    top = max(abs(v) for _, v, _ in items) or 1
    label_w, pad = 200, 130
    height = len(items) * row_height + 10
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'style="width:100%;height:auto">']
    for i, (label, value, color) in enumerate(items):
        y = i * row_height + 6
        bar = (abs(value) / top) * (width - label_w - pad)
        parts.append(
            f'<text x="0" y="{y + 14}" class="lbl">'
            f'{html.escape(str(label))[:32]}</text>'
            f'<rect x="{label_w}" y="{y + 3}" width="{bar:.1f}" height="16" '
            f'rx="3" fill="{color}"/>'
            f'<text x="{label_w + bar + 8:.1f}" y="{y + 15}" class="val">'
            f'{euro(value)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def line_chart(months, series, width=760, height=230):
    """Andamento mensile. series = [(nome, [valori], colore)]."""
    if not months:
        return "<p>nessun dato</p>"
    flat = [v for _, values, _ in series for v in values]
    top = max(flat + [0]) or 1
    bottom = min(flat + [0])
    span = (top - bottom) or 1
    left, base = 56, height - 26

    def point(index, value):
        x = left + index * (width - left - 12) / max(len(months) - 1, 1)
        y = 12 + (top - value) / span * (base - 12)
        return x, y

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'style="width:100%;height:auto">']
    for frac in (0, 0.5, 1):
        value = top - frac * span
        y = 12 + frac * (base - 12)
        parts.append(f'<line x1="{left}" y1="{y:.0f}" x2="{width - 12}" '
                     f'y2="{y:.0f}" class="grid"/>'
                     f'<text x="0" y="{y + 4:.0f}" class="axis">'
                     f'{value / 1000:.1f}k</text>')
    for _, values, color in series:
        coords = " ".join(f"{x:.1f},{y:.1f}"
                          for x, y in (point(i, v) for i, v in enumerate(values)))
        parts.append(f'<polyline points="{coords}" fill="none" '
                     f'stroke="{color}" stroke-width="2.5" '
                     f'stroke-linejoin="round"/>')
    # Etichette dei mesi diradate, altrimenti si sovrappongono su 24 mesi.
    step = max(1, len(months) // 12)
    for i in range(0, len(months), step):
        x, _ = point(i, top)
        parts.append(f'<text x="{x:.0f}" y="{height - 6}" class="axis" '
                     f'text-anchor="middle">{months[i][2:]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def legend(series):
    chips = "".join(
        f'<span class="chip"><i style="background:{color}"></i>'
        f'{html.escape(name)}</span>' for name, _, color in series)
    return f'<div class="legend">{chips}</div>'


def table(headers, rows, align_right=()):
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = []
    for row in rows:
        cells = "".join(
            f'<td{" class=num" if i in align_right else ""}>{cell}</td>'
            for i, cell in enumerate(row))
        body.append(f"<tr>{cells}</tr>")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


# --------------------------------------------------------------------------
# calcoli
# --------------------------------------------------------------------------

def recurring(frame, min_months=6):
    """Merchant presenti in molti mesi: la lista di partenza per le disdette."""
    out = []
    for merchant, group in frame[frame.Importo < 0].groupby("Merchant"):
        months = group["Mese"].nunique()
        if months < min_months or not merchant:
            continue
        total = group.Importo.sum()
        labels = Counter(group.Voce.dropna())
        out.append({
            "merchant": merchant,
            "categoria": labels.most_common(1)[0][0] if labels else "",
            "mesi": months,
            "tx": len(group),
            "totale": total,
            "annuo": total / months * 12,
        })
    return sorted(out, key=lambda row: row["annuo"])


def build(frame):
    """Prepara le viste che servono alle quattro sezioni."""
    frame = frame.copy()
    frame["Data"] = pd.to_datetime(frame["Data"], errors="coerce")
    frame = frame.dropna(subset=["Data"])
    frame["Mese"] = frame["Data"].dt.strftime("%Y-%m")
    frame["Anno"] = frame["Data"].dt.year
    # Il nome intero, per i riquadri che ragionano sulla voce e non sull'area:
    # "Casa" da sola mette insieme le bollette e la ristrutturazione.
    frame["Voce"] = [bilancio.join_category(a, s) or "Senza categoria"
                     for a, s in zip(frame.get("Categoria", []),
                                     frame.get("Sottocategoria", []))]

    real = frame[frame["Natura"] != NOT_SPENDING]
    return {
        "all": frame,
        "real": real,
        "out": real[real.Importo < 0],
        "income": real[real.Importo > 0],
        "moves": frame[frame["Natura"] == NOT_SPENDING],
        "months": max(real["Mese"].nunique(), 1),
    }


# --------------------------------------------------------------------------
# pagina
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#fbfaf8;--card:#fff;--ink:#1c1a17;--dim:#6b6560;--line:#e6e1da;
--accent:#3f6f5f;--warn:#b5654a}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16161a;--card:#1e1e24;--ink:#eceaf0;--dim:#9c96a3;--line:#31313a;
--accent:#6fae99;--warn:#d98a6a}}
:root[data-theme=dark]{--bg:#16161a;--card:#1e1e24;--ink:#eceaf0;--dim:#9c96a3;
--line:#31313a;--accent:#6fae99;--warn:#d98a6a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 "Iowan Old Style",Georgia,serif;padding:34px 20px 64px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:31px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.13em;color:var(--dim);
margin:40px 0 12px;font-family:ui-sans-serif,system-ui,sans-serif}
.sub{color:var(--dim);margin:0 0 26px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px 22px;margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px}
.kpi b{display:block;font-size:24px;letter-spacing:-.02em;
font-family:ui-sans-serif,system-ui,sans-serif;font-weight:600}
.kpi span{font-size:10px;text-transform:uppercase;letter-spacing:.1em;
color:var(--dim);font-family:ui-sans-serif,system-ui,sans-serif}
.kpi.good b{color:var(--accent)}
.kpi.bad b{color:var(--warn)}
text.lbl{font:12px ui-sans-serif,system-ui,sans-serif;fill:var(--ink)}
text.val{font:11px ui-monospace,SFMono-Regular,monospace;fill:var(--dim)}
text.axis{font:10px ui-sans-serif,system-ui,sans-serif;fill:var(--dim)}
line.grid{stroke:var(--line);stroke-width:1}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;
font:12px ui-sans-serif,system-ui,sans-serif;color:var(--dim)}
.legend i{display:inline-block;width:11px;height:11px;border-radius:2px;
margin-right:5px;vertical-align:-1px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;
font-family:ui-sans-serif,system-ui,sans-serif}
th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.09em;
color:var(--dim);border-bottom:1px solid var(--line);padding:7px 12px 7px 0;
font-weight:600;white-space:nowrap}
td{padding:7px 12px 7px 0;border-bottom:1px solid var(--line)}
td.num{text-align:right;font-family:ui-monospace,SFMono-Regular,monospace;
white-space:nowrap}
tr:last-child td{border-bottom:none}
.note{font-size:13px;color:var(--dim);border-left:2px solid var(--line);
padding-left:14px;margin:14px 0}
"""


def render(data, title="Bilancio familiare"):
    out, income, months = data["out"], data["income"], data["months"]
    total_out = out.Importo.sum()
    total_in = income.Importo.sum()
    saving = total_in + total_out
    rate = saving / total_in if total_in else 0
    ordinary = out[out.Natura != "Straordinarie"].Importo.sum()

    count = f"{len(data['all']):,}".replace(",", ".")
    body = [
        f"<h1>{html.escape(title)}</h1>",
        f'<p class="sub">{count} transazioni · {months} mesi · '
        f'{out.Data.min():%m/%Y} – {out.Data.max():%m/%Y}</p>',
    ]

    # ---- riepilogo -------------------------------------------------------
    body.append(
        '<div class="kpis">'
        f'<div class="kpi"><span>entrate / mese</span>'
        f'<b>{euro(total_in / months)}</b></div>'
        f'<div class="kpi"><span>uscite / mese</span>'
        f'<b>{euro(-total_out / months)}</b></div>'
        f'<div class="kpi {"good" if saving > 0 else "bad"}">'
        f'<span>risparmio / mese</span><b>{euro(saving / months)}</b></div>'
        f'<div class="kpi"><span>tasso di risparmio</span><b>{rate:.0%}</b></div>'
        f'<div class="kpi"><span>spesa ordinaria / mese</span>'
        f'<b>{euro(-ordinary / months)}</b></div>'
        '</div>')
    body.append('<p class="note">La <b>spesa ordinaria</b> esclude le '
                'straordinarie (mobili, ristrutturazioni, manutenzioni): sono '
                'episodiche e, lasciate dentro la media, fanno sembrare cattivo '
                'un mese normale.</p>')

    # ---- livello 1: natura -----------------------------------------------
    body.append("<h2>Dove puoi agire</h2>")
    by_nature = out.groupby("Natura").Importo.sum()
    items = [(name, by_nature[name] / months, COLORS[name])
             for name, _, _, _ in NATURE if name in by_nature.index]
    body.append(f'<div class="card">{bar_chart(items)}'
                '<p class="note">Media mensile. Le prime si cambiano una volta '
                "l'anno, le ultime domani mattina.</p></div>")

    rows = []
    for name, _, lever, horizon in NATURE:
        if name not in by_nature.index:
            continue
        value = by_nature[name]
        rows.append([html.escape(name), euro(value / months),
                     f"{value / total_out:.0%}", html.escape(lever),
                     html.escape(horizon)])
    body.append(table(["natura", "al mese", "quota", "leva", "orizzonte"],
                      rows, align_right=(1, 2)))

    # ---- andamento -------------------------------------------------------
    body.append("<h2>Andamento</h2>")
    months_list = sorted(out.Mese.unique())
    spend = out.groupby("Mese").Importo.sum().reindex(months_list, fill_value=0)
    ord_spend = (out[out.Natura != "Straordinarie"].groupby("Mese").Importo.sum()
                 .reindex(months_list, fill_value=0))
    earn = income.groupby("Mese").Importo.sum().reindex(months_list, fill_value=0)
    series = [("entrate", list(earn), "#3f9e8c"),
              ("uscite", [-v for v in spend], "#b5654a"),
              ("uscite ordinarie", [-v for v in ord_spend], "#d99442")]
    body.append(f'<div class="card">{line_chart(months_list, series)}'
                f"{legend(series)}</div>")

    # ---- livello 2: aree -------------------------------------------------
    body.append("<h2>Aree, anno su anno</h2>")
    areas = out.Categoria.fillna("Senza categoria")
    out_area = out.assign(Area=areas)
    years = sorted(out_area.Anno.unique())[-2:]
    pivot = (out_area[out_area.Anno.isin(years)]
             .pivot_table(index="Area", columns="Anno", values="Importo",
                          aggfunc="sum").fillna(0))
    rows = []
    for area, row in pivot.sort_values(pivot.columns[-1]).iterrows():
        old = row.get(years[0], 0) if len(years) > 1 else 0
        new = row.get(years[-1], 0)
        delta = new - old
        pct = f"{delta / abs(old):+.0%}" if old else "—"
        style = "" if delta >= 0 else ' style="color:var(--warn)"'
        rows.append([html.escape(str(area)), euro(old), euro(new),
                     f"<span{style}>{euro(delta)}</span>", pct])
    body.append(table(["area", str(years[0]) if len(years) > 1 else "—",
                       str(years[-1]), "differenza", "%"],
                      rows, align_right=(1, 2, 3, 4)))
    body.append('<p class="note">Differenza negativa = quest\'anno hai speso '
                'di piu\'.</p>')

    # ---- livello 3: voci e ricorrenti ------------------------------------
    body.append("<h2>Voci per costo annuo</h2>")
    by_cat = out.groupby("Voce").Importo.agg(["sum", "count"]).sort_values("sum")
    rows = [[html.escape(str(cat)), euro(row["sum"] / months * 12),
             euro(row["sum"] / months), int(row["count"])]
            for cat, row in by_cat.head(20).iterrows()]
    body.append(table(["voce", "all'anno", "al mese", "tx"], rows,
                      align_right=(1, 2, 3)))

    body.append("<h2>Costi ricorrenti</h2>")
    rows = [[html.escape(str(r["merchant"])), html.escape(str(r["categoria"])),
             f"{r['mesi']}/{months}", euro(r["annuo"]),
             euro(r["totale"] / r["mesi"])]
            for r in recurring(out)[:20]]
    body.append(table(["merchant", "categoria", "mesi attivi", "costo annuo",
                       "al mese"], rows, align_right=(2, 3, 4)))
    body.append('<p class="note">Presenti in almeno 6 mesi distinti. E\' la '
                'lista da cui partire per disdette e cambi fornitore: ogni riga '
                'e\' un costo che continua da solo finche\' non lo fermi.</p>')

    # ---- livello 4: merchant ---------------------------------------------
    body.append("<h2>Dove finiscono i soldi</h2>")
    named = out[out.Merchant.notna() & (out.Merchant != "")]
    by_shop = named.groupby("Merchant").Importo.sum().sort_values()
    items = [(shop, value / months, "#4a7fb5")
             for shop, value in by_shop.head(18).items()]
    body.append(f'<div class="card">{bar_chart(items)}'
                '<p class="note">Media mensile per negozio. Se lo stesso posto '
                'compare due volte con nomi diversi, aggiungilo a '
                "merchant.csv.</p></div>")

    if len(data["moves"]):
        moves = data["moves"]
        body.append("<h2>Esclusi dai totali</h2>")
        body.append(f'<p class="note">{len(moves)} giroconti per '
                    f'{euro(moves.Importo.sum())}: spostamenti fra conti tuoi o '
                    'verso familiari. Non sono spese, contarli falserebbe sia le '
                    'uscite sia il tasso di risparmio.</p>')

    return (f"<title>{html.escape(title)}</title><style>{CSS}</style>"
            f'<div class="wrap">{"".join(body)}</div>')


# --------------------------------------------------------------------------

def write_excel(data, path):
    """Un foglio per livello, piu' il dettaglio completo filtrabile."""
    out, months = data["out"], data["months"]
    areas = out.Categoria.fillna("Senza categoria")
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        (out.groupby("Natura").Importo.agg(totale="sum", transazioni="count")
         .assign(al_mese=lambda d: d.totale / months)
         .sort_values("totale").to_excel(writer, sheet_name="1 natura"))
        (out.assign(Area=areas)
         .pivot_table(index="Area", columns="Anno", values="Importo",
                      aggfunc="sum")
         .to_excel(writer, sheet_name="2 aree"))
        (out.groupby("Voce").Importo.agg(totale="sum", transazioni="count")
         .assign(al_mese=lambda d: d.totale / months,
                 all_anno=lambda d: d.totale / months * 12)
         .sort_values("totale").to_excel(writer, sheet_name="3 voci"))
        pd.DataFrame(recurring(out)).to_excel(writer, sheet_name="4 ricorrenti",
                                              index=False)
        (out.groupby("Merchant").Importo.agg(totale="sum", transazioni="count")
         .sort_values("totale").to_excel(writer, sheet_name="5 merchant"))
        (out.groupby("Mese").Importo.sum().rename("uscite").to_frame()
         .to_excel(writer, sheet_name="6 mesi"))
        data["all"].to_excel(writer, sheet_name="transazioni", index=False)


def generate(frame, folder, title="Bilancio familiare"):
    """Scrive dashboard.html e dashboard.xlsx. Ritorna i due percorsi."""
    data = build(frame)
    page = folder / "dashboard.html"
    book = folder / "dashboard.xlsx"
    page.write_text(render(data, title), encoding="utf-8")
    write_excel(data, book)
    return page, book


if __name__ == "__main__":
    import sys
    from pathlib import Path

    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    table_frame = pd.read_csv(target / "consolidato.csv", sep=";",
                              encoding="utf-8-sig")
    for written in generate(table_frame, target):
        print(f"scritto {written}")
