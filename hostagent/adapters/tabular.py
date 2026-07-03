"""Tabular engine adapter (018 US2, T361) — BentoML CPU service, folded in from serving/bento.

Off-lease CPU: `POST /engines/tabular/predict` relays `{rows}` to the child's `/predict` and returns
the predictions dict verbatim. All behaviour is the shared `BentoCpuAdapter`; this file only names
the engine + its route + run script.
"""
from hostagent.adapters._bento_cpu import BentoCpuAdapter


class TabularAdapter(BentoCpuAdapter):
    engine_id = "tabular"
    verb = "predict"               # POST /engines/tabular/predict -> the child's /predict
    run_script = "tabular_run.sh"
    _pip = "lightgbm joblib"
