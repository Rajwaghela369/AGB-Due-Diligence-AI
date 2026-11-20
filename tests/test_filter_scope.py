import pandas as pd
import pytest

from pipelines.p2_eval.s3_filter_scope import filter_scope, foreign_law_contracts
from pipelines.p2_eval.s4_split import UnassignedClauses, split_clauses

# The real clause from contract 18, which cites Rome I to say that the foreign
# choice does not displace the consumer's home protections.
FRENCH = (
    "Die vorliegenden Allgemeinen Geschäftsbedingungen sind nach französischem Recht "
    "abgefasst und entsprechend auszulegen. In Übereinstimmung mit der Vorschrift "
    "EG 593/2008 widersprechen sie jedoch nicht den Bestimmungen am Wohnort des Kunden."
)
GERMAN = "Es gilt das Recht der Bundesrepublik Deutschland unter Ausschluss des UN-Kaufrechts."


def _clause(**overrides):
    row = {
        "clause_id": "agb-1",
        "source_id": 1,
        "contract_id": 1,
        "text": "Wir haften nur bei Vorsatz.",
        "topics_raw": ["liability"],
        "topics": ["liability"],
        "is_fragment": False,
        "label_void": False,
    }
    return row | overrides


def _frame(rows):
    return pd.DataFrame(rows)


def test_foreign_law_detects_only_an_actual_foreign_choice():
    clauses = _frame(
        [
            _clause(source_id=1, contract_id=18, text=FRENCH, topics_raw=["applicableLaw"]),
            _clause(source_id=2, contract_id=20, text=GERMAN, topics_raw=["applicableLaw"]),
        ]
    )

    # Carrying an `applicableLaw` clause is not the same as choosing foreign
    # law - 61 of 89 in-scope shops have one, nearly all choosing German law.
    assert foreign_law_contracts(clauses) == [18]


def test_foreign_law_ignores_the_phrase_outside_a_choice_of_law_clause():
    clauses = _frame(
        [
            _clause(
                contract_id=5,
                text="Unsere Niederlassung unterliegt schweizer Recht.",
                topics_raw=["liability"],
            )
        ]
    )

    assert foreign_law_contracts(clauses) == []


def test_filter_scope_keeps_foreign_law_clauses_and_flags_them(tmp_path):
    clauses = _frame(
        [
            _clause(source_id=1, contract_id=18, text=FRENCH, topics_raw=["applicableLaw"]),
            _clause(source_id=2, contract_id=18, label_void=True),
            _clause(source_id=3, contract_id=20, topics_raw=["warranty"]),
            _clause(source_id=4, contract_id=20, topics_raw=["payment"]),
        ]
    )
    path = tmp_path / "clauses.parquet"
    clauses.to_parquet(path, index=False)

    result = filter_scope(path, ["liability", "warranty"], tmp_path)

    # The payment clause and the choice-of-law clause fall outside the remit;
    # the void clause from the foreign-law shop stays, flagged.
    assert (result.clauses, result.void) == (2, 1)
    assert result.foreign_law_clauses == 1
    assert result.foreign_law_contracts == [18]

    scoped = pd.read_parquet(result.path)
    assert set(scoped.source_id) == {2, 3}
    assert scoped.set_index("source_id").loc[2, "foreign_law_choice"]


def test_filter_scope_keeps_fragments(tmp_path):
    clauses = _frame([_clause(source_id=1, text="12.3 WANN SIE ANWENDBAR SIND", is_fragment=True)])
    path = tmp_path / "clauses.parquet"
    clauses.to_parquet(path, index=False)

    result = filter_scope(path, ["liability", "warranty"], tmp_path)

    assert (result.clauses, result.fragments) == (1, 1)


def _scope_file(tmp_path, source_ids):
    frame = _frame([_clause(source_id=i, clause_id=f"agb-{i}") for i in source_ids])
    frame["foreign_law_choice"] = False
    path = tmp_path / "scope.parquet"
    frame.to_parquet(path, index=False)
    return path


def _splits_file(tmp_path, assignment):
    path = tmp_path / "splits.parquet"
    pd.DataFrame({"id": list(assignment), "split": [assignment[i] for i in assignment]}).to_parquet(
        path, index=False
    )
    return path


def test_split_writes_one_file_per_half(tmp_path):
    scope = _scope_file(tmp_path, [1, 2, 3])
    splits = _splits_file(tmp_path, {1: "train", 2: "train", 3: "test"})

    result = split_clauses(scope, splits, "train", "test", tmp_path)

    assert result.counts == {"train": 2, "test": 1}
    assert pd.read_parquet(result.paths["test"]).source_id.tolist() == [3]


def test_split_fails_loudly_on_an_unassigned_clause(tmp_path):
    scope = _scope_file(tmp_path, [1, 2])
    splits = _splits_file(tmp_path, {1: "train"})

    with pytest.raises(UnassignedClauses, match="1 of 2"):
        split_clauses(scope, splits, "train", "test", tmp_path)
