from __future__ import annotations
import hashlib,importlib.util,json,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[3];SCRIPT=ROOT/"skills/sage-eval-gauge/scripts/capture_operator_inputs.py"
s=importlib.util.spec_from_file_location("capture_operator_inputs_test",SCRIPT);loader=importlib.util.module_from_spec(s);sys.modules[s.name]=loader;s.loader.exec_module(loader)
def binding(path,fmt,fields):return {"path":str(path),"sha256":hashlib.sha256(path.read_bytes()).hexdigest(),"format":fmt,"fields":fields}
def test_environment_wins_and_secret_only_stays_memory(tmp_path):
 p=tmp_path/"in.env";p.write_text("MODEL=file-value\nCALLBACK=secret-only\n")
 loaded=loader.load_inputs([binding(p,"dotenv",[{"field":"MODEL","environment":"MODEL_ID","secret_handle":"model"},{"field":"CALLBACK","secret_handle":"callback"}])],environ={"MODEL_ID":"environment-value"})
 assert loaded.environment=={"MODEL_ID":"environment-value"} and loaded.operator_values=={"model":"environment-value","callback":"secret-only"}
 assert "file-value" not in repr(loaded)
def test_hash_missing_field_duplicate_and_exception_do_not_leak(tmp_path):
 p=tmp_path/"in.env";p.write_text("TOKEN=SYNTHETIC_SECRET\n");b=binding(p,"dotenv",[{"field":"TOKEN","secret_handle":"h"}]);p.write_text("TOKEN=changed\n")
 with pytest.raises(loader.OperatorInputError) as error:loader.load_inputs([b],environ={})
 assert "SYNTHETIC_SECRET" not in str(error.value)
 p.write_text("TOKEN=SYNTHETIC_SECRET\n");d=binding(p,"dotenv",[{"field":"MISSING","environment":"X"}])
 with pytest.raises(loader.OperatorInputError):loader.load_inputs([d],environ={})
 dup=binding(p,"dotenv",[{"field":"TOKEN","secret_handle":"h"},{"field":"TOKEN","secret_handle":"h"}])
 with pytest.raises(loader.OperatorInputError):loader.load_inputs([dup],environ={})
def test_json_pointer_escape_and_nonstring_reject(tmp_path):
 p=tmp_path/"in.json";p.write_text(json.dumps({"a/b":{"t~k":"value"},"bad":3}))
 loaded=loader.load_inputs([binding(p,"json",[{"field":"/a~1b/t~0k","environment":"DEST"}])],environ={})
 assert loaded.environment=={"DEST":"value"}
 with pytest.raises(loader.OperatorInputError):loader.load_inputs([binding(p,"json",[{"field":"/bad","environment":"DEST"}])],environ={})
