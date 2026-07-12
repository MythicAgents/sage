#!/usr/bin/env python3
"""Probe whether a KDC advertises PKINIT PA-DATA without using a certificate.

The probe sends an AS-REQ without pre-authentication and parses the
KDC_ERR_PREAUTH_REQUIRED METHOD-DATA list. PA type 16 is PA-PK-AS-REQ.
This is not a proof that the KDC has a usable KDC-authentication certificate.
"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
from datetime import datetime, timedelta, timezone

from pyasn1.codec.der import decoder, encoder
from pyasn1.type import char, namedtype, tag, univ, useful


APP = tag.tagClassApplication
CTX = tag.tagClassContext
CONS = tag.tagFormatConstructed


def explicit(num: int) -> tag.Tag:
    return tag.Tag(CTX, CONS, num)


class KerberosString(char.GeneralString):
    pass


class PrincipalName(univ.Sequence):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType("name-type", univ.Integer().subtype(explicitTag=explicit(0))),
        namedtype.NamedType(
            "name-string",
            univ.SequenceOf(componentType=KerberosString()).subtype(explicitTag=explicit(1)),
        ),
    )


class KDCReqBody(univ.Sequence):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType("kdc-options", univ.BitString().subtype(explicitTag=explicit(0))),
        namedtype.OptionalNamedType("cname", PrincipalName().subtype(explicitTag=explicit(1))),
        namedtype.NamedType("realm", KerberosString().subtype(explicitTag=explicit(2))),
        namedtype.OptionalNamedType("sname", PrincipalName().subtype(explicitTag=explicit(3))),
        namedtype.OptionalNamedType("from", useful.GeneralizedTime().subtype(explicitTag=explicit(4))),
        namedtype.NamedType("till", useful.GeneralizedTime().subtype(explicitTag=explicit(5))),
        namedtype.OptionalNamedType("rtime", useful.GeneralizedTime().subtype(explicitTag=explicit(6))),
        namedtype.NamedType("nonce", univ.Integer().subtype(explicitTag=explicit(7))),
        namedtype.NamedType(
            "etype",
            univ.SequenceOf(componentType=univ.Integer()).subtype(explicitTag=explicit(8)),
        ),
    )


class ASReq(univ.Sequence):
    tagSet = univ.Sequence.tagSet.tagExplicitly(tag.Tag(APP, CONS, 10))
    componentType = namedtype.NamedTypes(
        namedtype.NamedType("pvno", univ.Integer().subtype(explicitTag=explicit(1))),
        namedtype.NamedType("msg-type", univ.Integer().subtype(explicitTag=explicit(2))),
        namedtype.NamedType("req-body", KDCReqBody().subtype(explicitTag=explicit(4))),
    )


class PAData(univ.Sequence):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType("padata-type", univ.Integer().subtype(explicitTag=explicit(1))),
        namedtype.NamedType("padata-value", univ.OctetString().subtype(explicitTag=explicit(2))),
    )


class MethodData(univ.SequenceOf):
    componentType = PAData()


class KRBError(univ.Sequence):
    tagSet = univ.Sequence.tagSet.tagExplicitly(tag.Tag(APP, CONS, 30))
    componentType = namedtype.NamedTypes(
        namedtype.NamedType("pvno", univ.Integer().subtype(explicitTag=explicit(0))),
        namedtype.NamedType("msg-type", univ.Integer().subtype(explicitTag=explicit(1))),
        namedtype.OptionalNamedType("ctime", useful.GeneralizedTime().subtype(explicitTag=explicit(2))),
        namedtype.OptionalNamedType("cusec", univ.Integer().subtype(explicitTag=explicit(3))),
        namedtype.NamedType("stime", useful.GeneralizedTime().subtype(explicitTag=explicit(4))),
        namedtype.NamedType("susec", univ.Integer().subtype(explicitTag=explicit(5))),
        namedtype.NamedType("error-code", univ.Integer().subtype(explicitTag=explicit(6))),
        namedtype.OptionalNamedType("crealm", KerberosString().subtype(explicitTag=explicit(7))),
        namedtype.OptionalNamedType("cname", PrincipalName().subtype(explicitTag=explicit(8))),
        namedtype.NamedType("realm", KerberosString().subtype(explicitTag=explicit(9))),
        namedtype.NamedType("sname", PrincipalName().subtype(explicitTag=explicit(10))),
        namedtype.OptionalNamedType("e-text", KerberosString().subtype(explicitTag=explicit(11))),
        namedtype.OptionalNamedType("e-data", univ.OctetString().subtype(explicitTag=explicit(12))),
    )


ERROR_NAMES = {
    25: "KDC_ERR_PREAUTH_REQUIRED",
    52: "KRB_ERR_RESPONSE_TOO_BIG",
}

PA_NAMES = {
    2: "PA-ENC-TIMESTAMP",
    11: "PA-ETYPE-INFO",
    16: "PA-PK-AS-REQ",
    17: "PA-PK-AS-REP",
    19: "PA-ETYPE-INFO2",
    128: "PA-PAC-REQUEST",
}


def _set_tagged(seq: univ.Sequence, name: str, value: object) -> None:
    seq.setComponentByName(name, seq.getComponentByName(name).clone(value))


def _principal(schema: PrincipalName, name_type: int, parts: list[str]) -> PrincipalName:
    principal = schema.clone()
    _set_tagged(principal, "name-type", name_type)
    names = principal.getComponentByName("name-string").clone()
    for part in parts:
        names.append(part)
    principal.setComponentByName("name-string", names)
    return principal


def _generalized_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%SZ")


def build_as_req(user: str, realm: str) -> bytes:
    req = ASReq()
    _set_tagged(req, "pvno", 5)
    _set_tagged(req, "msg-type", 10)

    body = req.getComponentByName("req-body").clone()
    _set_tagged(body, "kdc-options", univ.BitString(binValue="0" * 32))
    body.setComponentByName("cname", _principal(body.getComponentByName("cname"), 1, [user]))
    _set_tagged(body, "realm", realm.upper())
    body.setComponentByName("sname", _principal(body.getComponentByName("sname"), 2, ["krbtgt", realm.upper()]))
    _set_tagged(body, "till", _generalized_time(datetime.now(timezone.utc) + timedelta(hours=10)))
    _set_tagged(body, "nonce", secrets.randbits(31))
    etypes = body.getComponentByName("etype").clone()
    for etype in (18, 17, 23):
        etypes.append(etype)
    body.setComponentByName("etype", etypes)

    req.setComponentByName("req-body", body)
    return encoder.encode(req)


def send_udp(kdc: str, port: int, packet: bytes, timeout: float) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (kdc, port))
        data, _addr = sock.recvfrom(65535)
        return data


def send_tcp(kdc: str, port: int, packet: bytes, timeout: float) -> bytes:
    framed = len(packet).to_bytes(4, "big") + packet
    with socket.create_connection((kdc, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(framed)
        header = sock.recv(4)
        if len(header) != 4:
            raise RuntimeError("short TCP length header from KDC")
        size = int.from_bytes(header, "big")
        chunks = []
        remaining = size
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise RuntimeError("short TCP response from KDC")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


def parse_krb_error(data: bytes) -> dict[str, object]:
    err, _rest = decoder.decode(data, asn1Spec=KRBError())
    code = int(err.getComponentByName("error-code"))
    text = ""
    e_text = err.getComponentByName("e-text")
    if e_text is not None and e_text.hasValue():
        text = str(e_text)

    padata_types: list[int] = []
    e_data = err.getComponentByName("e-data")
    if e_data is not None and e_data.hasValue():
        try:
            method_data, _rest = decoder.decode(bytes(e_data), asn1Spec=MethodData())
            for item in method_data:
                padata_types.append(int(item.getComponentByName("padata-type")))
        except Exception as exc:  # Keep the raw KRB error useful even if METHOD-DATA parsing fails.
            text = f"{text}; method-data-parse-error={exc}" if text else f"method-data-parse-error={exc}"

    return {
        "error_code": code,
        "error_name": ERROR_NAMES.get(code, f"KRB_ERROR_{code}"),
        "e_text": text,
        "padata_types": sorted(set(padata_types)),
        "padata_names": [PA_NAMES.get(value, f"PA-{value}") for value in sorted(set(padata_types))],
        "pkinit_advertised": 16 in padata_types,
        "usable_pkinit_verified": False,
        "probe_scope": "method-data-only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kdc", default="10.4.10.12")
    parser.add_argument("--port", type=int, default=88)
    parser.add_argument("--realm", default="ESSOS.LOCAL")
    parser.add_argument("--user", default="administrator")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--tcp", action="store_true", help="Use TCP only")
    args = parser.parse_args()

    packet = build_as_req(args.user, args.realm)
    transport = "tcp" if args.tcp else "udp"
    try:
        response = send_tcp(args.kdc, args.port, packet, args.timeout) if args.tcp else send_udp(args.kdc, args.port, packet, args.timeout)
        parsed = parse_krb_error(response)
        if parsed["error_code"] == 52 and not args.tcp:
            response = send_tcp(args.kdc, args.port, packet, args.timeout)
            parsed = parse_krb_error(response)
            transport = "tcp"
        result = {
            "ok": True,
            "kdc": args.kdc,
            "port": args.port,
            "realm": args.realm.upper(),
            "principal": args.user,
            "transport": transport,
            **parsed,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "kdc": args.kdc,
            "port": args.port,
            "realm": args.realm.upper(),
            "principal": args.user,
            "transport": transport,
            "error": str(exc),
            "pkinit_advertised": False,
            "usable_pkinit_verified": False,
            "probe_scope": "method-data-only",
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok") and result.get("pkinit_advertised") else 2


if __name__ == "__main__":
    raise SystemExit(main())
