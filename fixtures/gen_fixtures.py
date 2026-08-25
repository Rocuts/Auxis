#!/usr/bin/env python3
"""
Generador de fixtures para la prueba tecnica de AI Engineer.
Cinco PDFs con tablas de impuestos (US), deliberadamente heterogeneos.
Datos SINTETICOS con forma realista.
"""
import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, PageTemplate, Frame, Paragraph,
                                Spacer, Table, TableStyle, PageBreak, KeepTogether)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY

OUT = "/home/claude/fixtures"
os.makedirs(OUT, exist_ok=True)

INK = colors.HexColor("#111111")
RULE = colors.HexColor("#333333")
SOFT = colors.HexColor("#767676")
BAND = colors.HexColor("#EFEFEA")
NAVY = colors.HexColor("#1B2A4A")


def build(path, pagesize, story, footer_fn, meta, margins=(0.85, 0.85, 0.8, 0.8)):
    """margins = (left, right, top, bottom) en pulgadas"""
    l, r, t, b = margins
    doc = BaseDocTemplate(
        path, pagesize=pagesize,
        leftMargin=l * inch, rightMargin=r * inch,
        topMargin=t * inch, bottomMargin=b * inch,
        title=meta["title"], author=meta["author"],
        subject=meta["subject"], creator=meta.get("creator", "Document Composition Service"),
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=footer_fn)])
    doc.build(story)
    print("  ->", os.path.basename(path))


# ══════════════════════════════════════════════════════════════════════
# DOC 1 — Federal income tax brackets
# Estilo: serif institucional. Trampa: matriz ancha, 1 fila visual = 4 registros.
# ══════════════════════════════════════════════════════════════════════
def doc1():
    S = lambda n, **k: ParagraphStyle(n, **{**dict(fontName="Times-Roman", textColor=INK), **k})
    h_agency = S("ag", fontName="Times-Bold", fontSize=8.5, leading=11,
                 alignment=TA_CENTER, textColor=SOFT)
    h_title = S("ti", fontName="Times-Bold", fontSize=15, leading=19, alignment=TA_CENTER,
                spaceBefore=10, spaceAfter=2)
    h_sub = S("su", fontSize=10.5, leading=13, alignment=TA_CENTER, textColor=colors.HexColor("#444"))
    body = S("bd", fontSize=9.2, leading=12.6, alignment=TA_JUSTIFY, spaceAfter=6)
    note = S("nt", fontSize=7.6, leading=10, textColor=SOFT)
    caption = S("cp", fontName="Times-Bold", fontSize=9.5, leading=12, spaceBefore=12, spaceAfter=5)

    st = []
    st.append(Paragraph("DEPARTMENT OF REVENUE ANALYSIS &nbsp;&bull;&nbsp; OFFICE OF TAX POLICY", h_agency))
    st.append(Spacer(1, 3))
    st.append(Table([[""]], colWidths=[6.8 * inch], rowHeights=[1.6],
                    style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), RULE)])))
    st.append(Paragraph("Individual Income Tax Rate Schedules", h_title))
    st.append(Paragraph("Tax Year 2026 &mdash; Taxable Income Brackets by Filing Status", h_sub))
    st.append(Spacer(1, 14))

    st.append(Paragraph(
        "The schedules below set out the marginal rates applicable to ordinary taxable income for "
        "the 2026 tax year. Amounts shown are the bracket boundaries after the application of the "
        "standard deduction or itemized deductions, as applicable. Each rate applies only to the "
        "portion of taxable income falling within the stated bracket. Bracket boundaries are subject "
        "to annual inflation adjustment.", body))

    st.append(Paragraph("Table 1. &nbsp;Ordinary Income Rate Schedules, Tax Year 2026", caption))

    hdr1 = ["", Paragraph("<b>Taxable Income Bracket</b>", S("x", fontName="Times-Bold", fontSize=9,
                                                            alignment=TA_CENTER, textColor=colors.white)),
            "", "", ""]
    hdr2 = ["Rate", "Single", "Married Filing\nJointly", "Married Filing\nSeparately", "Head of\nHousehold"]

    rows = [
        ("10%", "$0 – $12,250", "$0 – $24,500", "$0 – $12,250", "$0 – $17,450"),
        ("12%", "$12,251 – $49,800", "$24,501 – $99,550", "$12,251 – $49,800", "$17,451 – $66,600"),
        ("22%", "$49,801 – $106,150", "$99,551 – $212,300", "$49,801 – $106,150", "$66,601 – $106,150"),
        ("24%", "$106,151 – $202,650", "$212,301 – $405,250", "$106,151 – $202,650", "$106,151 – $202,650"),
        ("32%", "$202,651 – $257,300", "$405,251 – $514,600", "$202,651 – $257,300", "$202,651 – $257,250"),
        ("35%", "$257,301 – $643,250", "$514,601 – $771,900", "$257,301 – $385,950", "$257,251 – $643,250"),
        ("37%", "$643,251 and over", "$771,901 and over", "$385,951 and over", "$643,251 and over"),
    ]

    data = [hdr1, hdr2] + [list(r) for r in rows]
    w = [0.62 * inch, 1.62 * inch, 1.62 * inch, 1.62 * inch, 1.42 * inch]
    t = Table(data, colWidths=w, repeatRows=2, hAlign="CENTER")
    t.setStyle(TableStyle([
        ("SPAN", (1, 0), (4, 0)),
        ("BACKGROUND", (0, 0), (-1, 1), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 1), colors.white),
        ("FONTNAME", (0, 0), (-1, 1), "Times-Bold"),
        ("FONTSIZE", (0, 0), (-1, 1), 8.6),
        ("LEADING", (0, 0), (-1, 1), 10.5),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 2), (-1, -1), "Times-Roman"),
        ("FONTSIZE", (0, 2), (-1, -1), 8.8),
        ("FONTNAME", (0, 2), (0, -1), "Times-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 5.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5.5),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA3B2")),
        ("LINEBELOW", (0, 1), (-1, 1), 1.1, NAVY),
        ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, colors.HexColor("#F4F5F8")]),
    ]))
    st.append(t)
    st.append(Spacer(1, 8))
    st.append(Paragraph(
        "Source: Office of Tax Policy, rate schedule release 2026-A. Bracket boundaries are stated in "
        "whole dollars. The uppermost bracket in each column is open-ended.", note))

    st.append(Spacer(1, 20))
    st.append(Paragraph("Table 2. &nbsp;Estates and Trusts &mdash; Ordinary Income", caption))
    t2 = Table([["Rate", "Taxable Income Bracket"],
                ["10%", "$0 – $3,250"],
                ["24%", "$3,251 – $11,750"],
                ["35%", "$11,751 – $16,050"],
                ["37%", "$16,051 and over"]],
               colWidths=[0.9 * inch, 2.6 * inch], hAlign="LEFT")
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DDE1E8")),
        ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Times-Roman"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.8),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#9AA3B2")),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
    ]))
    st.append(t2)

    def footer(c, d):
        c.saveState()
        c.setStrokeColor(colors.HexColor("#BBBBBB")); c.setLineWidth(0.4)
        c.line(d.leftMargin, 0.62 * inch, d.pagesize[0] - d.rightMargin, 0.62 * inch)
        c.setFont("Times-Italic", 7); c.setFillColor(SOFT)
        c.drawString(d.leftMargin, 0.46 * inch,
                     "Synthetic sample data prepared for systems evaluation. Not an authoritative tax publication.")
        c.setFont("Times-Roman", 7.5); c.setFillColor(INK)
        c.drawRightString(d.pagesize[0] - d.rightMargin, 0.46 * inch, "Pub. 5001-A (Rev. 11-2025)  |  Page %d" % d.page)
        c.restoreState()

    build(f"{OUT}/01_federal_income_tax_rate_schedules_TY2026.pdf", letter, st, footer,
          dict(title="Individual Income Tax Rate Schedules — Tax Year 2026",
               author="Office of Tax Policy", subject="Ordinary income marginal rate schedules"))


# ══════════════════════════════════════════════════════════════════════
# DOC 2 — Standard deduction
# Estilo: sans moderno. Trampas: montos SIN "$", regla en prosa (no tabular),
# notas al pie con marcador (a)(b), entidades de tipo distinto.
# ══════════════════════════════════════════════════════════════════════
def doc2():
    S = lambda n, **k: ParagraphStyle(n, **{**dict(fontName="Helvetica", textColor=INK), **k})
    kicker = S("kk", fontName="Helvetica-Bold", fontSize=7.4, leading=9,
               textColor=colors.HexColor("#8A6D1F"))
    h1 = S("h1", fontName="Helvetica-Bold", fontSize=16.5, leading=20, spaceBefore=6, spaceAfter=3)
    sub = S("sb", fontSize=10, leading=13, textColor=colors.HexColor("#555"))
    body = S("bd", fontSize=9.1, leading=13, spaceAfter=7)
    cap = S("cp", fontName="Helvetica-Bold", fontSize=9, leading=11, spaceBefore=15, spaceAfter=6,
            textColor=colors.HexColor("#333"))
    note = S("nt", fontSize=7.5, leading=10.5, textColor=SOFT, spaceBefore=5)

    st = []
    st.append(Paragraph("TAXPAYER SERVICES DIVISION &nbsp;/&nbsp; TECHNICAL BULLETIN TB-2025-14", kicker))
    st.append(Paragraph("Standard Deduction Schedule", h1))
    st.append(Paragraph("Issued 21 November 2025 &nbsp;&bull;&nbsp; effective for taxable years beginning on or after January 1, 2026", sub))
    st.append(Spacer(1, 4))
    st.append(Table([[""]], colWidths=[6.8 * inch], rowHeights=[2.2],
                    style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#C9A227"))])))
    st.append(Spacer(1, 12))

    st.append(Paragraph(
        "This bulletin states the basic standard deduction amounts and the additional amounts allowed "
        "to taxpayers who have attained age 65 before the close of the taxable year or who are blind. "
        "All amounts in this bulletin are expressed in United States dollars.", body))

    st.append(Paragraph("Section 1 &mdash; Basic standard deduction", cap))
    t1 = Table([
        ["Filing status", "Amount", "Prior year", "Change"],
        ["Single", "15,400", "15,000", "+400"],
        ["Married filing jointly", "30,800", "30,000", "+800"],
        ["Married filing separately", "15,400", "15,000", "+400"],
        ["Head of household", "23,100", "22,500", "+600"],
        ["Qualifying surviving spouse", "30,800", "30,000", "+800"],
    ], colWidths=[2.5 * inch, 1.25 * inch, 1.25 * inch, 1.0 * inch], hAlign="LEFT")
    t1.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.3, colors.HexColor("#DDDDDD")),
        ("LINEBELOW", (0, -1), (-1, -1), 0.9, INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 6),
    ]))
    st.append(t1)

    st.append(Paragraph("Section 2 &mdash; Additional standard deduction (age 65 or older, or blind)", cap))
    t2 = Table([
        ["Condition", "Per qualifying condition (a)"],
        ["Unmarried (single or head of household)", "2,050"],
        ["Married, per qualifying spouse", "1,650"],
    ], colWidths=[3.75 * inch, 2.25 * inch], hAlign="LEFT")
    t2.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.9),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, INK),
        ("LINEBELOW", (0, -1), (-1, -1), 0.9, INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 6),
    ]))
    st.append(t2)

    st.append(Paragraph("Section 3 &mdash; Dependents", cap))
    st.append(Paragraph(
        "The standard deduction allowed to an individual with respect to whom a deduction for a personal "
        "exemption is allowable to another taxpayer is limited to the <b>greater of</b> 1,400, "
        "<b>or</b> the individual's earned income for the taxable year plus 450. In no event may the amount "
        "so determined exceed the basic standard deduction otherwise applicable to the individual's filing "
        "status under Section 1 above. (b)", body))

    st.append(Spacer(1, 10))
    st.append(Paragraph(
        "(a) &nbsp;The additional amount is allowed once for age and once for blindness; a taxpayer meeting "
        "both conditions is allowed twice the stated amount.<br/>"
        "(b) &nbsp;Amounts in Section 3 are indexed and are restated each year in the annual inflation "
        "adjustment bulletin.", note))

    def footer(c, d):
        c.saveState()
        c.setFillColor(colors.HexColor("#F4F5F2"))
        c.rect(0, 0, d.pagesize[0], 0.62 * inch, stroke=0, fill=1)
        c.setFont("Helvetica", 6.8); c.setFillColor(SOFT)
        c.drawString(d.leftMargin, 0.32 * inch,
                     "SYNTHETIC SAMPLE DATA — prepared for systems evaluation. Not an authoritative tax publication.")
        c.setFont("Helvetica-Bold", 7); c.setFillColor(INK)
        c.drawRightString(d.pagesize[0] - d.rightMargin, 0.32 * inch, "TB-2025-14  ·  %d" % d.page)
        c.restoreState()

    build(f"{OUT}/02_standard_deduction_schedule_TY2026.pdf", letter, st, footer,
          dict(title="Standard Deduction Schedule — TB-2025-14",
               author="Taxpayer Services Division", subject="Basic and additional standard deduction amounts"))


# ══════════════════════════════════════════════════════════════════════
# DOC 3 — State & local sales tax
# Estilo: think-tank denso. Trampas: 51 filas en 2 paginas con header repetido
# y "(continued)", tasas SIN simbolo %, guion largo para "sin impuesto"
# (null vs cero), columna derivada (Combined = State + Local).
# ══════════════════════════════════════════════════════════════════════
def doc3():
    S = lambda n, **k: ParagraphStyle(n, **{**dict(fontName="Helvetica", textColor=INK), **k})
    h1 = S("h1", fontName="Helvetica-Bold", fontSize=13.5, leading=16, spaceAfter=2)
    sub = S("sb", fontSize=8.6, leading=11, textColor=colors.HexColor("#555"), spaceAfter=10)
    body = S("bd", fontSize=8.3, leading=11.4, alignment=TA_JUSTIFY, spaceAfter=6)
    cap = S("cp", fontName="Helvetica-Bold", fontSize=8.6, leading=11, spaceBefore=8, spaceAfter=5)
    note = S("nt", fontSize=7, leading=9.4, textColor=SOFT, spaceBefore=6)

    ROWS = [
        ("Alabama", "4.000", "5.290", "9.290"), ("Alaska", "—", "1.821", "1.821"),
        ("Arizona", "5.600", "2.777", "8.377"), ("Arkansas", "6.500", "2.947", "9.447"),
        ("California", "7.250", "1.601", "8.851"), ("Colorado", "2.900", "4.913", "7.813"),
        ("Connecticut", "6.350", "—", "6.350"), ("Delaware", "—", "—", "—"),
        ("District of Columbia", "6.000", "—", "6.000"), ("Florida", "6.000", "1.002", "7.002"),
        ("Georgia", "4.000", "3.384", "7.384"), ("Hawaii", "4.000", "0.500", "4.500"),
        ("Idaho", "6.000", "0.026", "6.026"), ("Illinois", "6.250", "2.607", "8.857"),
        ("Indiana", "7.000", "—", "7.000"), ("Iowa", "6.000", "0.941", "6.941"),
        ("Kansas", "6.500", "2.264", "8.764"), ("Kentucky", "6.000", "—", "6.000"),
        ("Louisiana", "5.000", "5.111", "10.111"), ("Maine", "5.500", "—", "5.500"),
        ("Maryland", "6.000", "—", "6.000"), ("Massachusetts", "6.250", "—", "6.250"),
        ("Michigan", "6.000", "—", "6.000"), ("Minnesota", "6.875", "1.156", "8.031"),
        ("Mississippi", "7.000", "0.062", "7.062"), ("Missouri", "4.225", "4.166", "8.391"),
        ("Montana", "—", "—", "—"), ("Nebraska", "5.500", "1.483", "6.983"),
        ("Nevada", "6.850", "1.386", "8.236"), ("New Hampshire", "—", "—", "—"),
        ("New Jersey", "6.625", "-0.030", "6.595"), ("New Mexico", "4.875", "2.760", "7.635"),
        ("New York", "4.000", "4.532", "8.532"), ("North Carolina", "4.750", "2.246", "6.996"),
        ("North Dakota", "5.000", "2.041", "7.041"), ("Ohio", "5.750", "1.488", "7.238"),
        ("Oklahoma", "4.500", "4.489", "8.989"), ("Oregon", "—", "—", "—"),
        ("Pennsylvania", "6.000", "0.341", "6.341"), ("Rhode Island", "7.000", "—", "7.000"),
        ("South Carolina", "6.000", "1.499", "7.499"), ("South Dakota", "4.200", "1.911", "6.111"),
        ("Tennessee", "7.000", "2.548", "9.548"), ("Texas", "6.250", "1.950", "8.200"),
        ("Utah", "6.100", "1.153", "7.253"), ("Vermont", "6.000", "0.359", "6.359"),
        ("Virginia", "5.300", "0.471", "5.771"), ("Washington", "6.500", "2.883", "9.383"),
        ("West Virginia", "6.000", "0.567", "6.567"), ("Wisconsin", "5.000", "0.700", "5.700"),
        ("Wyoming", "4.000", "1.441", "5.441"),
    ]
    HDR = ["State", "State Rate", "Avg. Local Rate", "Combined Rate", "Rank"]
    RANKS = {r[0]: str(i + 1) for i, r in enumerate(
        sorted(ROWS, key=lambda x: -(float(x[3]) if x[3] != "—" else -1)))}

    def mk(chunk):
        data = [HDR] + [[a, b, c, d, RANKS[a]] for (a, b, c, d) in chunk]
        t = Table(data, colWidths=[1.95 * inch, 1.15 * inch, 1.35 * inch, 1.35 * inch, 0.65 * inch],
                  hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#26303C")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.8),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.4),
            ("LEFTPADDING", (0, 0), (0, -1), 6),
            ("RIGHTPADDING", (-1, 0), (-1, -1), 6),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F4F6")]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#26303C")),
            ("LINEBELOW", (0, -1), (-1, -1), 0.6, colors.HexColor("#26303C")),
        ]))
        return t

    st = []
    st.append(Paragraph("State and Local Sales Tax Rates", h1))
    st.append(Paragraph("Fiscal Year 2026 &mdash; Rates in effect as of January 1, 2026 "
                        "&nbsp;|&nbsp; Fiscal Research Note No. 131", sub))
    st.append(Paragraph(
        "The table reports the general statewide sales tax rate together with a population-weighted "
        "average of local rates levied within each state. <b>All rates are expressed as percentages.</b> "
        "A long dash indicates that no tax of the relevant type is imposed. Local rates are weighted by "
        "ZIP-code level population and may change more frequently than statewide rates.", body))
    st.append(Paragraph("Table A. &nbsp;Statewide and average local rates, by state", cap))
    st.append(mk(ROWS[:27]))
    st.append(PageBreak())
    st.append(Paragraph("Table A. &nbsp;Statewide and average local rates, by state <i>(continued)</i>", cap))
    st.append(mk(ROWS[27:]))
    st.append(Paragraph(
        "Notes: The combined rate is the arithmetic sum of the statewide rate and the population-weighted "
        "average local rate. Rank is assigned on the combined rate, highest to lowest; states imposing no "
        "sales tax are ranked last. A negative average local rate reflects a statutory rebate applied in "
        "certain designated urban enterprise zones. Rates applicable to groceries, prepared food, motor "
        "vehicles and lodging may differ from the general rate and are not reported here.", note))

    def footer(c, d):
        c.saveState()
        c.setStrokeColor(colors.HexColor("#26303C")); c.setLineWidth(0.7)
        c.line(d.leftMargin, 0.58 * inch, d.pagesize[0] - d.rightMargin, 0.58 * inch)
        c.setFont("Helvetica", 6.6); c.setFillColor(SOFT)
        c.drawString(d.leftMargin, 0.40 * inch,
                     "Synthetic sample data for systems evaluation — not an authoritative source.")
        c.setFont("Helvetica-Bold", 7); c.setFillColor(colors.HexColor("#26303C"))
        c.drawRightString(d.pagesize[0] - d.rightMargin, 0.40 * inch, "FRN 131  |  %d of 2" % d.page)
        c.restoreState()

    build(f"{OUT}/03_state_local_sales_tax_rates_2026.pdf", letter, st, footer,
          dict(title="State and Local Sales Tax Rates — FY2026",
               author="Fiscal Research Office", subject="Statewide and average local sales tax rates"))


# ══════════════════════════════════════════════════════════════════════
# DOC 4 — Payroll withholding (LANDSCAPE)
# Trampas: 4 tablas distintas en una pagina, unidades mezcladas (% y $),
# orientacion apaisada, entidades que NO son brackets.
# ══════════════════════════════════════════════════════════════════════
def doc4():
    S = lambda n, **k: ParagraphStyle(n, **{**dict(fontName="Helvetica", textColor=INK), **k})
    h1 = S("h1", fontName="Helvetica-Bold", fontSize=14, leading=17, spaceAfter=2)
    sub = S("sb", fontSize=8.5, leading=11, textColor=colors.HexColor("#555"))
    cap = S("cp", fontName="Helvetica-Bold", fontSize=8.4, leading=10.5, spaceAfter=4,
            textColor=colors.HexColor("#0E4C5A"))
    note = S("nt", fontSize=6.9, leading=9.2, textColor=SOFT)

    def mini(data, widths, align_from=1):
        t = Table(data, colWidths=widths, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0E4C5A")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.6),
            ("ALIGN", (align_from, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#B8C4C8")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF3F4")]),
        ]))
        return t

    tA = mini([["Component", "Employee", "Employer", "Self-Employed"],
               ["Social Security (OASDI)", "6.20%", "6.20%", "12.40%"],
               ["Medicare (HI)", "1.45%", "1.45%", "2.90%"],
               ["Total", "7.65%", "7.65%", "15.30%"]],
              [1.55 * inch, 0.95 * inch, 0.95 * inch, 1.05 * inch])

    tB = mini([["Item", "2026", "2025"],
               ["Social Security wage base", "$181,800", "$176,100"],
               ["Medicare wage base", "No limit", "No limit"],
               ["FUTA wage base (federal)", "$7,000", "$7,000"],
               ["FUTA effective rate", "0.60%", "0.60%"]],
              [1.85 * inch, 1.10 * inch, 1.10 * inch])

    tC = mini([["Filing status", "Threshold"],
               ["Married filing jointly", "$250,000"],
               ["Married filing separately", "$125,000"],
               ["Single", "$200,000"],
               ["Head of household", "$200,000"],
               ["Qualifying surviving spouse", "$200,000"]],
              [2.05 * inch, 1.20 * inch])

    tD = mini([["Payroll period", "Periods per year", "Withholding allowance"],
               ["Weekly", "52", "$96.15"],
               ["Biweekly", "26", "$192.30"],
               ["Semimonthly", "24", "$208.33"],
               ["Monthly", "12", "$416.67"],
               ["Quarterly", "4", "$1,250.00"],
               ["Annually", "1", "$5,000.00"]],
              [1.35 * inch, 1.20 * inch, 1.45 * inch])

    left = [Paragraph("Table 1. Employment tax rates", cap), tA, Spacer(1, 12),
            Paragraph("Table 2. Wage bases and limits", cap), tB]
    right = [Paragraph("Table 3. Additional Medicare Tax — 0.9% surtax thresholds", cap), tC,
             Spacer(1, 12),
             Paragraph("Table 4. Withholding allowance by payroll period", cap), tD]

    grid = Table([[left, right]], colWidths=[4.7 * inch, 4.7 * inch], hAlign="LEFT")
    grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (0, -1), 22)]))

    st = [Paragraph("Employment Tax Rates and Withholding Thresholds", h1),
          Paragraph("Calendar Year 2026 &nbsp;|&nbsp; Employer Compliance Circular EC-26/11 "
                    "&nbsp;|&nbsp; Issued 13 November 2025", sub),
          Spacer(1, 5),
          Table([[""]], colWidths=[9.4 * inch], rowHeights=[2.0],
                style=TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#0E4C5A"))])),
          Spacer(1, 14), grid, Spacer(1, 16),
          Paragraph(
              "The Additional Medicare Tax of 0.9 percent applies to wages, compensation and self-employment "
              "income in excess of the threshold shown in Table 3 and is imposed on the employee only; there is "
              "no employer match. Employers must begin withholding in the pay period in which cumulative wages "
              "for the calendar year exceed $200,000, without regard to the employee's filing status or to wages "
              "paid by another employer. The withholding allowance amounts in Table 4 apply only to employees "
              "whose most recent withholding certificate was filed prior to 2020.", note)]

    def footer(c, d):
        c.saveState()
        c.setFillColor(colors.HexColor("#0E4C5A"))
        c.rect(0, 0, d.pagesize[0], 0.30 * inch, stroke=0, fill=1)
        c.setFont("Helvetica", 6.6); c.setFillColor(colors.HexColor("#CFE0E4"))
        c.drawString(d.leftMargin, 0.115 * inch,
                     "SYNTHETIC SAMPLE DATA — systems evaluation fixture. Not an authoritative tax publication.")
        c.setFont("Helvetica-Bold", 6.8); c.setFillColor(colors.white)
        c.drawRightString(d.pagesize[0] - d.rightMargin, 0.115 * inch, "EC-26/11   PAGE %d" % d.page)
        c.restoreState()

    build(f"{OUT}/04_employment_tax_rates_and_thresholds_2026.pdf", landscape(letter), st, footer,
          dict(title="Employment Tax Rates and Withholding Thresholds — CY2026",
               author="Employer Compliance Unit", subject="FICA, FUTA and Additional Medicare Tax parameters"),
          margins=(0.7, 0.7, 0.7, 0.62))


# ══════════════════════════════════════════════════════════════════════
# DOC 5 — Capital gains (se rasteriza despues → simula escaneo)
# Trampas: sin capa de texto (obliga OCR), separador "to" en vez de guion,
# nota al pie con una tasa adicional (NIIT) que no esta en la tabla.
# ══════════════════════════════════════════════════════════════════════
def doc5_digital(path):
    S = lambda n, **k: ParagraphStyle(n, **{**dict(fontName="Times-Roman", textColor=colors.black), **k})
    h1 = S("h1", fontName="Times-Bold", fontSize=14, leading=17, alignment=TA_CENTER, spaceAfter=3)
    sub = S("sb", fontName="Times-Italic", fontSize=9.5, leading=12,
            alignment=TA_CENTER, spaceAfter=14)
    body = S("bd", fontSize=9.4, leading=13, alignment=TA_JUSTIFY, spaceAfter=8)
    cap = S("cp", fontName="Times-Bold", fontSize=9.5, leading=12, spaceBefore=10, spaceAfter=6)
    note = S("nt", fontSize=8, leading=11, textColor=colors.black, spaceBefore=8)

    st = []
    st.append(Spacer(1, 6))
    st.append(Paragraph("INTERNAL CIRCULAR &mdash; CAPITAL GAINS DIVISION", S(
        "k", fontName="Times-Bold", fontSize=8, leading=10, alignment=TA_CENTER,
        textColor=colors.HexColor("#333333"), spaceAfter=8)))
    st.append(Paragraph("Preferential Rates on Long-Term Capital Gain "
                        "and Qualified Dividend Income", h1))
    st.append(Paragraph("Tax Year 2025 &mdash; Circular CG-2025/07", sub))

    st.append(Paragraph(
        "<b>SUPERSEDED.</b> &nbsp;This circular states the rates applicable to taxable years beginning "
        "before January 1, 2026. It is retained for reference in connection with amended returns and "
        "prior-period assessments. For taxable years beginning on or after January 1, 2026, see Circular "
        "CG-2026/03.", body))
    st.append(Paragraph(
        "Net long-term capital gain and qualified dividend income are taxed at the preferential rates set "
        "out below. The applicable rate is determined by reference to the taxpayer's total taxable income, "
        "including the gain itself. Where taxable income spans more than one band, the gain is allocated "
        "across the bands in order and each portion is taxed at the corresponding rate.", body))

    st.append(Paragraph("Table 1. Preferential rate bands by filing status, taxable income", cap))
    data = [
        ["Rate", "Single", "Married Filing Jointly", "Married Filing Separately", "Head of Household"],
        ["0 percent", "$0 to $48,350", "$0 to $96,700", "$0 to $48,350", "$0 to $64,750"],
        ["15 percent", "$48,351 to $533,400", "$96,701 to $600,050", "$48,351 to $300,000", "$64,751 to $566,700"],
        ["20 percent", "Over $533,400", "Over $600,050", "Over $300,000", "Over $566,700"],
    ]
    t = Table(data, colWidths=[0.85 * inch, 1.5 * inch, 1.6 * inch, 1.6 * inch, 1.5 * inch], hAlign="CENTER")
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Times-Roman"),
        ("FONTNAME", (0, 1), (0, -1), "Times-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.2),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCDCDC")),
    ]))
    st.append(t)

    st.append(Paragraph("Table 2. Special rate categories", cap))
    t2 = Table([["Category", "Maximum rate"],
                ["Unrecaptured section 1250 gain", "25 percent"],
                ["Collectibles and certain small business stock", "28 percent"],
                ["Short-term capital gain", "Ordinary rates"]],
               colWidths=[3.6 * inch, 1.6 * inch], hAlign="CENTER")
    t2.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Times-Roman"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.2),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCDCDC")),
    ]))
    st.append(t2)

    st.append(Paragraph(
        "NOTE. &nbsp;An additional Net Investment Income Tax of 3.8 percent applies to the lesser of net "
        "investment income or the excess of modified adjusted gross income over $200,000 (single and head "
        "of household), $250,000 (married filing jointly) or $125,000 (married filing separately). This "
        "surtax is imposed in addition to the rates shown in Table 1 and is not reflected in those rates.",
        note))

    def footer(c, d):
        c.saveState()
        c.setFont("Times-Italic", 7); c.setFillColor(colors.HexColor("#444444"))
        c.drawString(d.leftMargin, 0.5 * inch,
                     "Synthetic sample data for systems evaluation. Not an authoritative tax publication.")
        c.setFont("Times-Roman", 7.5); c.setFillColor(colors.black)
        c.drawRightString(d.pagesize[0] - d.rightMargin, 0.5 * inch, "CG-2025/07  —  %d" % d.page)
        c.restoreState()

    build(path, letter, st, footer,
          dict(title="Preferential Rates on Long-Term Capital Gain — CG-2025/07",
               author="Capital Gains Division", subject="Preferential rate bands"),
          margins=(0.95, 0.95, 0.9, 0.85))


if __name__ == "__main__":
    print("Generando fixtures digitales...")
    doc1(); doc2(); doc3(); doc4()
    doc5_digital(f"{OUT}/_tmp_05_digital.pdf")
    print("Listo.")
