"""The v1 slice. Stratification is what makes 70 clauses worth 419."""

import pandas as pd
import pytest

from evals.datasets import INPUT_KEYS, EvalItem, load_dev, select, slice_label, stratified


def _item(clause_id: str, void: bool, contract: int = 1) -> EvalItem:
    return EvalItem(
        id=clause_id,
        input={"clause_id": clause_id, "title": "t", "text": "x", "topics": ["liability"]},
        expected_output={"label_void": void},
        metadata={"split": "dev", "contract_id": contract},
    )


def _population(void: int, non_void: int) -> list[EvalItem]:
    return [
        *(_item(f"agb-v{i:03d}", True, contract=i) for i in range(void)),
        *(_item(f"agb-n{i:03d}", False, contract=100 + i) for i in range(non_void)),
    ]


def test_stratified_takes_every_void_clause():
    # Recall only involves void clauses, so taking all of them is the whole
    # point: 70 stratified clauses hold 31 void where 200 contiguous hold 22.
    items = stratified(_population(31, 388), non_void=39, seed=0)

    assert sum(1 for i in items if i.label_void) == 31
    assert len(items) == 70


def test_stratified_weights_correct_back_to_the_population():
    items = stratified(_population(31, 388), non_void=39, seed=0)

    void_weights = {i.metadata["sample_weight"] for i in items if i.label_void}
    non_void_weights = sorted({i.metadata["sample_weight"] for i in items if not i.label_void})

    # Void is sampled at 100%, so it stands only for itself.
    assert void_weights == {1.0}
    assert non_void_weights == [pytest.approx(388 / 39, abs=1e-4)]
    # 9.9487: every sampled non-void clause stands for ~10 of the 388.
    assert non_void_weights[0] == pytest.approx(9.9487, abs=1e-4)


def test_stratified_tags_the_stratum():
    items = stratified(_population(5, 50), non_void=10, seed=0)

    assert {i.metadata["stratum"] for i in items if i.label_void} == {"void"}
    assert {i.metadata["stratum"] for i in items if not i.label_void} == {"non_void"}


def test_stratified_is_deterministic_for_a_seed():
    first = stratified(_population(31, 388), non_void=39, seed=0)
    again = stratified(_population(31, 388), non_void=39, seed=0)
    other = stratified(_population(31, 388), non_void=39, seed=1)

    assert [i.id for i in first] == [i.id for i in again]
    assert [i.id for i in first] != [i.id for i in other]


def test_stratified_samples_rather_than_taking_the_first_n():
    # The weighting is only unbiased if the stratum represents the population.
    # In the real dev half the first 39 non-void clauses come from 15 of 84
    # shops where a seeded sample spans 33.
    population = _population(5, 200)
    items = stratified(population, non_void=20, seed=0)

    chosen = [i.id for i in items if not i.label_void]
    first_n = [i.id for i in population if not i.label_void][:20]
    assert chosen != first_n


def test_stratified_returns_items_sorted_by_id():
    items = stratified(_population(5, 50), non_void=10, seed=0)

    assert [i.id for i in items] == sorted(i.id for i in items)


def test_stratified_refuses_to_oversample():
    with pytest.raises(ValueError, match="only 5 exist"):
        stratified(_population(3, 5), non_void=10)


def test_select_all_weights_everything_evenly():
    # An unsliced run's weighted metrics must equal its raw ones, so the
    # default weight is 1.0 rather than a missing key reading as zero.
    items = select(_population(2, 3), spec="all")

    assert len(items) == 5
    assert {i.metadata["sample_weight"] for i in items} == {1.0}


def test_select_first_n_takes_a_contiguous_slice():
    items = select(_population(2, 8), spec="first:4")

    assert len(items) == 4
    assert {i.metadata["sample_weight"] for i in items} == {1.0}


def test_select_stratified_delegates():
    items = select(_population(5, 50), spec="stratified", non_void=10, seed=0)

    assert len(items) == 15


def test_select_rejects_an_unknown_spec():
    with pytest.raises(ValueError, match="unknown slice spec"):
        select(_population(1, 1), spec="every-other-one")


def test_slice_label_keeps_slices_distinct_in_report_names():
    assert slice_label("stratified", 70) == "dev70"
    assert slice_label("first:5", 5) == "dev5"
    assert slice_label("all", 419) == "dev"


def test_dev_items_carry_exactly_the_published_input_keys(tmp_path):
    # Dev and held-out runs must hand the task identical shapes, or their
    # numbers are not comparable.
    frame = pd.DataFrame(
        [
            {
                "clause_id": "agb-349",
                "contract_id": 12,
                "section_no": "6",
                "section_title": "Haftung",
                "text": "Das Recht auf Schadenersatz wird beschränkt.",
                "topics": ["liability"],
                "is_fragment": False,
                "foreign_law_choice": False,
                "label_void": True,
            }
        ]
    )
    path = tmp_path / "train.parquet"
    frame.to_parquet(path, index=False)

    items = load_dev(path)

    assert tuple(items[0].input) == INPUT_KEYS
    assert items[0].label_void is True
    assert "label_void" not in items[0].input
