# Reference-portfolio calibration record

This record establishes the shipped starting values for the deterministic
forecast on the labelled reference data. It is an engineering calibration,
not evidence from a customer book: the displayed probability remains a
bounded risk score until a customer's own historical backtest is complete.

## Procedure

The calibration uses the deterministic product path in this order for each
borrower and scoring day:

1. Take the utilisation signal history available on that day.
2. Apply T3 persistence and T4 materiality through their production domain
   functions.
3. Project the 90-day path and find the first inclusive covenant crossing.
4. Treat the C-79 evaluation result as an escalated warning only when the
   path crosses and the evidence is sustained. This is the same `crossed and
   sustained` rule used by the product evaluation arm; it avoids treating a
   transient probability fluctuation as a case escalation.
5. Apply the selected probability weights to the final stable-cohort forecast
   and apply T1 banding. Driver shares are normalized with T5 and must sum to
   one.

The paired acceptance checks are deliberately evaluated together:

- G1 requires at least 70% of deteriorating borrowers to warn at least 30
  days before their derived threshold crossing and at least 50% at least 60
  days before.
- G3 requires no more than 10% of the monitored evaluation book to be
  escalated on any scoring day and no more than 5% false escalation across
  the noisy-transient and stable labelled cohorts.

The deterministic fixture is the reduced C-79 reference build used by the
offline evaluation examples: seed 17, 24 borrowers, 57 facilities, four
financial quarters, two borrowers in each authored cohort, 365 scoring days,
and an 85.00 maximum-utilisation covenant. The denominator for the daily
escalation share is all 24 borrowers, including the templated remainder, so
the result cannot be made to look quiet by dropping unlabelled rows.

The first pass met both paired checks, so its settings were retained. A
The first pass evaluates the complete T1/T3/T4/T5 threshold set and the
three-term weight mapping together. It met both paired checks, so no change
to T1, T4, T5 or the mapping weights was justified. A permissive T3 trial was
nevertheless scored to exercise the rejection path:
changing T3's event arm from three events to one did not improve the
aggregate G1 rates, raised the maximum daily escalation share from 8.33% to
16.67%, and raised the combined false-escalation rate from 0% to 50%. It was
rejected and the original settings remained in force.

The forecast domain contract requires its weights from the scoring caller;
the threshold JSON intentionally accepts exactly T1 through T12 and cannot
carry an extra weights section. The selected weights below are therefore the
versioned evaluation/deployment input for the call site, while the four
threshold sections are verified against `config/thresholds.default.json` by
the regression suite.

## Recorded result

The JSON block is the canonical calibration record. Decimal values are stored
as strings so that reruns do not depend on binary floating-point conversion.
Scores are reported to six decimal places; the stable probability is reported
to twelve decimal places.

```json
{
  "record_type": "covenant_radar_threshold_calibration",
  "record_version": 1,
  "status": "accepted",
  "dataset": {
    "seed": 17,
    "borrower_count": 24,
    "facility_count": 57,
    "quarter_count": 4,
    "authored_cohort_size": 2,
    "signal_days": 365,
    "covenant_threshold": "85.00"
  },
  "acceptance": {
    "g1_flagged_30_rate": "0.70",
    "g1_flagged_60_rate": "0.50",
    "g3_max_escalation_share": "0.10",
    "g3_false_escalation_rate": "0.05"
  },
  "procedure": {
    "warning_horizon_days": 90,
    "warning_rule": "first crossing within the horizon and sustained T3 evidence",
    "reporting_precision": "rates to 6 decimal places; stable probability to 12 decimal places"
  },
  "iterations": [
    {
      "iteration": 0,
      "changes": {},
      "decision": "baseline retained; both paired acceptance checks passed",
      "scores": {
        "g1_flagged_30_rate": "1.000000",
        "g1_flagged_60_rate": "1.000000",
        "g1_lead_days": [156, 156],
        "g3_false_escalation_rate": "0.000000",
        "g3_max_escalation_count": 2,
        "g3_max_escalation_share": "0.083333",
        "g3_noisy_false_escalation_rate": "0.000000",
        "g3_stable_false_escalation_rate": "0.000000",
        "stable_latest_below_amber": true,
        "stable_latest_max_probability": "0.017287133838"
      }
    },
    {
      "iteration": 1,
      "changes": {
        "T3.sustained_events": {
          "before": 3,
          "after": 1
        }
      },
      "decision": "rejected; G3 escalation share and false-escalation rate exceeded acceptance checks",
      "scores": {
        "g1_flagged_30_rate": "1.000000",
        "g1_flagged_60_rate": "1.000000",
        "g1_lead_days": [156, 156],
        "g3_false_escalation_rate": "0.500000",
        "g3_max_escalation_count": 4,
        "g3_max_escalation_share": "0.166667",
        "g3_noisy_false_escalation_rate": "1.000000",
        "g3_stable_false_escalation_rate": "0.000000",
        "stable_latest_below_amber": true,
        "stable_latest_max_probability": "0.017287133838"
      }
    }
  ],
  "selected": {
    "thresholds": {
      "T1": {
        "act": "0.70",
        "amber": "0.40"
      },
      "T3": {
        "sustained_days": 14,
        "sustained_events": 3,
        "event_window_days": 30
      },
      "T4": {
        "headroom_erosion_pct": "0.05"
      },
      "T5": {
        "contribution_share": "0.10"
      }
    },
    "weights": {
      "distance": "0.50",
      "velocity": "0.30",
      "pressure": "0.20",
      "max_probability": "0.99"
    }
  },
  "final_scores": {
    "g1_flagged_30_rate": "1.000000",
    "g1_flagged_60_rate": "1.000000",
    "g1_lead_days": [156, 156],
    "g3_false_escalation_rate": "0.000000",
    "g3_max_escalation_count": 2,
    "g3_max_escalation_share": "0.083333",
    "g3_noisy_false_escalation_rate": "0.000000",
    "g3_stable_false_escalation_rate": "0.000000",
    "stable_latest_below_amber": true,
    "stable_latest_max_probability": "0.017287133838"
  },
  "final_scores_sha256": "38f60dbdc847842f2533934d6b98a8d295400fca61ba54b7ef8604665f69c04b"
}
```

The selected T1, T3, T4 and T5 values are unchanged from the packaged
threshold defaults because the paired procedure found no accepted reason to
move them. The regression suite reruns the reference build from the recorded
settings and compares every final score and the SHA-256 record digest. A
customer calibration must use the same procedure with its own labelled
history and must retain its own record; these synthetic results are not a
claim about customer-book probabilities.
