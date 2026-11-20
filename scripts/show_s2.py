"""Run step 2.2 and print what each piece of the normalizer does.

Inspection only. Reads data/raw/agb-de/agb-de-anonym.csv (run scripts/show_s1.py
first) and writes data/interim/agb_clauses.parquet. Safe to re-run.
"""

from collections import Counter

import pandas as pd
from core.config import load_yaml
from core.paths import CONFIGS, INTERIM, RAW, ROOT

from pipelines.p2_eval.s2_normalize import (
    extract_citations,
    normalize,
    split_title,
    to_agent_topics,
)

PLACEHOLDERS = ("<<NAME>>", "<<STADT>>", "<<STRASSE>>")
IN_SCOPE = {"liability", "warranty"}

# Every title shape the corpus actually contains, including the degenerate ones.
TITLE_SAMPLES = (
    "10. Gewährleistung",
    "§8 Mängelgewährleistung, Garantie",
    "§4 - LIEFERUNG UND VERSANDKOSTEN",
    "5.      Eigentums- und Gefahrenübergang",
    "5",
    "7.4.: ",
    "Gewährleistung",
    None,
)

CITATION_SAMPLE = (
    "Es gilt § 478 BGB. Die Rügepflicht nach § 377 HGB bleibt unberührt. "
    "Vgl. § 281 Abs. 1 BGB sowie die Regelung in § 10 dieser AGB."
)


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n {title}\n{'=' * 78}")


def main() -> None:
    pd.set_option("display.width", 200)
    config = load_yaml(CONFIGS / "eval.yaml")
    scope = config["scope"]

    head("INPUT - the scope rules this step reads")
    print(f"  fragment_chars : {scope['fragment_chars']}  (texts shorter than this are flagged)")
    print(f"  placeholders   : {scope['placeholders']}  (must survive verbatim)")

    head("PIECE 1 - split_title(): two numbering conventions, three degenerate cases")
    for sample in TITLE_SAMPLES:
        number, heading = split_title(sample)
        print(f"  {str(sample)!r:45} -> section_no={number!r:8} section_title={heading!r}")

    head("PIECE 2 - extract_citations(): the law is recorded, never guessed")
    print(f"  text: {CITATION_SAMPLE}\n")
    for citation in extract_citations(CITATION_SAMPLE):
        note = "" if citation["law"] else "   <- unknown: could be the shop's own section"
        print(
            f"  {citation['raw']:20} paragraph={citation['paragraph']:6} "
            f"law={citation['law']!s:6}{note}"
        )

    head("PIECE 3 - to_agent_topics(): AGB-DE's vocabulary -> core.schemas.Topic")
    for raw in (["liability"], ["warranty", "delivery"], ["payment"], ["applicableLaw"], []):
        print(f"  {str(raw):32} -> {to_agent_topics(raw)}")
    print("\n  note: applicableLaw collapses to 'other', which is exactly why")
    print("        topics_raw is kept too - step 2.3 needs it to find those clauses")

    head("FULL STEP - normalize()")
    result = normalize(RAW / "agb-de" / "agb-de-anonym.csv", scope["fragment_chars"], INTERIM)
    print("\n  NormalizeResult:")
    for field in ("rows", "fragments", "with_citations"):
        print(f"    {field:16} {getattr(result, field)}")
    print(f"    path             {result.path.relative_to(ROOT)}")

    frame = pd.read_parquet(result.path)

    head("OUTPUT SHAPE")
    print(f"  rows x columns : {frame.shape}")
    print("\n  columns:")
    for name, dtype in frame.dtypes.items():
        print(f"    {name:16} {dtype}")

    head("OUTPUT CONTENT")
    print(f"  label_void      : {frame.label_void.value_counts().to_dict()}")
    print(f"  is_fragment     : {frame.is_fragment.value_counts().to_dict()}")
    print(f"  section_no null : {frame.section_no.isna().sum()}")
    print(f"  section_title null : {frame.section_title.isna().sum()}")

    head("PLACEHOLDER SURVIVAL - anonymisation must pass through untouched")
    for placeholder in PLACEHOLDERS:
        count = int(frame.text.str.contains(placeholder, regex=False).sum())
        print(f"  {placeholder:12} {count:>5} clauses")

    head("SAMPLE ROWS")
    print(
        frame[["clause_id", "contract_id", "section_no", "section_title", "topics", "label_void"]]
        .head(6)
        .to_string(index=False, max_colwidth=34)
    )

    head("WHY cited_refs IS DIAGNOSTIC ONLY - the in-scope picture")
    scoped = frame[frame.topics_raw.apply(lambda t: bool(IN_SCOPE & set(t)))]
    named: Counter = Counter()
    unnamed: Counter = Counter()
    for refs in scoped.cited_refs:
        for citation in refs:
            (named if citation["law"] else unnamed)[citation["paragraph"]] += 1
    print(f"  in-scope clauses            : {len(scoped)}")
    print(f"  ...citing at least one §    : {int((scoped.cited_refs.str.len() > 0).sum())}")
    print(f"  citations naming a law      : {sum(named.values())}  {named.most_common(6)}")
    print(f"  citations with no law named : {sum(unnamed.values())}  {unnamed.most_common(6)}")
    low = sum(v for k, v in unnamed.items() if int("".join(c for c in k if c.isdigit())) <= 20)
    print(f"  ...of those, § <= 20        : {low}  <- mostly the shop's own cross-references")


if __name__ == "__main__":
    main()
