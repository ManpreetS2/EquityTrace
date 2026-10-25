"""Reject corrupt SEC numeric evidence at the domain boundary."""

import pytest


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_sec_values_are_rejected_before_persistence(invalid: float) -> None:
    from pydantic import ValidationError

    from equitytrace.sec.normalization import normalize_company_facts

    payload = {
        "cik": "0001000099",
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "val": invalid,
                                "end": "2023-12-31",
                                "filed": "2024-02-01",
                                "fy": 2023,
                                "fp": "FY",
                                "form": "10-K",
                            }
                        ]
                    }
                }
            }
        },
    }
    with pytest.raises(ValidationError, match="finite"):
        normalize_company_facts(payload)
