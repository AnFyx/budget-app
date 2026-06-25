"""Lanceur de l'app Streamlit, forcé sur la loopback (jamais exposé au réseau).

Usage :
    python run_app.py

Équivaut à streamlit run app.py en imposant --server.address 127.0.0.1, pour
qu'aucune interface réseau autre que la loopback ne serve l'application.
"""

from __future__ import annotations

import sys

from streamlit.web import cli as stcli


def main() -> None:
    sys.argv = [
        "streamlit",
        "run",
        "app.py",
        "--server.address",
        "127.0.0.1",
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
