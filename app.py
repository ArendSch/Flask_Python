import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pdfplumber
from flask import Flask, abort, render_template, request
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # max 32 MB upload

# Hier worden de uitgelezen gegevens per document als JSON bewaard,
# zodat ze later vergeleken kunnen worden met opnameformulier en calculatie.
DATA_DIR = Path(__file__).parent / "data"
DOC_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# ---------------------------------------------------------------------------
# Totaalprijs
# ---------------------------------------------------------------------------

# Labels die op een totaalprijs wijzen, in volgorde van voorkeur.
# Het eerste label dat in de PDF voorkomt wint; bij meerdere treffers telt de laatste.
TOTAL_LABELS = [
    r"Angebotspreis",
    r"Gesamtsumme",
    r"Gesamtbetrag",
    r"Gesamtpreis\s+netto",
    r"Totaalprijs",
    r"Totaalbedrag",
    r"Totaal\s+excl\.?\s*BTW",
    r"Totaal",
    r"Grand\s+total",
    r"Total\s+amount",
    r"Total",
    r"Summe",
]

# Europees bedrag, bv. 250.522,35 of 1.234,00 (eventueel met € ervoor of erna)
AMOUNT = r"€?\s*(-?\d{1,3}(?:\.\d{3})*,\d{2})\s*€?"

# ---------------------------------------------------------------------------
# Stalafmetingen (overzicht)
# ---------------------------------------------------------------------------

# Per maat een lijst met labels (Duits en Nederlands). De eerste regel in de PDF die matcht wint.
NUM = r"(\d+(?:[.,]\d+)?)\s*m\b"
DIMENSIONS = {
    "Lengte": [r"^-?\s*Länge\s*:", r"Stalllänge(?:\s+innen)?\s*:?", r"^-?\s*Lengte\s*:?"],
    "Breedte": [r"^-?\s*Breite\s*:", r"Stallbreite(?:\s+innen)?(?:\s+im\s+Tierbereich)?\s*:?", r"^-?\s*Breedte\s*:?"],
    "Goothoogte": [r"Traufe?n?höhe\s*:?", r"Goothoogte\s*:?"],
    "Nokhoogte": [r"Firsthöhe\s*:?", r"Nokhoogte\s*:?"],
}

# ---------------------------------------------------------------------------
# Technische projectgegevens (met bron en pagina)
# ---------------------------------------------------------------------------

# Per veld één vaste rekenregel, zodat vergelijkingen niet onopgemerkt fout gaan:
#   type "geheel":   alleen hele getallen; een punt is een duizendtal (31.723 = 31723).
#                    Een komma of decimalen geven een waarschuwing en geen getal.
#   type "decimaal": één punt of komma is het decimaalteken (1.89 m = 1,89 m = 1.89).
#                    Duizendtal plus decimaal mag ook (1.234,50 of 1,234.50).
#   eenheid:         standaardeenheid van het veld. Andere eenheden worden omgerekend
#                    (mm/cm -> m, kV -> V). Een onbekende eenheid geeft een waarschuwing.
#   multi=True:      meerdere waarden op één regel worden afzonderlijk opgeslagen.
TECH_FIELDS = [
    {"key": "spanning", "groep": "Elektrische voorzieningen", "label": "Spanning",
     "bron_label": "Spannung", "type": "decimaal", "eenheid": "V", "patterns": [r"Spannung"]},
    {"key": "frequentie", "groep": "Elektrische voorzieningen", "label": "Frequentie",
     "bron_label": "Frequenz", "type": "decimaal", "eenheid": "Hz", "patterns": [r"Frequenz"]},
    {"key": "fasen", "groep": "Elektrische voorzieningen", "label": "Aantal fasen",
     "bron_label": "Phasen", "type": "geheel", "eenheid": "", "patterns": [r"Phasen"]},
    {"key": "lengte_incl_eindsets", "groep": "Afmetingen stalinrichting", "label": "Lengte inclusief eindsets",
     "bron_label": "Anlagenlänge inkl. Endsätze", "type": "decimaal", "eenheid": "m",
     "patterns": [r"Anlagenlänge\s+inkl\.?\s+Endsätze"]},
    {"key": "gangbreedtes", "groep": "Afmetingen stalinrichting", "label": "Gangbreedtes",
     "bron_label": "Gangbreiten", "type": "decimaal", "eenheid": "m", "multi": True,
     "patterns": [r"Gangbreiten?"]},
    {"key": "ruimte_voor_eindset", "groep": "Afmetingen stalinrichting", "label": "Ruimte vóór eindset",
     "bron_label": "Platz vor Endsatz", "type": "decimaal", "eenheid": "m",
     "patterns": [r"Platz\s+vor\s+(?:dem\s+)?Endsatz"]},
    {"key": "ruimte_achter_eindset", "groep": "Afmetingen stalinrichting", "label": "Ruimte achter eindset",
     "bron_label": "Platz hinter Endsatz", "type": "decimaal", "eenheid": "m",
     "patterns": [r"Platz\s+hinter\s+(?:dem\s+)?Endsatz"]},
    {"key": "aantal_dieren", "groep": "Stalgegevens", "label": "Aantal dieren per stal",
     "bron_label": "Anzahl Tiere pro Stall", "type": "geheel", "eenheid": "",
     "patterns": [r"Anzahl\s+Tiere\s+pro\s+Stall"]},
    {"key": "goothoogte", "groep": "Stalgegevens", "label": "Goothoogte",
     "bron_label": "Traufenhöhe", "type": "decimaal", "eenheid": "m", "patterns": [r"Traufe?n?höhe"]},
    {"key": "wintergarden", "groep": "Stalgegevens", "label": "Afmetingen wintergarden",
     "bron_label": "Kaltscharrraum – Abmessungen", "type": "decimaal", "eenheid": "m",
     "special": "wintergarden"},
]
FIELDS_BY_KEY = {f["key"]: f for f in TECH_FIELDS}

# Toegestane eenheden per standaardeenheid, met omrekenfactor naar die standaardeenheid
UNIT_FACTORS = {
    "m": {"m": 1, "cm": 0.01, "mm": 0.001},
    "V": {"v": 1, "volt": 1, "kv": 1000},
    "Hz": {"hz": 1},
    "": {"": 1, "st": 1, "stk": 1, "stück": 1, "stuks": 1, "ph": 1, "phasen": 1, "fasen": 1},
}

VALUE_WITH_UNIT = r"-?\d[\d.,]*\s*[A-Za-zµ°%²³/]*"
MULTI_SPLIT = re.compile(r"\s*(?:;|\bund\b|&|/|,\s+(?=\d))\s*")


def parse_eu_amount(text: str) -> Decimal:
    return Decimal(text.replace(".", "").replace(",", "."))


def format_eu_amount(value: Decimal) -> str:
    s = f"{value:,.2f}"  # 250,522.35
    return "€ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def extract_pages(file_stream) -> list:
    """Tekst per pagina (index 0 = pagina 1)."""
    with pdfplumber.open(file_stream) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def find_total(text: str):
    """Geeft (label, bedrag, regel) terug, of None als er niets gevonden is."""
    for label in TOTAL_LABELS:
        pattern = re.compile(rf"^.*\b({label})\b.*?{AMOUNT}\s*$", re.IGNORECASE | re.MULTILINE)
        matches = list(pattern.finditer(text))
        if matches:
            m = matches[-1]  # laatste treffer = meestal het eindtotaal
            return m.group(1), parse_eu_amount(m.group(2)), m.group(0).strip()
    return None


def find_dimensions(text: str) -> dict:
    """Geeft {naam: '105,00 m'} terug; ontbrekende maten krijgen None."""
    result = {}
    for name, labels in DIMENSIONS.items():
        result[name] = None
        for label in labels:
            m = re.search(rf"{label}\s*{NUM}", text, re.IGNORECASE | re.MULTILINE)
            if m:
                result[name] = f"{m.group(1).replace('.', ',')} m"
                break
    return result


def _m(value: str) -> str:
    return f"{value.replace('.', ',')} m"


def _find_wintergarden_raw(text: str) -> list:
    """Wintergarden per zijde als [(zijde, lengte, breedte, bronregel)] met originele getallen."""
    # Vorm 1: blok "Kaltscharrraum - Abmessungen:" met "- Links: 105,00 m x 3,75 m"
    block = re.search(
        r"(?:Kaltscharr+aum|Wintergarten|Wintergarden|Wintertuin)[^\n]*Abmessungen[^\n]*\n((?:\s*-[^\n]*\n?)+)",
        text,
        re.IGNORECASE,
    )
    if block:
        sides = list(re.finditer(rf"-\s*(\w+)\s*:\s*{NUM}\s*x\s*{NUM}", block.group(1)))
        if sides:
            return [(s.group(1), s.group(2), s.group(3), s.group(0).strip()) for s in sides]

    # Vorm 2: losse regels "Kaltscharraumlänge 105,00 m 105,00 m" / "...breite 3,75 m 3,75 m"
    lengths = re.search(rf"(?:Kaltscharr+aum|Wintergarten)länge\s+{NUM}(?:\s+{NUM})?", text, re.IGNORECASE)
    widths = re.search(rf"(?:Kaltscharr+aum|Wintergarten)breite\s+{NUM}(?:\s+{NUM})?", text, re.IGNORECASE)
    result = []
    if lengths or widths:
        for i, zijde in ((1, "Links"), (2, "Rechts")):
            l = lengths.group(i) if lengths else None
            b = widths.group(i) if widths else None
            if l or b:
                bron = " | ".join(m.group(0) for m in (lengths, widths) if m)
                result.append((zijde, l, b, bron))
    return result


def find_wintergarden(text: str) -> list:
    """Wintergarden (Kaltscharrraum) per zijde: [{'zijde', 'lengte', 'breedte'}]."""
    return [
        {
            "zijde": zijde,
            "lengte": _m(l) if l else "niet gevonden",
            "breedte": _m(b) if b else "niet gevonden",
        }
        for zijde, l, b, _ in _find_wintergarden_raw(text)
    ]


def _parse_digits(num: str, soort: str):
    """Zet de getaltekst om volgens de vaste regel van het veld; None als het niet eenduidig is."""
    sign = -1 if num.startswith("-") else 1
    num = num.lstrip("-")
    if soort == "geheel":
        if re.fullmatch(r"\d+|\d{1,3}(?:\.\d{3})+", num):
            return sign * int(num.replace(".", ""))
        return None
    # decimaal
    if re.fullmatch(r"\d+(?:[.,]\d+)?", num):                # 1.89 / 1,89 / 105
        return sign * float(num.replace(",", "."))
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", num):        # 1.234,50
        return sign * float(num.replace(".", "").replace(",", "."))
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", num):        # 1,234.50
        return sign * float(num.replace(",", ""))
    return None


def parse_number(original, field: dict):
    """Geeft (getal in standaardeenheid, standaardeenheid, waarschuwing) terug.

    Voorbeelden: '1.89 m' -> (1.89, 'm', None); '31.723' (geheel) -> (31723, '', None);
    '1890 mm' -> (1.89, 'm', None); '1,5' (geheel) -> (None, '', 'waarschuwing').
    """
    eenheid = field["eenheid"]
    if not original:
        return None, eenheid, None
    m = re.match(r"^\s*(-?\d[\d.,]*)\s*(.*?)\s*$", original)
    if not m:
        return None, eenheid, f"'{original}' is geen getal"

    num, unit = m.group(1), m.group(2)
    value = _parse_digits(num, field["type"])
    if value is None:
        regel = "geen geheel getal" if field["type"] == "geheel" else "geen eenduidig decimaal getal"
        return None, eenheid, f"'{num}' is {regel}"

    factor = UNIT_FACTORS[eenheid].get(unit.lower())
    if factor is None:
        return None, eenheid, f"Onbekende eenheid '{unit}' (verwacht: {eenheid or 'geen eenheid'})"

    value = value * factor
    if field["type"] == "decimaal":
        value = round(value, 6)
    return value, eenheid, None


def _value(original, pagina, bronregel, field: dict, sublabel=None) -> dict:
    getal, eenheid, waarschuwing = parse_number(original, field)
    return {
        "sublabel": sublabel,
        "origineel": original,        # exact zoals in de offerte (met eenheid)
        "getal": getal,               # omgezet volgens de vaste regel van het veld
        "eenheid": eenheid,           # standaardeenheid van het veld
        "waarschuwing": waarschuwing, # gevuld als de omzetting niet eenduidig was
        "pagina": pagina,
        "bronregel": bronregel,
    }


def _extract_wintergarden_field(field: dict, pages: list) -> list:
    for nr, text in enumerate(pages, start=1):
        rows = _find_wintergarden_raw(text)
        if rows:
            values = []
            for zijde, l, b, bron in rows:
                values.append(_value(f"{l} m" if l else None, nr, bron, field, f"{zijde} – lengte"))
                values.append(_value(f"{b} m" if b else None, nr, bron, field, f"{zijde} – breedte"))
            return values
    return []


def _extract_line_field(field: dict, pages: list) -> list:
    values, seen = [], set()
    # 1e keus: "- Label: waarde" (projectsamenvatting); 2e keus: "Label waarde" (tabellen)
    for form in ("colon", "plain"):
        for nr, text in enumerate(pages, start=1):
            for pat in field["patterns"]:
                if form == "colon":
                    regex = rf"^\s*-?\s*{pat}\s*:\s*(.+?)\s*$"
                else:
                    regex = rf"^\s*-?\s*{pat}\s+({VALUE_WITH_UNIT})"
                for m in re.finditer(regex, text, re.IGNORECASE | re.MULTILINE):
                    raw = m.group(1).strip()
                    parts = MULTI_SPLIT.split(raw) if field.get("multi") else [raw]
                    for part in filter(None, (p.strip() for p in parts)):
                        if part in seen:
                            continue
                        seen.add(part)
                        values.append(_value(part, nr, m.group(0).strip(), field))
        if values:
            break
    if field.get("multi") and len(values) > 1:
        for i, v in enumerate(values, start=1):
            v["sublabel"] = f"Gang {i}"
    return values


def extract_tech_data(pages: list) -> list:
    """Alle technische projectgegevens met originele waarde, eenheid, pagina en bronregel."""
    result = []
    for field in TECH_FIELDS:
        if field.get("special") == "wintergarden":
            values = _extract_wintergarden_field(field, pages)
        else:
            values = _extract_line_field(field, pages)
        result.append({
            "key": field["key"],
            "groep": field["groep"],
            "label": field["label"],
            "bron_label": field["bron_label"],
            "gevonden": bool(values),
            # Niets gevonden: één lege waarde, die als 'Niet gevonden' wordt getoond
            "type": field["type"],
            "eenheid": field["eenheid"],
            "waarden": values or [_value(None, None, None, field)],
        })
    return result


# ---------------------------------------------------------------------------
# Opslag
# ---------------------------------------------------------------------------

def _doc_path(doc_id: str) -> Path:
    if not DOC_ID_RE.match(doc_id):
        abort(404)
    return DATA_DIR / f"{doc_id}.json"


def save_document(doc: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    _doc_path(doc["document_id"]).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _render(doc=None, error=None):
    s = (doc or {}).get("samenvatting", {})
    return render_template(
        "index.html",
        result=s.get("totaalprijs"),
        error=error,
        filename=(doc or {}).get("document"),
        dimensions=s.get("stalafmetingen"),
        wintergarden=s.get("wintergarden"),
        doc=doc,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return _render()

    file = request.files.get("pdf")
    if not file or file.filename == "":
        return _render(error="Kies eerst een PDF-bestand.")
    if not file.filename.lower().endswith(".pdf"):
        return _render(error="Alleen PDF-bestanden zijn toegestaan.")

    try:
        pages = extract_pages(file.stream)
    except Exception as exc:  # beschadigde of onleesbare PDF
        return _render(error=f"Kon de PDF niet lezen: {exc}")

    text = "\n".join(pages)
    found = find_total(text)
    totaal = None
    if found:
        label, amount, line = found
        totaal = {"label": label, "amount": format_eu_amount(amount), "line": line,
                  "bedrag": float(amount)}

    stem = secure_filename(Path(file.filename).stem) or "offerte"
    doc = {
        "document_id": f"{datetime.now():%Y%m%d-%H%M%S}-{stem}",
        "document": file.filename,
        "soort": "leveranciersofferte",
        "uitgelezen_op": datetime.now().isoformat(timespec="seconds"),
        "samenvatting": {
            "totaalprijs": totaal,
            "stalafmetingen": find_dimensions(text),
            "wintergarden": find_wintergarden(text),
        },
        "technische_gegevens": extract_tech_data(pages),
    }
    save_document(doc)
    return _render(doc, error=None if totaal else "Geen totaalprijs gevonden in deze PDF.")


if __name__ == "__main__":
    app.run(debug=True)
