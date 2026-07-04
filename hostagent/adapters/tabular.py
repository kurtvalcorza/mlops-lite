"""Tabular engine adapter (018 US2, T361; 020 T410 — slim child launch).

Off-lease CPU: `POST /engines/tabular/predict` relays `{rows}` to the child's `/predict` and
returns the predictions dict verbatim. All behaviour is the shared `ChildCpuAdapter`; this file
only names the engine + its route + run script.
"""
from hostagent.adapters._child_cpu import ChildCpuAdapter


class TabularAdapter(ChildCpuAdapter):
    engine_id = "tabular"
    verb = "predict"               # POST /engines/tabular/predict -> the child's /predict
    run_script = "tabular_run.sh"
