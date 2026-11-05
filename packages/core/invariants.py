"""Shared invariants, asserted on both sides of a component boundary.

The label-leak check is asserted by the P2 publish step AND by the app's clause
loader, so a label can never reach the agent by either route.
"""

_LABEL_FIELDS = {"void", "label_void", "is_void", "label", "y", "target"}


class LabelLeakError(AssertionError):
    """A ground-truth label was found where the agent could see it."""


def assert_no_labels(columns: list[str], where: str) -> None:
    leaked = sorted(set(columns) & _LABEL_FIELDS)
    if leaked:
        raise LabelLeakError(
            f"label field(s) {leaked} present in {where}; "
            "labels belong only in the Langfuse dataset"
        )
