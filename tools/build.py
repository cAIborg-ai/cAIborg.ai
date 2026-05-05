"""Build Universe Vitamins product pages from JSON data + HTML template — MULTILANG.

Usage: .venv/bin/python3 tools/build.py

Reads:
  - assets/data/products.<lang>.json   for each lang in (en, ru, kz, tr)
  - assets/templates/product.html       single template

Writes for each VALID lang JSON:
  - universe_product_<slug><url_suffix>.html  (e.g. universe_product_omega.html for EN, _ru/_kz/_tr for others)

If a lang JSON is invalid/missing, that lang is skipped (warning), but lang-switcher links still appear on built pages.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "assets" / "data"
TPL = REPO / "assets" / "templates" / "product.html"

LANGS = [
    {"code": "en", "label": "EN", "suffix": ""},
    {"code": "ru", "label": "RU", "suffix": "_ru"},
    {"code": "kz", "label": "KZ", "suffix": "_kz"},
    {"code": "tr", "label": "TR", "suffix": "_tr"},
]

BADGE_COLORS = {
    "green": "bg-green-50 border-green-200 text-green-800",
    "blue": "bg-blue-50 border-blue-200 text-blue-800",
    "purple": "bg-purple-50 border-purple-200 text-purple-800",
    "yellow": "bg-yellow-50 border-yellow-200 text-yellow-800",
    "pink": "bg-pink-50 border-pink-200 text-pink-800",
}

CHECK_SVG = '<svg class="w-4 h-4 text-brand-green mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>'


# ============================================================================
# Block renderers (lang-aware where needed)
# ============================================================================

def render_badges(badges):
    out = []
    for b in badges:
        cls = BADGE_COLORS.get(b["color"], BADGE_COLORS["green"])
        out.append(f'<span class="inline-flex items-center gap-1 {cls} border px-3 py-1 rounded-full text-xs font-medium">{b["label"]}</span>')
    return "\n                        ".join(out)


def render_lab_callout(lab):
    if not lab:
        return ""
    return f'''<div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-6">
                        <p class="text-sm font-semibold text-blue-900">
                            ✓ {lab["title"]}
                        </p>
                        <p class="text-xs text-blue-700 mt-1">
                            {lab["subtitle"]}
                        </p>
                    </div>'''


def render_pkg_box(pkg):
    lines = "\n                            ".join(
        f'<p class="text-sm text-gray-600">{ln}</p>' for ln in pkg["lines"]
    )
    return f'''<div class="border-2 border-brand-green rounded-lg p-4 mb-4">
                        <div class="text-center">
                            <p class="text-brand-green font-semibold mb-1">{pkg["title"]}</p>
                            {lines}
                        </div>
                    </div>'''


def render_key_benefits(items):
    out = [f'<li class="flex items-start gap-2">{CHECK_SVG}<span>{it}</span></li>' for it in items]
    return "\n                            ".join(out)


def render_narrative_paragraphs(paragraphs):
    return "\n                    ".join(
        f'<p class="text-gray-700 mb-4 leading-relaxed">{p}</p>' for p in paragraphs
    )


def render_why_works(cards):
    out = []
    for c in cards:
        bullets = "\n                                ".join(f'<li>• {b}</li>' for b in c["bullets"])
        out.append(f'''<div class="bg-gray-50 p-6 rounded-lg">
                            <h3 class="text-lg font-semibold text-brand-dark mb-3">{c["title"]}</h3>
                            <ul class="text-sm text-gray-600 space-y-2">
                                {bullets}
                            </ul>
                            <p class="text-xs text-gray-500 mt-4 italic">
                                {c["footnote"]}
                            </p>
                        </div>''')
    return "\n\n                        ".join(out)


def render_sf_rows(rows):
    indent_class = ["", "pl-4", "pl-8"]
    out = []
    last = len(rows) - 1
    for i, r in enumerate(rows):
        ind = indent_class[r.get("indent", 0)] if r.get("indent", 0) < 3 else "pl-8"
        border = "border-b-4 border-black" if i == last else "border-b border-gray-200"
        td_cls = f'py-2 {ind}' if ind else 'py-2'
        out.append(f'''<tr class="{border}">
                            <td class="{td_cls}">{r["name"]}</td>
                            <td class="text-right py-2 font-medium">{r["amount"]}</td>
                            <td class="text-right py-2">{r["rda"]}</td>
                        </tr>''')
    return "\n                        ".join(out)


def render_supplement_facts(sf, ui):
    rows_html = render_sf_rows(sf["rows"])
    lab_box = ""
    if sf.get("lab_box"):
        lb = sf["lab_box"]
        lab_box = f'''<!-- Lab Verification Box -->
                    <div class="p-4 bg-blue-50 rounded-lg border border-blue-200">
                        <p class="text-sm font-semibold text-blue-900 mb-1">✓ Independent Lab Verification:</p>
                        <p class="text-sm text-blue-700">
                            Label claim: {lb["claim"]}<br>
                            <strong>Actual tested potency (RAN lab): {lb["actual"]}</strong><br>
                            Product exceeds claimed concentration by {lb["percent"]}%
                        </p>
                    </div>'''
    extra = sf.get("caution_extra", "")
    extra_p = f'<p class="mt-2">{extra}</p>' if extra else ""
    return f'''<table class="w-full text-sm mb-6 border-collapse">
                    <caption class="text-left font-bold mb-4 text-lg text-brand-dark">{ui.get("section_supplement_facts", "Supplement Facts")}</caption>
                    <tbody>
                        <tr class="border-b border-gray-300">
                            <th class="text-left py-2 font-semibold">Serving Size:</th>
                            <td class="text-right py-2">{sf["serving_size"]}</td>
                        </tr>
                        <tr class="border-b-4 border-black">
                            <th class="text-left py-2 font-semibold">Servings Per Container:</th>
                            <td class="text-right py-2">{sf["servings_per_container"]}</td>
                        </tr>
                    </tbody>
                </table>

                <table class="w-full text-sm">
                    <thead>
                        <tr class="border-b-2 border-black">
                            <th class="text-left py-2 font-semibold">Active Ingredient</th>
                            <th class="text-right py-2 font-semibold">Amount per Serving</th>
                            <th class="text-right py-2 font-semibold">% RDA</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                    <tfoot>
                        <tr>
                            <td colspan="3" class="text-xs pt-2 text-gray-600">† Daily Value not established.</td>
                        </tr>
                    </tfoot>
                </table>

                <div class="mt-6 space-y-4">
                    <div class="text-sm text-gray-700">
                        <p><strong>Other Ingredients:</strong> {sf["other_ingredients"]}</p>
                    </div>

                    {lab_box}

                    <div class="text-sm text-gray-600">
                        <p><strong>Caution:</strong> {sf["caution"]}</p>
                        {extra_p}
                    </div>
                </div>'''


def render_health_benefits(cards):
    out = []
    for c in cards:
        bullets = "\n                        ".join(f'<li>• {b}</li>' for b in c["bullets"])
        out.append(f'''<div class="bg-white p-6 rounded-lg">
                    <h3 class="text-lg font-semibold text-brand-dark mb-3">{c["title"]}</h3>
                    <ul class="text-sm text-gray-600 space-y-1">
                        {bullets}
                    </ul>
                </div>''')
    return "\n\n                ".join(out)


def render_usage(usage):
    return f'''<div class="bg-gray-50 p-6 rounded-lg">
                    <h3 class="font-semibold text-brand-dark mb-4">{usage.get("h3", "How to Take:")}</h3>
                    <div class="space-y-4 text-sm text-gray-700">
                        <div>
                            <p class="font-medium text-brand-dark mb-1">Recommended Dosage:</p>
                            <p>{usage["dosage"]}</p>
                        </div>
                        <div>
                            <p class="font-medium text-brand-dark mb-1">Best Time to Take:</p>
                            <p>{usage["time"]}</p>
                        </div>
                        <div>
                            <p class="font-medium text-brand-dark mb-1">Course Duration:</p>
                            <p>{usage["course"]}</p>
                        </div>
                        <div class="p-4 bg-blue-50 rounded border border-blue-200 mt-4">
                            <p class="text-xs text-blue-900">
                                <strong>Note:</strong> {usage["note"]}
                            </p>
                        </div>
                    </div>
                </div>'''


def render_storage(storage, caution):
    s_bullets = "\n                            ".join(f'<li>• {b}</li>' for b in storage["bullets"])
    c_bullets = "\n                            ".join(f'<li>• {b}</li>' for b in caution["bullets"])
    return f'''<div class="bg-white p-6 rounded-lg">
                        <h3 class="font-semibold text-brand-dark mb-3">Storage Instructions:</h3>
                        <ul class="text-sm text-gray-700 space-y-2">
                            {s_bullets}
                        </ul>
                        <p class="text-xs text-gray-500 mt-4 italic">
                            <strong>Shelf Life:</strong> {storage["shelf_life"]}
                        </p>
                    </div>

                    <div class="bg-white p-6 rounded-lg">
                        <h3 class="font-semibold text-brand-dark mb-3">Caution &amp; Contraindications:</h3>
                        <ul class="text-sm text-gray-700 space-y-2">
                            {c_bullets}
                        </ul>
                        <p class="text-xs text-gray-500 mt-4 italic">
                            <strong>Note:</strong> {caution["note"]}
                        </p>
                    </div>'''


def render_pkg_info(p):
    s = p["specs"]
    d = p["dimensions"]
    m = p["manufacturing"]
    return f'''<div class="bg-gray-50 p-6 rounded-lg">
                        <h3 class="font-semibold text-brand-dark mb-3">Product Specifications:</h3>
                        <div class="text-sm text-gray-700 space-y-2">
                            <p><strong>Quantity:</strong> {s["quantity"]}</p>
                            <p><strong>Servings:</strong> {s["servings"]}</p>
                            <p><strong>Supply Duration:</strong> {s["supply"]}</p>
                            <p><strong>{s.get("extra_label", "Net Weight")}:</strong> {s["extra"]}</p>
                        </div>
                    </div>

                    <div class="bg-gray-50 p-6 rounded-lg">
                        <h3 class="font-semibold text-brand-dark mb-3">Package Dimensions:</h3>
                        <div class="text-sm text-gray-700 space-y-2">
                            <p><strong>Length:</strong> {d["l"]}</p>
                            <p><strong>Width:</strong> {d["w"]}</p>
                            <p><strong>Height:</strong> {d["h"]}</p>
                            <p class="text-xs text-gray-500 italic mt-2">{d["caption"]}</p>
                        </div>
                    </div>

                    <div class="bg-gray-50 p-6 rounded-lg">
                        <h3 class="font-semibold text-brand-dark mb-3">Manufacturing:</h3>
                        <div class="text-sm text-gray-700 space-y-2">
                            <p><strong>Country:</strong> {m["country"]}</p>
                            <p><strong>Location:</strong> {m["location"]}</p>
                            <p><strong>Certification:</strong> {m["cert"]}</p>
                            <p class="text-xs text-gray-500 italic mt-2">{m["note"]}</p>
                        </div>
                    </div>'''


def render_whats_inside(cards):
    out = []
    for c in cards:
        out.append(f'''<div class="bg-gray-50 p-6 rounded-lg">
                    <div class="w-12 h-12 rounded-full bg-brand-green bg-opacity-10 flex items-center justify-center mb-4">
                        <svg class="w-6 h-6 text-brand-green" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="{c.get("icon_path", "M13 10V3L4 14h7v7l9-11h-7z")}"></path>
                        </svg>
                    </div>
                    <h3 class="text-lg font-semibold text-brand-dark mb-2">{c["title"]}</h3>
                    <p class="text-sm text-gray-600 leading-relaxed">{c["desc"]}</p>
                </div>''')
    return "\n\n                ".join(out)


def render_certifications(certs):
    out = []
    for c in certs:
        out.append(f'''<div class="flex flex-col items-center">
                        <div class="w-16 h-16 bg-gray-100 rounded-lg flex items-center justify-center mb-2">
                            <span class="text-xs text-gray-500">{c}</span>
                        </div>
                        <p class="text-xs text-gray-600 text-center">{c}</p>
                    </div>''')
    return "\n                    ".join(out)


def render_who_needs(suitable, priority, suitable_title, priority_title):
    s_html = "\n                        ".join(f'<li>✓ {x}</li>' for x in suitable)
    p_html = "\n                        ".join(f'<li>• {x}</li>' for x in priority)
    return f'''<div class="bg-white p-6 rounded-lg">
                    <h3 class="font-semibold text-brand-dark mb-3">{suitable_title}</h3>
                    <ul class="text-sm text-gray-600 space-y-2">
                        {s_html}
                    </ul>
                </div>

                <div class="bg-white p-6 rounded-lg">
                    <h3 class="font-semibold text-brand-dark mb-3">{priority_title}</h3>
                    <ul class="text-sm text-gray-600 space-y-2">
                        {p_html}
                    </ul>
                </div>'''


def render_pdf(pdf):
    if not pdf or not pdf.get("link"):
        return '<p class="text-sm text-gray-500 italic">Product datasheet coming soon.</p>'
    return f'''<a href="{pdf["link"]}" target="_blank" rel="noopener noreferrer" class="flex items-center gap-4 p-4 bg-gray-50 rounded-lg hover:bg-gray-100 transition group">
                            <div class="w-12 h-12 rounded-lg bg-brand-green bg-opacity-10 flex items-center justify-center flex-shrink-0">
                                <svg class="w-6 h-6 text-brand-green" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
                                </svg>
                            </div>
                            <div class="flex-1 min-w-0">
                                <h4 class="font-semibold text-brand-dark group-hover:text-brand-green transition">{pdf["title"]}</h4>
                                <p class="text-xs text-gray-500">{pdf["subtitle"]}</p>
                            </div>
                            <svg class="w-5 h-5 text-gray-400 group-hover:text-brand-green transition flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path>
                            </svg>
                        </a>'''


def render_shop_more(cards, lang_suffix, shop_now_label):
    out = []
    for c in cards:
        out.append(f'''<a href="universe_products{lang_suffix}.html" class="block bg-white p-8 rounded-lg border border-gray-200 hover:border-brand-green transition group">
                    <h3 class="text-xl font-semibold text-brand-dark mb-2 group-hover:text-brand-green transition">{c["title"]}</h3>
                    <p class="text-sm text-gray-600 mb-4">{c["desc"]}</p>
                    <span class="text-brand-green font-medium text-sm">{shop_now_label}</span>
                </a>''')
    return "\n                ".join(out)


def render_lang_switcher(slug, current_code):
    """4 anchors EN | RU | KZ | TR. Active highlighted. Each links to same product in other lang."""
    parts = []
    for i, lang in enumerate(LANGS):
        href = f"universe_product_{slug}{lang['suffix']}.html"
        if lang["code"] == current_code:
            parts.append(f'<a href="{href}" class="text-brand-green font-medium">{lang["label"]}</a>')
        else:
            parts.append(f'<a href="{href}" class="text-gray-600 hover:text-brand-green transition">{lang["label"]}</a>')
    sep = '<span class="text-gray-400">|</span>'
    return f'\n                        {sep.join("\\n                        " + p + "\\n                        " for p in parts)}'.replace("\\n", "\n").replace(sep, sep)  # avoid messy


def render_lang_switcher_clean(slug, current_code):
    """Clean impl: build 4 anchors with `|` separators."""
    items = []
    for lang in LANGS:
        href = f"universe_product_{slug}{lang['suffix']}.html"
        if lang["code"] == current_code:
            items.append(f'<a href="{href}" class="text-brand-green font-medium">{lang["label"]}</a>')
        else:
            items.append(f'<a href="{href}" class="text-gray-600 hover:text-brand-green transition">{lang["label"]}</a>')
    sep = '\n                        <span class="text-gray-400">|</span>\n                        '
    return sep.join(items)


# ============================================================================
# Main render
# ============================================================================

def render_product(slug, p, ui, contact, lang_suffix, lang_code, template):
    """Substitute all placeholders for one product in one lang."""
    repl = {
        "TITLE_SEO": p["title"],
        "SLUG": slug,
        "LANG_SUFFIX": lang_suffix,
        "LANG_SWITCHER": render_lang_switcher_clean(slug, lang_code),
        "NAME_SHORT": p["name_short"],
        "NAME_FULL_HTML": p["name_full_html"],
        "TAGLINE": p["tagline"],
        "DESCRIPTION": p["description"],
        "BADGE_TOP": p["badge_top"],
        "IMG_FRONT": p["img_front"],
        "IMG_RIGHT": p.get("img_right", p["img_front"]),
        "IMG_LEFT": p.get("img_left", p["img_front"]),
        "IMG_ALT": p["img_alt"],
        "BADGES_HTML": render_badges(p["badges"]),
        "LAB_CALLOUT_HTML": render_lab_callout(p.get("lab_callout")),
        "PKG_BOX_HTML": render_pkg_box(p["pkg_box"]),
        "KEY_BENEFITS_HTML": render_key_benefits(p["key_benefits"]),
        "NARRATIVE_H2": p["narrative_h2"],
        "NARRATIVE_PARAGRAPHS_HTML": render_narrative_paragraphs(p["narrative_paragraphs"]),
        "WHY_WORKS_HTML": render_why_works(p["why_works"]),
        "SF_HTML": render_supplement_facts(p["supplement_facts"], ui),
        "HB_HTML": render_health_benefits(p["health_benefits"]),
        "USAGE_HTML": render_usage(p["usage"]),
        "STORAGE_CAUTION_HTML": render_storage(p["storage"], p["caution"]),
        "PKG_INFO_HTML": render_pkg_info(p["pkg_info"]),
        "WHATS_INSIDE_INTRO": p["whats_inside_intro"],
        "WHATS_INSIDE_HTML": render_whats_inside(p["whats_inside"]),
        "CERTS_HTML": render_certifications(p["certifications"]),
        "WHO_SUITABLE_TITLE": p.get("who_suitable_title", "Suitable for:"),
        "WHO_NEEDS_HTML": render_who_needs(p["who_suitable"], p["who_priority"], p.get("who_suitable_title", "Suitable for:"), ui.get("high_priority_label", "High priority for:")),
        "PDF_HTML": render_pdf(p.get("pdf")),
        "SHOP_MORE_HTML": render_shop_more(p["shop_more"], lang_suffix, ui.get("shop_now", "Shop Now →")),
    }
    # UI placeholders: {{UI_<KEY_UPPER>}} from _ui dict
    for k, v in ui.items():
        if isinstance(v, str):
            repl[f"UI_{k.upper()}"] = v
    # CONTACT placeholders: {{CONTACT_<KEY_UPPER>}} from _contact dict
    for k, v in contact.items():
        repl[f"CONTACT_{k.upper()}"] = v

    out = template
    for key, val in repl.items():
        out = out.replace("{{" + key + "}}", val)
    return out


def main():
    template = TPL.read_text(encoding="utf-8")
    total = 0
    for lang in LANGS:
        json_path = DATA_DIR / f"products.{lang['code']}.json"
        if not json_path.exists():
            print(f"⚠ skip {lang['code']}: file missing")
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"⚠ skip {lang['code']}: invalid JSON ({e})")
            continue
        ui = data.get("_ui", {})
        contact = data.get("_contact", {})
        suffix = lang["suffix"]
        code = lang["code"]
        n = 0
        for slug, p in data.items():
            if slug.startswith("_"):
                continue
            html = render_product(slug, p, ui, contact, suffix, code, template)
            out = REPO / f"universe_product_{slug}{suffix}.html"
            out.write_text(html, encoding="utf-8")
            n += 1
            total += 1
        print(f"✓ {lang['code']}: {n} files")
    print(f"\nGenerated {total} files total.")


if __name__ == "__main__":
    main()
