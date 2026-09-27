#!/usr/bin/env python3
"""Explicit hash-bound operator input loader for evaluator memory."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib,json
from pathlib import Path
from collections.abc import Mapping,Sequence
from typing import Any
import re

class OperatorInputError(ValueError): pass

@dataclass
class LoadedInputs:
    environment:dict[str,str];operator_values:dict[str,str];metadata:list[dict[str,Any]]
    def __repr__(self):return f"LoadedInputs(environment_keys={len(self.environment)}, operator_values={len(self.operator_values)})"

def _pointer(document:Any,pointer:str)->Any:
    if not isinstance(pointer,str) or not pointer.startswith("/"):raise OperatorInputError("selected field invalid")
    value=document
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])",token):raise OperatorInputError("selected field invalid")
        token=token.replace("~1","/").replace("~0","~")
        if isinstance(value,Mapping) and token in value:value=value[token]
        elif isinstance(value,list) and token.isdigit() and int(token)<len(value):value=value[int(token)]
        else:return None
    return value

def _dotenv(raw:bytes)->dict[str,str]:
    result={}
    try:lines=raw.decode().splitlines()
    except UnicodeError:raise OperatorInputError("input source invalid") from None
    for line in lines:
        line=line.strip()
        if not line or line.startswith("#"):continue
        if line.startswith("export "):line=line[7:].strip()
        if "=" not in line:raise OperatorInputError("input source invalid")
        key,value=line.split("=",1);key=key.strip();value=value.strip()
        if not key or key in result:raise OperatorInputError("input source invalid")
        if len(value)>=2 and value[:1]==value[-1:] and value[0] in "\"'":value=value[1:-1]
        result[key]=value
    return result

def load_inputs(bindings:Sequence[Mapping[str,Any]],*,environ:Mapping[str,str])->LoadedInputs:
    if not isinstance(bindings,Sequence) or isinstance(bindings,(str,bytes)) or not isinstance(environ,Mapping):raise OperatorInputError("input bindings invalid")
    environment={};operator_values={};metadata=[];destinations=set()
    for binding in bindings:
        try:
            path=Path(binding["path"]);digest=binding["sha256"];fmt=binding["format"];fields=binding["fields"]
            raw=path.read_bytes()
            if not path.is_absolute() or hashlib.sha256(raw).hexdigest()!=digest or fmt not in {"dotenv","json"} or not isinstance(fields,list):raise OperatorInputError("input source invalid")
            document=json.loads(raw) if fmt=="json" else _dotenv(raw)
        except OperatorInputError:raise
        except Exception:raise OperatorInputError("input source invalid") from None
        for spec in fields:
            if not isinstance(spec,Mapping):raise OperatorInputError("selected field invalid")
            allowed={"field","environment","secret_handle","required"}
            if not set(spec)<=allowed or "field" not in spec:raise OperatorInputError("selected field invalid")
            field=spec["field"];destination=spec.get("environment");handle=spec.get("secret_handle");required=spec.get("required",True)
            if type(required)is not bool or destination is not None and (not isinstance(destination,str) or not destination) or handle is not None and (not isinstance(handle,str) or not handle):raise OperatorInputError("selected field invalid")
            value=_pointer(document,field) if fmt=="json" else document.get(field)
            if value is not None and not isinstance(value,str):raise OperatorInputError("selected field invalid")
            effective=environ.get(destination,value) if destination is not None else value
            if effective is not None and not isinstance(effective,str):raise OperatorInputError("selected field invalid")
            if destination is not None and (destination in destinations):raise OperatorInputError("environment destination conflicts")
            if destination is not None:destinations.add(destination)
            if handle is not None and handle in operator_values:raise OperatorInputError("operator handle duplicated")
            if effective is None or effective=="":
                if required:raise OperatorInputError("required selected value missing")
                metadata.append({"source_sha256":digest,"field":field,"selected_value_present":False,"handle":handle});continue
            if destination is not None:environment[destination]=effective
            if handle is not None:operator_values[handle]=effective
            metadata.append({"source_sha256":digest,"field":field,"selected_value_present":True,"handle":handle})
    return LoadedInputs(environment,operator_values,metadata)

def main()->int:print("DRY RUN: import load_inputs with explicit bindings.");return 0
if __name__=="__main__":raise SystemExit(main())
