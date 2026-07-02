// Browser script for the Sage `state` command.
//
// The command appends a machine-readable JSON payload to its output behind the marker
// "@@@SAGE_STATE_JSON@@@" (see container/agent_functions/state.py :: _render_ledger). This script parses
// that payload and renders the engagement-state ledger as an interactive Mythic table (per-hop status
// coloring, copy-icons on identifiers), plus tabs for the notice, the agent-view text, and details.
//
// Actions with no payload (wipe, usage/errors) contain no marker, so we fall back to plaintext — the same
// text the operator would see with the browser script disabled.
function(task, responses){
    const MARKER = "@@@SAGE_STATE_JSON@@@";

    let combined = "";
    for (let i = 0; i < responses.length; i++){
        combined += responses[i];
    }

    // Errored task or nothing structured to render -> show the raw text.
    if (task.status !== undefined && task.status !== null &&
        String(task.status).toLowerCase().indexOf("error") !== -1){
        return {"plaintext": combined};
    }
    const markerIdx = combined.indexOf(MARKER);
    if (markerIdx === -1){
        return {"plaintext": combined};
    }

    let payload;
    try {
        payload = JSON.parse(combined.substring(markerIdx + MARKER.length).trim());
    } catch (e){
        return {"plaintext": combined};
    }

    // Status -> row color. Keys are compared case-insensitively.
    const statusColors = {
        "achieved": "#1b5e20",  // green
        "failed":   "#7f1d1d",  // red
        "blocked":  "#8a5a00",  // amber
        "pending":  "#37474f"   // slate
    };

    const headers = [
        {"plaintext": "actions",  "type": "button", "width": 110, "disableSort": true},
        {"plaintext": "#",        "type": "number", "width": 50},
        {"plaintext": "hop",      "type": "string", "fillWidth": true},
        {"plaintext": "effect",   "type": "string", "width": 300},
        {"plaintext": "status",   "type": "string", "width": 110},
        {"plaintext": "prov",     "type": "string", "width": 90},
        {"plaintext": "task",     "type": "number", "width": 80},
        {"plaintext": "cb",       "type": "number", "width": 70},
        {"plaintext": "evidence", "type": "string", "width": 300}
    ];

    // A per-row action menu that re-tasks the `state` command. Buttons pass a dictionary keyed on the STABLE
    // hop label (id or technique:target) — never the positional row number — so an edit still targets the
    // right hop after rows are renumbered by a prior delete. Requires supported_ui_features = ["state:edit"].
    function editItem(name, action, label, status, icon, iconColor, confirm){
        const params = {"action": action, "hop": label};
        if (status){ params["status"] = status; }
        const item = {
            "name": name, "type": "task", "ui_feature": "state:edit",
            "parameters": params, "openDialog": false, "getConfirmation": !!confirm,
            "startIcon": icon, "startIconColor": iconColor, "hoverText": name
        };
        if (confirm){ item["acceptText"] = "Confirm"; }
        return item;
    }

    const hops = payload.hops || [];
    const rows = [];
    for (let i = 0; i < hops.length; i++){
        const h = hops[i];
        const status = (h.status === undefined || h.status === null) ? "" : String(h.status);
        const color = statusColors[status.toLowerCase()];
        const hasTask = (h.task !== undefined && h.task !== null && h.task !== "");
        const hasCb = (h.cb !== undefined && h.cb !== null && h.cb !== "");
        const label = h.label || "";

        // Full-row text for the "Copy row" action (Mythic copyIcon can only copy a cell's own text, so a
        // whole-row copy is offered as a string button that opens a modal with the joined fields).
        const rowText =
            "#" + ((h.n === undefined || h.n === null) ? "" : String(h.n)) +
            " | hop=" + label +
            " | effect=" + (h.effect || "") +
            " | status=" + (status || "-") +
            " | prov=" + (h.prov || "-") +
            " | task=" + (hasTask ? String(h.task) : "-") +
            " | cb=" + (hasCb ? String(h.cb) : "-") +
            " | evidence=" + (h.evidence || "");

        const row = {
            "#":        {"plaintext": (h.n === undefined || h.n === null) ? "" : String(h.n)},
            "hop":      {"plaintext": label, "copyIcon": true},
            "effect":   {"plaintext": h.effect || "", "copyIcon": !!(h.effect), "startIcon": "key", "startIconColor": "gold"},
            "status":   {"plaintext": status || "-", "copyIcon": !!status},
            "prov":     {"plaintext": (h.prov === undefined || h.prov === null || h.prov === "") ? "-" : String(h.prov)},
            "task":     {"plaintext": hasTask ? String(h.task) : "-", "copyIcon": hasTask},
            "cb":       {"plaintext": hasCb ? String(h.cb) : "-", "copyIcon": hasCb},
            "evidence": {"plaintext": h.evidence || "", "copyIcon": !!(h.evidence)},
            "actions":  {"button": {
                "name": "Edit", "type": "menu", "hoverText": "Delete, set status, or copy this hop",
                "value": [
                    editItem("Delete hop", "remove", label, null, "delete", "red", true),
                    editItem("Set achieved", "set", label, "achieved", "list", "green", false),
                    editItem("Set failed",   "set", label, "failed",   "list", "red", false),
                    editItem("Set blocked",  "set", label, "blocked",  "list", "gold", false),
                    editItem("Set pending",  "set", label, "pending",  "list", "gray", false),
                    {"name": "Copy row", "type": "string", "value": rowText,
                     "title": "Hop row (select to copy)", "startIcon": "list", "hoverText": "View/copy the full row"}
                ]
            }}
        };
        if (color){
            row["rowStyle"] = {"backgroundColor": color, "color": "white"};
        }
        rows.push(row);
    }

    const titleBits = ["Engagement State: " + (payload.engagement_id || "?")];
    if (payload.hop_count !== undefined && payload.hop_count !== null){
        titleBits.push(payload.hop_count + " hop(s)");
    }
    const tableTitle = titleBits.join("  •  ");

    // Hops table first so it is the default view (the whole point of the browser script).
    const tabs = [{
        "title": "Hops (" + hops.length + ")",
        "content": {"table": [{"headers": headers, "rows": rows, "title": tableTitle}]}
    }];

    // Confirmation / reconcile notes for edit actions (remove/set/objective/reconcile).
    if (payload.notice){
        tabs.push({"title": "Result", "content": {"plaintext": payload.notice}});
    }

    // Exactly what gets injected into the model each turn. Kept as verbatim monospace text ON PURPOSE: its
    // value is being byte-faithful to what the model receives, so it is not reformatted.
    if (payload.agent_view){
        tabs.push({"title": "Agent View", "content": {"plaintext": payload.agent_view}});
    }

    // Objective / provenance / ledger path. MUST be plaintext, not a table: Mythic renders a table
    // plaintext cell inside a <pre style="white-space:pre"> (and strips newlines), so a table cell can
    // NEVER word-wrap — cellStyle only styles the wrapping <div>, not the <pre>. The objective is long, so
    // plaintext is the only content type that wraps it. (Confirmed in MythicReactUI ResponseDisplayTable.)
    let details = "";
    function addDetail(key, val){
        if (val === undefined || val === null || val === "") { return; }
        details += key + ": " + String(val) + "\n";
    }
    addDetail("objective", payload.objective);
    addDetail("objective_source", payload.objective_source);
    addDetail("engagement_id", payload.engagement_id);
    addDetail("hop_count", payload.hop_count);
    addDetail("path", payload.path);
    if (details){
        tabs.push({"title": "Details", "content": {"plaintext": details}});
    }

    return {"tabs": tabs};
}
