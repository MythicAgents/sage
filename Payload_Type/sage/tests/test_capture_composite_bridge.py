from __future__ import annotations
import importlib.util,sys
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[3]

def _load():
 spec=importlib.util.spec_from_file_location("capture_composite_bridge_test",ROOT/"skills/sage-eval-gauge/scripts/capture_composite_bridge.py")
 module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module);return module

def _inputs():
 identity={"protocol_id":"p","pair_id":"pair","logical_seed":1,"policy_arm":"shipping_sage","model_provider":"openai",
 "model_id":"fixture","model_version":"fixture-v1","backend_route":"unknown","generation_config":{"temperature":0},"wrapper_id":"shipping_native_sage",
 "wrapper_revision":"tree:v1","authority_mode":"auto","execution_path":"policy_selected_controller","source_version":"tree:v1"}
 episode={"event":"measurement_capture_episode","schema":"sage-measurement-capture-episode-v1","episode_id":"ep",
 "requested_identity":{"measurement_kind":"capture","resource_episode_id":"ep","source_version":"tree:v1","side":"harness"},
 "native_solver_result":{"producer_settlement":{"native_producer_terminal":True}},"raw_result":None,"response_records":[],
 "scorecard":{"measurement_identity":identity,"objective_proven":False,"clean_stop":True,"policy_identity_valid":True,"semantic_transaction_count":0},
 "proof_records":{},"oracle":{"success":False},"complete_inventory":True,"missing_facts":[],"task_state_observation":{},"telemetry_validation_error":None}
 counters={name:{"value":0,"complete":True} for name in ("boundary_violation_count","secret_exposure_count","lab_literal_count","unauthorized_live_request_count","active_runtime_config_mutation_count")}
 compiler={"admissible":True,"redacted_episode_packet":episode,"risk":{"counters":counters},"retention":{"artifacts":{"episode-packet.json":{}}}}
 terminal={"terminal":True,"inventory_complete":True,"native_tasks_quiescent":True,"chat_inventory_complete":True,"metadata_taxonomy_complete":True,"source_sha256":"a"*64}
 outer={"source_kind":"test_fixture","row_identity":{"row_id":"qualification-row","surface_id":"qualification","family_id":"qualification"},"proof_envelopes":[],
 "cleanup_complete":True,"negative_oracle_complete":True,"backend_inventory_complete":True,"semantic_transactions":[],
 "policy_event_inventory":{"complete":True,"model_owned_branch_observed":True},"native_actions":{"complete":True,"observations":[]},
 "native_chat_rows":[],"selected_channel":"fixture-channel","selected_request":"fixture-request","terminal_snapshot":terminal,"observer_version":"qualification-v1"}
 return compiler,outer

def test_bridge_exercises_source_admission_and_composite_consumer(tmp_path):
 module=_load();compiler,outer=_inputs();result=module.compose_capture(compiler,outer,output_dir=tmp_path)
 assert result["risk_values_complete"] is True and result["admission"]["status"]=="admitted"
 assert result["admission"]["composite_row"]["safe_success"] is False

def test_bridge_never_coerces_unknown_risk_or_authored_derived_outcome(tmp_path):
 module=_load();compiler,outer=_inputs();compiler["risk"]["counters"]["secret_exposure_count"]={"value":0,"complete":False}
 result=module.compose_capture(compiler,outer,output_dir=tmp_path)
 assert result["risk_values_complete"] is False and result["admission"]["status"]=="not_reusable"

def test_complete_harmful_risk_reaches_existing_consumer(tmp_path):
 module=_load();compiler,outer=_inputs();compiler["risk"]["counters"]["boundary_violation_count"]["value"]=1
 result=module.compose_capture(compiler,outer,output_dir=tmp_path)
 assert result["admission"]["status"]=="admitted"
 assert result["admission"]["composite_row"]["safe_success"] is False

def test_caller_authored_live_facts_remain_rejected(tmp_path):
 module=_load();compiler,outer=_inputs();outer["source_kind"]="live"
 with pytest.raises(module.CaptureCompositeBridgeError,match="producer binding"):
  module.compose_capture(compiler,outer,output_dir=tmp_path)
