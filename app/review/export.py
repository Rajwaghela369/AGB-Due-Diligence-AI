"""Render a lawyer's durable decisions as deterministic DD deliverables.

Exports deliberately bypass summary generation: they may reuse a paragraph already cached for
the decision set, but downloading a file must never trigger a paid model call.
"""

from __future__ import annotations

import csv
import html
import io
import re
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import Cm

from app.domain import (
    DRAFT_WATERMARK,
    EXPORT_DISCLAIMER,
    EXPORT_MEDIA_TYPES,
    ExportBlocker,
    ExportFile,
    ExportFormat,
    Finding,
    Reviewer,
    decided_verdict,
)
from app.review.interfaces import ExportBlocked
from app.review.summary import _build_summary

if TYPE_CHECKING:
    from core.schemas import LawChunk

    from app.review.service import Service

_CSV_COLUMNS = [
    "position",
    "nummer",
    "ueberschrift",
    "klausel",
    "modell_ergebnis",
    "modell_konfidenz",
    "entscheidung",
    "ergebnis",
    "normen",
    "begruendung",
    "notiz",
    "entschieden_von",
    "entschieden_am",
    "agent_version",
    "entwurf",
]
_DECISIONS = {
    None: "offen",
    "accept": "übernommen",
    "override": "geändert",
    "dismiss": "verworfen",
}
_VERDICTS = {
    "potentially_void": "potenziell unwirksam",
    "uncertain": "zu prüfen",
    "valid": "wirksam",
    None: "—",
}
_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Berlin"))


def _utc(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _in_force(finding: Finding) -> tuple[str | None, list[str]]:
    if finding.verdict is None:
        return None, []
    if finding.decision is None:
        return finding.verdict.verdict, list(finding.verdict.legal_basis)
    return decided_verdict(finding.decision)


def _citation(service: Service, chunk_id: str) -> str:
    chunk = (service.statutes or {}).get(chunk_id)
    return chunk.citation if chunk is not None else chunk_id


def _reason(finding: Finding) -> str:
    if finding.decision is not None:
        if finding.decision.reason_de:
            return finding.decision.reason_de
        if finding.decision.action in ("override", "dismiss"):
            return "Entscheidung der prüfenden Person, ohne Modellbegründung."
    return finding.verdict.reason_de if finding.verdict is not None else ""


def _exporter(reviewer: Reviewer) -> str:
    name = reviewer.name or "Unbekannt"
    return name if reviewer.authenticated else f"{name} (nicht angemeldet)"


def _filename(filename: str, fmt: ExportFormat, draft: bool, date: datetime) -> str:
    safe = re.sub(r"[/\\\x00-\x1f\x7f]", "", filename).strip() or "Dokument"
    stem = (safe.rsplit(".", 1)[0] if "." in safe else safe).strip() or "Dokument"
    suffix = f"_{DRAFT_WATERMARK}" if draft else ""
    return f"AGB-Check_{stem}_{date:%Y-%m-%d}{suffix}.{fmt}"


def _md_text(value: Any, *, line_break: str = " ") -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = html.escape(text, quote=False)
    text = re.sub(r"([\\|`*_[\]])", r"\\\1", text)
    return text.replace("\n", line_break)


def _md_cell(value: Any) -> str:
    return _md_text(value, line_break="<br>")


def _xml_safe(value: Any) -> str:
    """Drop characters XML 1.0 forbids before handing text to python-docx."""
    return _XML_INVALID.sub("", str(value or ""))


def _csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formulas in every textual CSV field."""
    # Tabs and carriage returns are formula triggers themselves, so only discard ordinary
    # padding before checking the first meaningful byte.
    text = str(value or "").lstrip(" ")
    return "'" + text if text.startswith(_CSV_FORMULA_PREFIXES) else text


def _result_label(finding: Finding, verdict: str | None) -> str:
    if verdict is not None:
        return _VERDICTS[verdict]
    if finding.status == "out_of_scope":
        return "nicht im Prüfumfang"
    if finding.verdict is None:
        return "nicht geprüft"
    return _VERDICTS[None]


def _statute_sort(chunk: LawChunk) -> tuple[Any, ...]:
    def natural(value: str | None) -> tuple[Any, ...]:
        return tuple(
            int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", value or "")
        )

    return (
        chunk.law,
        natural(chunk.paragraph),
        natural(chunk.item),
        natural(chunk.sub_item),
        chunk.chunk_id,
    )


def _appendix(service: Service, findings: list[Finding]) -> list[LawChunk]:
    statutes = service.statutes or {}
    ids = {chunk_id for finding in findings for chunk_id in _in_force(finding)[1]}
    return sorted(
        (statutes[chunk_id] for chunk_id in ids if chunk_id in statutes), key=_statute_sort
    )


def _markdown(
    service: Service,
    summary: Any,
    findings: list[Finding],
    reviewer: Reviewer,
    draft: bool,
    review_date: datetime,
    export_date: datetime,
) -> bytes:
    title = "AGB-Prüfvermerk" + (f" – {DRAFT_WATERMARK}" if draft else "")
    lines = []
    if draft:
        lines.extend([f"> **{DRAFT_WATERMARK.upper()}** – Nicht zur finalen Verwendung", ""])
    lines.extend(
        [
            f"# {title}",
            "",
            f"- **Dokument:** {_md_text(summary.filename)}",
            f"- **Prüfdatum:** {review_date:%d.%m.%Y}",
            f"- **Exportiert am:** {export_date:%d.%m.%Y}",
            f"- **Exportiert von:** {_md_text(_exporter(reviewer))}",
            f"- **Prüfende Personen:** {_md_text(', '.join(summary.reviewers) or '—')}",
            f"- **Agent-Version:** `{_md_text(summary.agent_version)}`",
            "",
            f"> {EXPORT_DISCLAIMER}",
            "",
            "## Zusammenfassung",
            "",
            f"Feststellungen: {summary.counts.findings}; potenziell unwirksam: "
            f"{summary.counts.potentially_void}; zu prüfen: {summary.counts.uncertain}; "
            f"wirksam: {summary.counts.valid}; verworfen: {summary.counts.dismissed}.",
            "",
            summary.recommendation
            + (" *(KI-generierte Empfehlung)*" if summary.recommendation_from_llm else ""),
        ]
    )
    if summary.void_groups:
        lines.extend(["", "### Risiken nach Norm", ""])
        for group in summary.void_groups:
            clauses = ", ".join(item.number or str(item.position + 1) for item in group.findings)
            lines.append(f"- **{group.label}:** {clauses}")
    lines.extend(
        [
            "",
            "## Feststellungen",
            "",
            "| Klausel | Entscheidung | Ergebnis | Normen | Begründung | Notiz | "
            "Entschieden von | Entschieden am |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for finding in findings:
        verdict, basis = _in_force(finding)
        decision = finding.decision
        clause = f"{finding.clause.number or finding.position + 1} – {finding.clause.text}"
        values = [
            clause,
            _DECISIONS[decision.action if decision else None],
            _result_label(finding, verdict),
            " | ".join(_citation(service, chunk_id) for chunk_id in basis),
            _reason(finding),
            decision.note if decision else "",
            decision.decided_by if decision else "",
            _utc(decision.decided_at) if decision else "",
        ]
        lines.append("| " + " | ".join(_md_cell(value) for value in values) + " |")
    statutes = _appendix(service, findings) if service.deliverable.export.statute_appendix else []
    if statutes:
        lines.extend(["", "## Anhang: zitierte Normen", ""])
        for chunk in statutes:
            lines.extend([f"### {chunk.citation}", "", chunk.text, ""])
    return ("\n".join(lines).rstrip() + "\n").encode()


def _csv(service: Service, findings: list[Finding], agent_version: str, draft: bool) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=_CSV_COLUMNS, delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    for finding in findings:
        verdict, basis = _in_force(finding)
        decision = finding.decision
        model = finding.verdict
        row: dict[str, Any] = {
            "position": finding.position,
            "nummer": finding.clause.number or "",
            "ueberschrift": finding.clause.heading or "",
            "klausel": finding.clause.text,
            "modell_ergebnis": model.verdict if model else "",
            "modell_konfidenz": str(model.confidence) if model else "",
            "entscheidung": _DECISIONS[decision.action if decision else None],
            "ergebnis": verdict or _result_label(finding, verdict),
            "normen": " | ".join(_citation(service, chunk_id) for chunk_id in basis),
            "begruendung": _reason(finding),
            "notiz": decision.note if decision else "",
            "entschieden_von": decision.decided_by if decision else "",
            "entschieden_am": _utc(decision.decided_at) if decision else "",
            "agent_version": agent_version,
            "entwurf": DRAFT_WATERMARK if draft else "nein",
        }
        numeric = {"position", "modell_konfidenz"}
        writer.writerow(
            {key: value if key in numeric else _csv_safe(value) for key, value in row.items()}
        )
    return output.getvalue().encode("utf-8-sig")


def _clear_body(document: Any) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _page_field(paragraph: Any) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instruction, end])


def _watermark(section: Any) -> None:
    paragraph = section.header.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    xml = (
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office"><w:pict>'
        '<v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" '
        'adj="10800" path="m@7,l@8,m@5,21600l@6,21600e">'
        '<v:formulas><v:f eqn="sum #0 0 10800"/><v:f eqn="prod #0 2 1"/>'
        '<v:f eqn="sum 21600 0 @1"/><v:f eqn="sum 0 0 @2"/>'
        '<v:f eqn="sum 21600 0 @3"/><v:f eqn="if @0 @3 0"/>'
        '<v:f eqn="if @0 21600 @1"/><v:f eqn="if @0 0 @2"/>'
        '<v:f eqn="if @0 @4 21600"/><v:f eqn="mid @5 @6"/>'
        '<v:f eqn="mid @8 @5"/><v:f eqn="mid @7 @8"/>'
        '<v:f eqn="mid @6 @7"/><v:f eqn="sum @6 0 @5"/></v:formulas>'
        '<v:path textpathok="t" o:connecttype="custom" '
        'o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" '
        'o:connectangles="270,180,90,0"/><v:textpath on="t" fitshape="t"/>'
        '<v:handles><v:h position="#0,bottomRight" xrange="6629,14971"/></v:handles>'
        '<o:lock v:ext="edit" text="t" shapetype="t"/></v:shapetype>'
        '<v:shape id="PowerPlusWaterMarkObject" '
        'o:spid="_x0000_s1025" type="#_x0000_t136" '
        'style="position:absolute;width:420pt;height:90pt;rotation:315;z-index:-251654144;'
        "mso-position-horizontal:center;mso-position-horizontal-relative:margin;"
        'mso-position-vertical:center;mso-position-vertical-relative:margin" '
        'fillcolor="#d9d9d9" stroked="f" o:allowincell="f"><v:fill opacity=".5"/>'
        '<v:textpath style="font-family:Arial;font-size:1pt" on="t" fitshape="t" '
        f'string="{DRAFT_WATERMARK}"/></v:shape></w:pict></w:r>'
    )
    paragraph._p.append(parse_xml(xml))


def _add_labeled(document: Any, label: str, value: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.add_run(label + ": ").bold = True
    paragraph.add_run(_xml_safe(value))


def _docx(
    service: Service,
    summary: Any,
    findings: list[Finding],
    reviewer: Reviewer,
    draft: bool,
    review_date: datetime,
    export_date: datetime,
    timestamp: datetime,
) -> bytes:
    template = service.deliverable.export.template_dir / "memo.docx"
    document = DocxDocument(template)
    _clear_body(document)
    section = document.sections[0]
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.0)
    footer = section.footer.paragraphs[0]
    footer.text = _xml_safe(f"AGB-Check · {summary.agent_version} · Seite ")
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _page_field(footer)
    if draft:
        _watermark(section)

    title = document.add_paragraph(style="Title")
    title.add_run("AGB-Prüfvermerk" + (f" – {DRAFT_WATERMARK}" if draft else ""))
    _add_labeled(document, "Dokument", summary.filename)
    _add_labeled(document, "Prüfdatum", f"{review_date:%d.%m.%Y}")
    _add_labeled(document, "Exportiert am", f"{export_date:%d.%m.%Y}")
    _add_labeled(document, "Exportiert von", _exporter(reviewer))
    _add_labeled(document, "Prüfende Personen", ", ".join(summary.reviewers) or "—")
    _add_labeled(document, "Agent-Version", summary.agent_version)
    disclaimer = document.add_paragraph(_xml_safe(EXPORT_DISCLAIMER), style="Disclaimer")
    disclaimer.runs[0].italic = True

    document.add_heading("Zusammenfassung", level=1)
    document.add_paragraph(
        f"Feststellungen: {summary.counts.findings}; potenziell unwirksam: "
        f"{summary.counts.potentially_void}; zu prüfen: {summary.counts.uncertain}; "
        f"wirksam: {summary.counts.valid}; verworfen: {summary.counts.dismissed}."
    )
    recommendation = document.add_paragraph(_xml_safe(summary.recommendation))
    if summary.recommendation_from_llm:
        recommendation.add_run(" (KI-generierte Empfehlung)").italic = True
    for group in summary.void_groups:
        clauses = ", ".join(item.number or str(item.position + 1) for item in group.findings)
        _add_labeled(document, group.label, clauses)

    document.add_heading("Feststellungen", level=1)
    headers = [
        "Klausel",
        "Entscheidung",
        "Ergebnis",
        "Normen",
        "Begründung",
        "Notiz",
        "Audit",
    ]
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "AGB Findings"
    for cell, header in zip(table.rows[0].cells, headers, strict=True):
        cell.text = header
    for finding in findings:
        verdict, basis = _in_force(finding)
        decision = finding.decision
        row = table.add_row().cells
        values = [
            f"{finding.clause.number or finding.position + 1}\n{finding.clause.text}",
            _DECISIONS[decision.action if decision else None],
            _result_label(finding, verdict),
            "\n".join(_citation(service, chunk_id) for chunk_id in basis),
            _reason(finding),
            decision.note if decision else "",
            (f"{decision.decided_by or '—'}\n{_utc(decision.decided_at)}" if decision else "—"),
        ]
        for cell, value in zip(row, values, strict=True):
            cell.text = _xml_safe(value)

    statutes = _appendix(service, findings) if service.deliverable.export.statute_appendix else []
    if statutes:
        document.add_page_break()
        document.add_heading("Anhang: zitierte Normen", level=1)
        for chunk in statutes:
            document.add_heading(_xml_safe(chunk.citation), level=2)
            document.add_paragraph(_xml_safe(chunk.text))

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    document.core_properties.created = timestamp
    document.core_properties.modified = timestamp
    raw = io.BytesIO()
    document.save(raw)
    return _stable_zip(raw.getvalue(), timestamp)


def _stable_zip(content: bytes, timestamp: datetime) -> bytes:
    stamp = timestamp.astimezone(UTC)
    year = min(2107, max(1980, stamp.year))
    date_time = (year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second)
    source = zipfile.ZipFile(io.BytesIO(content))
    output = io.BytesIO()
    with source, zipfile.ZipFile(output, "w") as target:
        for original in source.infolist():
            info = zipfile.ZipInfo(original.filename, date_time)
            info.compress_type = original.compress_type
            info.external_attr = original.external_attr
            info.create_system = original.create_system
            target.writestr(info, source.read(original.filename))
    return output.getvalue()


def build_export(
    service: Service,
    review_id: str,
    fmt: ExportFormat,
    *,
    draft: bool,
    reviewer: Reviewer,
) -> ExportFile:
    """Build one export, enforcing final-export readiness before rendering."""
    review = service.get(review_id)
    if review is None:
        raise KeyError(review_id)
    findings = service.findings(review_id)
    blockers = [
        ExportBlocker(
            position=finding.position,
            number=finding.clause.number,
            heading=finding.clause.heading,
            verdict=finding.verdict.verdict,
        )
        for finding in findings
        if finding.verdict is not None
        and finding.verdict.verdict in ("potentially_void", "uncertain")
        and finding.decision is None
    ]
    if not draft and review.status in ("queued", "running", "failed"):
        raise ExportBlocked("not_finished", [])
    if not draft and blockers:
        raise ExportBlocked("undecided", blockers)

    summary = _build_summary(service, review_id, allow_model_call=False)
    rendered_findings = findings
    now = _now()
    review_date = review.finished_at or review.created_at or now
    if review_date.tzinfo is None:
        review_date = review_date.replace(tzinfo=UTC)
    review_date = review_date.astimezone(ZoneInfo("Europe/Berlin"))
    if fmt == "md":
        content = _markdown(service, summary, rendered_findings, reviewer, draft, review_date, now)
    elif fmt == "csv":
        content = _csv(service, rendered_findings, review.agent_version, draft)
    else:
        content = _docx(
            service,
            summary,
            rendered_findings,
            reviewer,
            draft,
            review_date,
            now,
            review.finished_at or review.created_at or now,
        )
    return ExportFile(
        filename=_filename(summary.filename, fmt, draft, now),
        media_type=EXPORT_MEDIA_TYPES[fmt],
        content=content,
        draft=draft,
    )
