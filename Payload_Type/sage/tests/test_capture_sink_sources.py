from __future__ import annotations
import hashlib, importlib.util, json, sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[3]
def load(path,name):
 s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s); sys.modules[name]=m; s.loader.exec_module(m); return m
sink=load(ROOT/"skills/sage-eval-gauge/scripts/capture_sink_sources.py","capture_sink_sources_test")
native=load(ROOT/"skills/sage-eval-gauge/scripts/native_inventory.py","native_inventory_sink_test")
redaction=load(ROOT/"skills/sage-eval-gauge/scripts/capture_redaction.py","capture_redaction_sink_test")

def row(table,id):
 v={field:None for field in native.TABLE_FIELDS[table]}; v.update(id=id,operation_id=4); return v
def snap(**tables):
 rows={table:list(tables.get(table,())) for table in native.TABLE_FIELDS}
 return {"schema":"sage-m3-native-inventory-v1","operation_id":4,"complete":True,"failures":[],
  "snapshot_consistency":"single_operation_root_query","query_hashes":{"operation_snapshot":native.SNAPSHOT_QUERY_SHA256},"rows":rows}
def index():
 i=redaction.SecretIndex(); i.register("h:operator","OPERATOR_SECRET","operator"); i.register("h:runtime","RUNTIME_SECRET","runtime"); return i

def test_actual_snapshot_delta_classifies_fields_without_raw_values():
 task=row("task",1); task.update(original_params="",display_params="")
 endtask=dict(task,display_params="OPERATOR_SECRET")
 cred=row("credential",2); cred.update(credential="RUNTIME_SECRET")
 message=row("chat_message",3); message.update(message="RUNTIME_SECRET")
 result=sink.scan_native_delta(snap(task=[task]),snap(task=[endtask],credential=[cred],chat_message=[message]),index())
 assert result["row_change_counts"]["task"]=={"new":0,"changed":1}
 assert any(x["operator_forbidden"] for x in result["secret_matches"])
 runtime=[x for x in result["secret_matches"] if x["category"]=="runtime"]
 assert {x["runtime_context_allowed"] for x in runtime}=={True}
 assert "OPERATOR_SECRET" not in json.dumps(result)

def test_changed_removed_and_bad_snapshot_stay_unknown():
 one=row("response",1); one["response_text"]="x"; two=dict(one,response_text="y")
 result=sink.scan_native_delta(snap(response=[one]),snap(response=[two]),index())
 assert result["unknowns"]==[{"table":"response","id":1,"reason":"intermediate_versions_unobserved"}]
 bad=snap(); bad["rows"]["response"]=[{"id":1,"operation_id":4}]
 with pytest.raises(sink.SinkSourceError): sink.scan_native_delta(bad,snap(),index())

def _export(attrs, *, event=False, status_description=None):
 from ai.observation_tracing import MetadataOnlyExporter
 from opentelemetry.sdk.trace import TracerProvider
 from opentelemetry.sdk.trace.export import SimpleSpanProcessor,SpanExporter,SpanExportResult
 from opentelemetry.trace import Status,StatusCode
 class D(SpanExporter):
  spans=()
  def export(self,spans): self.spans=spans; return SpanExportResult.SUCCESS
 d=D(); p=TracerProvider(); p.add_span_processor(SimpleSpanProcessor(MetadataOnlyExporter(d)))
 with p.get_tracer("dynamic").start_as_current_span("dynamic",attributes=attrs) as span:
  if event: span.add_event("forbidden")
  if status_description is not None: span.set_status(Status(StatusCode.ERROR,status_description))
 from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
 return object.__new__(OTLPSpanExporter)._translate_data(d.spans).SerializeToString()

def test_actual_metadata_export_valid_and_near_match_forbidden():
 good=_export({"llm.token_count.total":7,"openinference.span.kind":"LLM","secret":"OPERATOR_SECRET"})
 assert sink.validate_phoenix_export(good)["known"] is True
 from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
 bad=ExportTraceServiceRequest(); bad.ParseFromString(good)
 span=bad.resource_spans[0].scope_spans[0].spans[0]; span.name="sage.measurement-near"
 span.status.message="OPERATOR_SECRET"; span.trace_state="dynamic=secret"; span.events.add(name="dynamic")
 attribute=span.attributes.add(); attribute.key="dynamic"; attribute.value.string_value="OPERATOR_SECRET"
 assert sink.validate_phoenix_export(bad.SerializeToString())["known"] is False
 unknown=good+b"\x98\x06\x01"
 assert sink.validate_phoenix_export(unknown)["reasons"]==["protobuf_unknown_fields"]
 duplicate=ExportTraceServiceRequest(); duplicate.ParseFromString(good)
 attrs=duplicate.resource_spans[0].resource.attributes; good_attr=type(attrs[0])(); good_attr.CopyFrom(attrs[0]); del attrs[:]
 bad_attr=attrs.add(); bad_attr.key="openinference.project.name"; bad_attr.value.string_value="dynamic"
 attrs.add().CopyFrom(good_attr)
 assert "resource_attributes_forbidden" in sink.validate_phoenix_export(duplicate.SerializeToString())["reasons"]

def test_artifact_append_requires_exact_extent_digest():
 data=b"prefix-OPERATOR_SECRET"; digest=hashlib.sha256(data).hexdigest()
 result=sink.scan_artifact_append("h:path",12,data,digest,index())
 assert result["append_length"]==len(data) and result["secret_matches"][0]["handle"]=="h:operator"
 assert "OPERATOR_SECRET" not in json.dumps(result)
 with pytest.raises(sink.SinkSourceError): sink.scan_artifact_append("h:path",12,data,"0"*64,index())

def test_identical_operator_runtime_value_is_unknown_only_in_trusted_native_sink():
 mixed=redaction.SecretIndex(); mixed.register("h:operator-same","SHARED_LAB_VALUE","operator")
 mixed.register("h:runtime-same","SHARED_LAB_VALUE","runtime")
 response=row("response",8); response["response_text"]="SHARED_LAB_VALUE"
 native_result=sink.scan_native_delta(snap(),snap(response=[response]),mixed)
 assert native_result["secret_matches"]==[]
 overlap=next(x for x in native_result["unknowns"] if x["reason"]=="overlapping_secret_origin")
 assert overlap["handles"]==["h:operator-same","h:runtime-same"]
 data=b"SHARED_LAB_VALUE"; digest=hashlib.sha256(data).hexdigest()
 artifact=sink.scan_artifact_append("h:log",0,data,digest,mixed)
 assert {x["category"] for x in artifact["secret_matches"]}=={"operator","runtime"}
 assert "SHARED_LAB_VALUE" not in json.dumps(native_result)+json.dumps(artifact)
