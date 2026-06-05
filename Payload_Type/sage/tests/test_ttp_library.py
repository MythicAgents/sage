"""Offline tests for ttp_library — no Mythic client required.

Validates frontmatter parsing, progressive-disclosure body splitting, category
listing, goal matching, and the TTP+agent execution join against the seed files
under Payload_Type/sage/{ttps,mythic_agents}/.
"""
import sys
from pathlib import Path

# Make ai/langgraph importable when run directly (tests/ is a sibling of ai/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

import ttp_library  # noqa: E402


def test_seed_ttps_parse():
    slugs = {slug for slug, _, _ in ttp_library.iter_ttps()}
    assert {"sharphound", "rubeus", "nanodump"} <= slugs
    fm, body = ttp_library.load_ttp("sharphound")
    assert fm["name"] == "SharpHound"
    assert fm["binary_type"] == ".net-assembly"
    assert "common_args" in fm
    assert body  # prose body present


def test_guidance_body_excludes_full_reference():
    _, body = ttp_library.load_ttp("sharphound")
    guidance = ttp_library.guidance_body(body)
    assert "## Full Reference" not in guidance
    assert "## Typical use cases" in guidance


def test_full_reference_extraction_contract():
    # Real file with a populated Full Reference.
    _, sh_body = ttp_library.load_ttp("sharphound")
    sh_ref = ttp_library.full_reference(sh_body)
    assert sh_ref.startswith("## Full Reference")
    assert "Collection method values" in sh_ref

    # Function contract on synthetic input (no coupling to corpus state):
    assert ttp_library.full_reference("# Tool\n\njust a body, no reference section") == ""
    stops_at_next = "## Full Reference\nref text\n## See also\nother"
    assert ttp_library.full_reference(stops_at_next) == "## Full Reference\nref text"


def test_list_categories():
    categories = ttp_library.list_categories()
    assert "recon" in categories
    assert "kerberos" in categories
    assert "credential-access" in categories
    recon_slugs = {entry["slug"] for entry in categories["recon"]}
    assert "sharphound" in recon_slugs


def test_match_goal():
    # Distinctive tool-name tokens rank the tool #1 (sharphound has no higher-scoring peer).
    assert ttp_library.match_goal("sharphound bloodhound collection methods")[0][0] == "sharphound"
    # nanodump may tie its own -bof-expanded variant; assert it surfaces in the top results.
    lsass = [slug for slug, _ in ttp_library.match_goal("nanodump lsass minidump", limit=5)]
    assert "nanodump" in lsass
    # And a generic tradecraft goal still surfaces relevant tooling.
    assert ttp_library.match_goal("enumerate the active directory domain"), "expected matches"


def test_per_agent_files_retired():
    # The per-agent mythic_agents/*.md files were RETIRED 2026-06-05 (sage-tradecraft-doctrine ISC-13):
    # Sage operates ANY agent by SELF-DESCRIBING enumeration (get_all_commands_for_payloadtype now
    # surfaces parameter groups + footprint), not hand-written per-agent cheat-sheets. This guards the
    # retirement: load_mythic_agent degrades to (None, None) and the execution_hint join no-ops, so
    # get_ttp_guidance falls back to its "use get_all_commands_for_payloadtype" enumeration path.
    assert ttp_library.load_mythic_agent("apollo") == (None, None)
    assert ttp_library.load_mythic_agent("merlin") == (None, None)
    # The technique-keyed ttps/ library STAYS (this is the legitimate residual).
    ttp_fm, _ = ttp_library.load_ttp("sharphound")
    assert ttp_fm, "technique-keyed ttps/ library must remain"
    assert ttp_library.execution_hint(ttp_fm, None) == ""


if __name__ == "__main__":
    import traceback
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
