# MARE ETL

This package is a new subsystem for v3.2.

It builds `feature_store.db` independently from the existing simulation result databases.

Stages:

- `extract.py`
- `transform.py`
- `load.py`

Important:

- Adapter method signatures are fixed.
- Real PMS and OTA API details still require engineering credentials and implementation.
- DB views are not required for ETL or ML inference.

