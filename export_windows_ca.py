"""Export the Windows trusted root certificates to a PEM file for Node.js tools.

Node.js (npx, MCP Inspector) ships its own CA list and ignores the Windows
store, so on networks that inspect HTTPS it fails with "unable to verify the
first certificate". Pointing NODE_EXTRA_CA_CERTS at this file keeps
certificate verification on instead of disabling it.

    python export_windows_ca.py
    set NODE_EXTRA_CA_CERTS=%CD%\\.certs\\windows-roots.pem
    npx @modelcontextprotocol/inspector
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / ".certs" / "windows-roots.pem"


def main() -> None:
    if not hasattr(ssl, "enum_certificates"):
        sys.exit("This helper is for Windows; other systems already share a system CA bundle with Node.")
    TARGET.parent.mkdir(exist_ok=True)
    seen = set()
    blocks = []
    for store in ("ROOT", "CA"):
        for der, encoding, trust in ssl.enum_certificates(store):
            if encoding != "x509_asn" or der in seen:
                continue
            if trust is not True and "1.3.6.1.5.5.7.3.1" not in trust:
                continue
            seen.add(der)
            blocks.append(ssl.DER_cert_to_PEM_cert(der))
    TARGET.write_text("".join(blocks), encoding="ascii")
    print("wrote {0} certificates to {1}".format(len(blocks), TARGET))
    print("set NODE_EXTRA_CA_CERTS={0}".format(TARGET))


if __name__ == "__main__":
    main()
