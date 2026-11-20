"""2.3 Scope filter: narrow to the prototype's remit, and flag without dropping.

Narrowing to liability and warranty is the filter. Within that slice nothing is
removed: a clause that is awkward to judge is still a clause the agent will meet
in production. Awkwardness is recorded as a column and acted on at the publish
boundary, which follows how P1 keeps its manifest inside the collection and uses
`kind` to hold it out of searches.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from core.logging import get_logger

log = get_logger("p2.scope")

APPLICABLE_LAW_TOPIC = "applicableLaw"

# A shop choosing the law of another country. Matched only inside a clause the
# annotators tagged `applicableLaw`, so ordinary prose about a foreign branch
# office cannot trip it.
_FOREIGN_LAW = re.compile(
    r"\b(?:schweizer(?:isch\w*)?|schweiz|österreich\w*|liechtenstein\w*|luxemburg\w*"
    r"|niederländisch\w*|französisch\w*|italienisch\w*|spanisch\w*|polnisch\w*"
    r"|englisch\w*|britisch\w*|US-amerikanisch\w*)\s+Recht\b"
    r"|\bRecht\s+(?:der\s+)?(?:Schweiz|Österreichs?|Italiens?|Frankreichs?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScopeResult:
    path: Path
    clauses: int
    void: int
    contracts: int
    fragments: int
    foreign_law_clauses: int
    foreign_law_contracts: list[int]


def foreign_law_contracts(clauses: pd.DataFrame) -> list[int]:
    """Shops whose choice-of-law clause names a law other than German law.

    Flagged for error analysis, never excluded. Under Art. 6(2) Rome I a choice
    of law in a consumer contract cannot strip the consumer of the mandatory
    protections of their home country, so § 305 ff. BGB still governs these
    clauses for a German consumer - which is why the annotators marked some of
    them void, and why dropping them would delete correct ground truth. One of
    the two shops in the corpus says as much in the clause itself, citing
    EG 593/2008. See docs/Phase 2 Results.md.
    """
    choice_of_law = clauses[clauses.topics_raw.apply(lambda t: APPLICABLE_LAW_TOPIC in t)]
    foreign = choice_of_law[choice_of_law.text.str.contains(_FOREIGN_LAW, regex=True)]
    return sorted(foreign.contract_id.unique().tolist())


def filter_scope(
    clauses_path: Path,
    in_scope_topics: list[str],
    out_dir: Path,
) -> ScopeResult:
    clauses = pd.read_parquet(clauses_path)
    wanted = set(in_scope_topics)

    # Detected across the whole corpus: the choice-of-law clause of a shop is
    # itself out of scope, so it would be invisible after the narrowing.
    flagged = foreign_law_contracts(clauses)

    scoped = clauses[clauses.topics_raw.apply(lambda t: bool(wanted & set(t)))].copy()
    scoped["foreign_law_choice"] = scoped.contract_id.isin(flagged)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "agb_scope_v1.parquet"
    scoped.to_parquet(path, index=False)

    result = ScopeResult(
        path=path,
        clauses=len(scoped),
        void=int(scoped.label_void.sum()),
        contracts=int(scoped.contract_id.nunique()),
        fragments=int(scoped.is_fragment.sum()),
        foreign_law_clauses=int(scoped.foreign_law_choice.sum()),
        foreign_law_contracts=sorted(
            scoped.loc[scoped.foreign_law_choice, "contract_id"].unique().tolist()
        ),
    )
    log.info(
        "scoped %d clauses (%d void) across %d contracts; "
        "flagged %d fragment(s) and %d clause(s) under a foreign choice of law",
        result.clauses,
        result.void,
        result.contracts,
        result.fragments,
        result.foreign_law_clauses,
    )
    return result
