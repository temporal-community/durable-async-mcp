#!/usr/bin/env python3
# ABOUTME: Generates client-architecture.excalidraw diagram.
# Produces architecture + sequence diagrams for client-side Temporal workflow design.

import json

_s = [1000]


def ns():
    _s[0] += 7
    return _s[0]


BASE = {
    "angle": 0,
    "fillStyle": "solid",
    "strokeWidth": 2,
    "strokeStyle": "solid",
    "roughness": 0,
    "opacity": 100,
    "groupIds": [],
    "frameId": None,
    "isDeleted": False,
    "boundElements": [],
    "updated": 1748000000000,
    "link": None,
    "locked": False,
}


def R(eid, x, y, w, h, bg="#ffffff", stroke="#1e1e1e", sw=2, dashed=False):
    return {
        **BASE,
        "id": eid, "type": "rectangle",
        "x": x, "y": y, "width": w, "height": h,
        "backgroundColor": bg, "strokeColor": stroke,
        "strokeWidth": sw,
        "strokeStyle": "dashed" if dashed else "solid",
        "roundness": {"type": 3},
        "seed": ns(), "version": 1, "versionNonce": ns(),
    }


def T(eid, x, y, w, h, txt, size=13, align="center", color="#1e1e1e"):
    return {
        **BASE,
        "id": eid, "type": "text",
        "x": x, "y": y, "width": w, "height": h,
        "backgroundColor": "transparent", "strokeColor": color,
        "strokeWidth": 1, "roundness": None,
        "seed": ns(), "version": 1, "versionNonce": ns(),
        "text": txt, "fontSize": size,
        "fontFamily": 1,
        "textAlign": align, "verticalAlign": "middle",
        "containerId": None, "originalText": txt, "lineHeight": 1.25,
    }


def CODE(eid, x, y, w, h, txt, size=11, color="#495057"):
    return {
        **BASE,
        "id": eid, "type": "text",
        "x": x, "y": y, "width": w, "height": h,
        "backgroundColor": "transparent", "strokeColor": color,
        "strokeWidth": 1, "roundness": None,
        "seed": ns(), "version": 1, "versionNonce": ns(),
        "text": txt, "fontSize": size,
        "fontFamily": 3,   # monospace
        "textAlign": "left", "verticalAlign": "top",
        "containerId": None, "originalText": txt, "lineHeight": 1.35,
    }


def A(eid, x1, y1, x2, y2, bidir=False, dashed=False, color="#1e1e1e", sw=2):
    dx, dy = x2 - x1, y2 - y1
    return {
        **BASE,
        "id": eid, "type": "arrow",
        "x": x1, "y": y1, "width": abs(dx), "height": abs(dy),
        "backgroundColor": "transparent", "strokeColor": color,
        "strokeWidth": sw, "roughness": 0,
        "strokeStyle": "dashed" if dashed else "solid",
        "roundness": {"type": 2},
        "seed": ns(), "version": 1, "versionNonce": ns(),
        "points": [[0, 0], [dx, dy]],
        "lastCommittedPoint": None,
        "startBinding": None, "endBinding": None,
        "startArrowhead": "arrow" if bidir else None,
        "endArrowhead": "arrow",
    }


def L(eid, x, y, dx, dy, color="#ced4da", sw=1, dashed=True):
    return {
        **BASE,
        "id": eid, "type": "line",
        "x": x, "y": y, "width": abs(dx), "height": abs(dy),
        "backgroundColor": "transparent", "strokeColor": color,
        "strokeWidth": sw, "roughness": 0,
        "strokeStyle": "dashed" if dashed else "solid",
        "roundness": None,
        "seed": ns(), "version": 1, "versionNonce": ns(),
        "points": [[0, 0], [dx, dy]],
        "lastCommittedPoint": None,
        "startBinding": None, "endBinding": None,
        "startArrowhead": None, "endArrowhead": None,
    }


E = []


# ============================================================
# ARCHITECTURE DIAGRAM  (y: 0 – 730)
# ============================================================

E.append(T("a_h", 0, 0, 1560, 36,
           "Architecture: Temporal Worker as Client Backend",
           size=22, color="#1864ab"))

# --- UI Layer ---
E.append(R("ui_out", 10, 48, 200, 250, bg="#f1f3f5", stroke="#495057", sw=2))
E.append(T("ui_lbl", 10, 48, 200, 26, "UI Layer", size=14, color="#495057"))
E.append(T("ui_note", 10, 75, 200, 18, "any / many", size=11, color="#868e96"))
E.append(R("ui_chat", 26, 102, 168, 42, bg="#dee2e6", stroke="#495057"))
E.append(T("ui_chat_t", 26, 102, 168, 42, "Chat (CLI / web)", size=12))
E.append(R("ui_mob",  26, 156, 168, 42, bg="#dee2e6", stroke="#495057"))
E.append(T("ui_mob_t", 26, 156, 168, 42, "Web / Mobile App", size=12))
E.append(R("ui_slk",  26, 210, 168, 42, bg="#dee2e6", stroke="#495057"))
E.append(T("ui_slk_t", 26, 210, 168, 42, "Slack / etc.", size=12))

E.append(A("ui_arr", 210, 173, 240, 220, bidir=True, color="#495057", sw=2))
E.append(T("ui_arr_t", 212, 130, 175, 38,
           "signals / queries\n(start · user_decision)\n← elicitation prompts",
           size=11, color="#495057"))

# --- Temporal Worker Process ---
E.append(R("tw_out", 240, 48, 860, 670, bg="#fff9db", stroke="#f08c00", sw=3))
E.append(T("tw_lbl", 252, 52, 560, 26, "Temporal Worker Process", size=15,
           color="#f08c00", align="left"))
E.append(T("tw_note", 252, 74, 560, 18,
           "handles N concurrent TaskTracker workflows  ·  activity class holds shared mcp_client",
           size=11, color="#868e96", align="left"))

# Global mcp_client
E.append(R("mc_box", 262, 102, 820, 86, bg="#d3f9d8", stroke="#2f9e44", sw=2))
E.append(T("mc_lbl", 270, 104, 300, 20, "Global: mcp_client", size=13,
           color="#2f9e44", align="left"))
E.append(T("mc_sub", 270, 122, 400, 18,
           "fastmcp.Client  —  one connection, shared by all activities via activity class",
           size=11, color="#2f9e44", align="left"))
E.append(CODE("mc_code", 270, 142, 560, 40,
              "mcp_client = Client(config)   # initialized once at worker startup\n"
              "# stdio: one subprocess  |  HTTP: one persistent connection"))

# Workflow A — polling
E.append(R("wfa", 262, 208, 395, 220, bg="#ffd43b", stroke="#f08c00", sw=2))
E.append(T("wfa_lbl", 270, 210, 250, 20, "TaskTracker Workflow A",
           size=13, color="#f08c00", align="left"))
E.append(T("wfa_id",  270, 228, 300, 16, "task_id: invoice-abc  ·  state: polling",
           size=11, color="#495057", align="left"))
E.append(CODE("wfa_code", 270, 252, 374, 90,
              "while True:\n"
              "    status = await workflow.execute_activity(\n"
              "        poll_task_status, task_id, ...)\n"
              "    if status == 'input_required': break\n"
              "    if status in TERMINAL: return ...\n"
              "    await workflow.sleep(timedelta(seconds=2))"))

# Workflow B — awaiting HITL (updated with signals/queries)
E.append(R("wfb", 672, 208, 415, 220, bg="#ffd43b", stroke="#f08c00", sw=2))
E.append(T("wfb_lbl", 680, 210, 300, 20, "TaskTracker Workflow B",
           size=13, color="#f08c00", align="left"))
E.append(T("wfb_id",  680, 228, 370, 16,
           "task_id: invoice-xyz  ·  state: awaiting_hitl",
           size=11, color="#495057", align="left"))
E.append(CODE("wfb_code", 680, 248, 396, 105,
              "@workflow.signal\n"
              "def elicitation_received(self, details): ...\n"
              "\n"
              "@workflow.signal\n"
              "def user_decision(self, decision): ...\n"
              "\n"
              "@workflow.query\n"
              "def get_pending_decision(self): ...\n"
              "\n"
              "await workflow.wait_condition(\n"
              "    lambda: self._pending_decision is not None,\n"
              "    timeout=timedelta(days=5))"))

# Activity Pool (updated — handle_elicitation replacing send_task_result)
E.append(R("ap_out", 262, 448, 820, 252, bg="#e7f5ff", stroke="#228be6", sw=2))
E.append(T("ap_lbl", 270, 450, 600, 22,
           "Activity Executions  (concurrent — all use global mcp_client)",
           size=13, color="#228be6", align="left"))

E.append(R("ap_a1", 275, 482, 248, 64, bg="#d0ebff", stroke="#228be6"))
E.append(T("ap_a1_t", 275, 482, 248, 28, "poll_task_status('abc')", size=12))
E.append(CODE("ap_a1_c", 283, 510, 232, 32,
              "JSON-RPC id: 42  →\n← response matched by id"))

E.append(R("ap_a2", 535, 482, 248, 64, bg="#d0ebff", stroke="#228be6"))
E.append(T("ap_a2_t", 535, 482, 248, 28, "poll_task_status('xyz')", size=12))
E.append(CODE("ap_a2_c", 543, 510, 232, 32,
              "JSON-RPC id: 43  →\n← response matched by id"))

# handle_elicitation (key activity — wider box)
E.append(R("ap_a3", 275, 558, 510, 122, bg="#d0ebff", stroke="#228be6", sw=2))
E.append(T("ap_a3_t", 275, 560, 300, 22, "handle_elicitation('xyz')", size=12,
           align="left"))
E.append(CODE("ap_a3_c", 283, 582, 490, 90,
              "1. calls tasks/result(taskId)  →  server sends elicitation back\n"
              "2. signals workflow: elicitation_received(prompt, schema)\n"
              "3. polls query get_pending_decision (heartbeating)\n"
              "4. on answer: returns ElicitResult  →  server gets decision\n"
              "   [if activity times out: Temporal retries → fresh connection]"))

E.append(R("ap_mux", 797, 558, 278, 122, bg="#a5d8ff", stroke="#228be6", sw=2))
E.append(T("ap_mux_t", 797, 558, 278, 122,
           "JSON-RPC Multiplexing\n\nEach request: unique id\nResponses: any order\nClient matches by id\n\nRetry policy = connection\nlifecycle policy",
           size=11, color="#1864ab"))

E.append(A("ap_up1", 399, 482, 650, 188, bidir=True, dashed=True, color="#2f9e44", sw=1))
E.append(A("ap_up2", 659, 482, 660, 188, bidir=True, dashed=True, color="#2f9e44", sw=1))

# --- MCP Server ---
E.append(R("ms_out", 1150, 48, 390, 300, bg="#e7f5ff", stroke="#228be6", sw=3))
E.append(T("ms_lbl", 1162, 52, 360, 26, "MCP Server", size=15, color="#228be6",
           align="left"))
E.append(T("ms_sub", 1162, 74, 310, 18, "async_mcp/server.py", size=11,
           color="#868e96", align="left"))
E.append(R("ms_pi", 1168, 102, 360, 68, bg="#d0ebff", stroke="#228be6"))
E.append(T("ms_pi_t", 1168, 102, 360, 68,
           "process_invoice tool\ntask=TaskConfig(mode='required')", size=12))
E.append(R("ms_th", 1168, 182, 360, 140, bg="#d0ebff", stroke="#228be6"))
E.append(T("ms_th_t", 1168, 182, 360, 140,
           "Custom Task Handlers\n(temporal_task_handlers.py)\n\ntasks/get\ntasks/result  (sends elicitation)\ntasks/list · tasks/cancel",
           size=12))

# --- Server-side Temporal ---
E.append(R("st_out", 1150, 378, 390, 340, bg="#ffe8cc", stroke="#e67700", sw=3))
E.append(T("st_lbl", 1162, 382, 360, 26, "Temporal Server (server-side)", size=15,
           color="#e67700", align="left"))
E.append(R("st_iwa", 1168, 418, 360, 68, bg="#ffd8a8", stroke="#e67700"))
E.append(T("st_iwa_t", 1168, 418, 360, 68,
           "InvoiceWorkflow: invoice-abc\nstate: PENDING-VALIDATION", size=12))
E.append(R("st_iwb", 1168, 498, 360, 68, bg="#ffd8a8", stroke="#e67700"))
E.append(T("st_iwb_t", 1168, 498, 360, 68,
           "InvoiceWorkflow: invoice-xyz\nstate: PENDING-APPROVAL", size=12))
E.append(R("st_pl",  1168, 578, 360, 68, bg="#ffd8a8", stroke="#e67700"))
E.append(T("st_pl_t", 1168, 578, 360, 68,
           "PayLineItem (child workflows)\nwaiting on due dates", size=12))

E.append(A("s_ia", 1340, 322, 1340, 418, bidir=True, color="#e67700"))
E.append(T("s_ia_t", 1345, 354, 130, 32, "Temporal\nPython SDK", size=11,
           color="#e67700", align="left"))
E.append(A("xb", 1082, 148, 1150, 175, bidir=True, color="#228be6", sw=2))
E.append(T("xb_t", 1040, 112, 400, 28,
           "MCP protocol  (stdio  or  streamable HTTP)",
           size=11, color="#228be6"))


# ============================================================
# SEQUENCE DIAGRAM  (y: 760 – ~2600)
# ============================================================

SY = 760

E.append(T("sq_h", 0, SY, 1560, 36,
           "Sequence: Task Lifecycle  (start → poll → HITL with elicitation → complete)",
           size=22, color="#1864ab"))

# Participant center-x
PX = {
    "ui":    85,
    "wf":    300,
    "act":   570,
    "mcp":   860,
    "stemp": 1120,
}
PW, PH = 160, 52
PLABELS = {
    "ui":    "UI\n(any type)",
    "wf":    "TaskTracker\nWorkflow",
    "act":   "Activity\n+ mcp_client",
    "mcp":   "MCP Server",
    "stemp": "Server\nTemporal",
}

LL_TOP = SY + 122
LL_BOT = SY + 1750

for k, cx in PX.items():
    x = cx - PW // 2
    E.append(R(f"p_{k}", x, SY + 58, PW, PH, bg="#f1f3f5", stroke="#495057", sw=2))
    E.append(T(f"p_{k}_t", x, SY + 58, PW, PH, PLABELS[k], size=12))
    E.append(L(f"ll_{k}", cx, LL_TOP, 0, LL_BOT - LL_TOP))

_y = [LL_TOP + 20]


def seq(eid, fp, tp, lbl, color="#495057", dashed=False, note=None, dy=50):
    y0 = _y[0]
    x1, x2 = PX[fp], PX[tp]
    dx = x2 - x1
    E.append({
        **BASE,
        "id": eid, "type": "arrow",
        "x": x1, "y": y0, "width": abs(dx), "height": 0,
        "backgroundColor": "transparent", "strokeColor": color,
        "strokeWidth": 2, "roughness": 0,
        "strokeStyle": "dashed" if dashed else "solid",
        "roundness": {"type": 2},
        "seed": ns(), "version": 1, "versionNonce": ns(),
        "points": [[0, 0], [dx, 0]],
        "lastCommittedPoint": None,
        "startBinding": None, "endBinding": None,
        "startArrowhead": None, "endArrowhead": "arrow",
    })
    lx = min(x1, x2) + 5
    lw = abs(dx) - 10
    E.append(T(eid + "_t", lx, y0 - 18, lw, 17, lbl, size=11, color=color))
    if note:
        E.append(T(eid + "_n", lx, y0 + 3, lw, 14, note, size=10, color="#868e96"))
        _y[0] += dy + 16
    else:
        _y[0] += dy


def phase_label(eid, label, color="#adb5bd"):
    """A horizontal phase separator label."""
    y0 = _y[0]
    E.append(L(eid + "_l", PX["ui"] - 60, y0, PX["stemp"] - PX["ui"] + 120, 0,
               color=color, sw=1, dashed=True))
    E.append(T(eid + "_t", PX["ui"] - 60, y0 - 16, 220, 15, label,
               size=11, color=color, align="left"))
    _y[0] += 20


def bracket(eid, label, y_start, y_end, color="#adb5bd", fp="wf", tp="mcp"):
    x1 = PX[fp] - 20
    x2 = PX[tp] + 20
    E.append(R(eid, x1, y_start, x2 - x1, y_end - y_start,
               bg="transparent", stroke=color, sw=1, dashed=True))
    E.append(T(eid + "_t", x1, y_start - 18, 120, 16, label,
               size=10, color=color, align="left"))


def annotation(eid, p, lbl, color="#f08c00"):
    y0 = _y[0]
    cx = PX[p]
    w = 280
    h = 36
    E.append(R(eid + "_r", cx - w // 2, y0, w, h,
               bg="#fff9db", stroke=color, sw=1, dashed=True))
    E.append(T(eid + "_t", cx - w // 2, y0, w, h, lbl, size=11, color=color))
    _y[0] += h + 14


# ── Phase 1: Start ───────────────────────────────────────────
phase_label("ph1", "Phase 1: Start task")
seq("s01", "ui",   "wf",    "start_workflow(invoice_json)  or  track_task(task_id)",
    color="#2f9e44")
seq("s02", "wf",   "act",   "execute_activity(start_task, invoice_json)",
    color="#f08c00")
seq("s03", "act",  "mcp",   "tools/call process_invoice  [JSON-RPC id:1]",
    color="#228be6")
seq("s04", "mcp",  "stemp", "start InvoiceWorkflow",
    color="#e67700")
seq("s05", "mcp",  "act",   "← taskId, status:working  [id:1]",
    color="#228be6", dashed=True)
seq("s06", "act",  "wf",    "← returns taskId",
    color="#f08c00", dashed=True)

# ── Phase 2: Polling loop ─────────────────────────────────────
phase_label("ph2", "Phase 2: Polling loop")
poll_sy = _y[0] - 8
seq("s07", "wf",  "act",  "execute_activity(poll_task_status, taskId)",
    color="#f08c00")
seq("s08", "act", "mcp",  "tasks/get(taskId)  [JSON-RPC id:2]",
    color="#228be6")
seq("s09", "mcp", "act",  "← status:working  [id:2]",
    color="#228be6", dashed=True)
seq("s10", "act", "wf",   "← 'working'  →  workflow.sleep(2s)",
    color="#f08c00", dashed=True)
seq("s11", "wf",  "act",  "execute_activity(poll_task_status, taskId)  [after sleep]",
    color="#f08c00")
seq("s12", "act", "mcp",  "tasks/get(taskId)  [JSON-RPC id:3]",
    color="#228be6")
seq("s13", "mcp", "act",  "← status:input_required  [id:3]",
    color="#e03131", dashed=True)
seq("s14", "act", "wf",   "← 'input_required'",
    color="#f08c00", dashed=True)
poll_ey = _y[0]
bracket("poll_b", "polling loop", poll_sy, poll_ey)

# ── Phase 3: Elicitation ──────────────────────────────────────
phase_label("ph3", "Phase 3: Elicitation  (activity holds connection; human decision decoupled)",
            color="#9c36b5")
elix_sy = _y[0] - 8

seq("s15", "wf",  "act",
    "execute_activity(handle_elicitation, taskId)",
    color="#f08c00",
    note="start_to_close_timeout=10min  retry_policy=unlimited")

seq("s16", "act", "mcp",
    "tasks/result(taskId)  [JSON-RPC id:4]  →  opens connection",
    color="#228be6",
    note="connection stays open until ElicitResult returned")

seq("s17", "mcp", "act",
    "← elicitation(prompt, schema)  [server sends details back over same connection]",
    color="#9c36b5")

seq("s18", "act", "wf",
    "signal(elicitation_received, {prompt, schema})",
    color="#f08c00",
    note="activity signals workflow — then polls, does NOT block on a response")

seq("s19", "wf",  "ui",
    "signal(elicitation_details, {prompt, schema})",
    color="#2f9e44",
    note="workflow notifies UI with full prompt + schema")

annotation("ann_hb",  "act",
           "activity heartbeating  ·  polling query get_pending_decision every 1s",
           color="#868e96")

seq("s20", "ui",  "wf",
    "signal(user_decision, 'approve')",
    color="#2f9e44",
    note="human responds via any UI  —  independent of activity state")

seq("s21", "act", "wf",
    "query get_pending_decision  →  'approve'  [poll finds it]",
    color="#f08c00",
    note="decision captured in workflow regardless of how many retries have occurred")

seq("s22", "act", "mcp",
    "ElicitResult('approve')  [still id:4 — same open connection]",
    color="#9c36b5",
    note="answer travels back over the tasks/result connection, not a new call")

seq("s23", "mcp", "stemp",
    "signal ApproveInvoice  →  workflow proceeds",
    color="#e67700")

seq("s24", "mcp", "act",
    "← tasks/result response  [connection closes]",
    color="#228be6", dashed=True)

seq("s25", "act", "wf",
    "← handle_elicitation returns",
    color="#f08c00", dashed=True)

elix_ey = _y[0]
bracket("elix_b", "elicitation", elix_sy, elix_ey, color="#9c36b5", fp="ui", tp="mcp")

# Timeout/retry note (to the right of the elicitation bracket)
note_x = PX["stemp"] + 40
note_y = elix_sy + 40
E.append(R("retry_box", note_x, note_y, 360, 160,
           bg="#fff0f6", stroke="#c2255c", sw=1, dashed=True))
E.append(T("retry_hdr", note_x + 8, note_y + 6, 344, 20,
           "On activity timeout (connection expired):",
           size=11, color="#c2255c", align="left"))
E.append(CODE("retry_body", note_x + 8, note_y + 28, 344, 124,
              "Temporal retries handle_elicitation\n"
              "→ fresh tasks/result call → new connection\n"
              "→ server re-elicits (still PENDING-APPROVAL)\n"
              "→ handler signals wf (idempotent)\n"
              "→ polls get_pending_decision\n"
              "\n"
              "If human already responded between retries:\n"
              "→ query returns decision immediately\n"
              "→ activity completes in one poll cycle\n"
              "\n"
              "Retry policy IS the connection lifecycle.",
              size=10, color="#862e9c"))

# ── Phase 4: Resume polling ───────────────────────────────────
phase_label("ph4", "Phase 4: Resume polling after elicitation")
poll2_sy = _y[0] - 8
seq("s26", "wf",  "act",  "execute_activity(poll_task_status, taskId)",
    color="#f08c00")
seq("s27", "act", "mcp",  "tasks/get(taskId)  [JSON-RPC id:5]",
    color="#228be6")
seq("s28", "mcp", "act",  "← status:completed  [id:5]",
    color="#2f9e44", dashed=True)
seq("s29", "act", "wf",   "← 'completed'",
    color="#f08c00", dashed=True)
poll2_ey = _y[0]
bracket("poll2_b", "polling loop", poll2_sy, poll2_ey)

# ── Phase 5: Fetch final result ───────────────────────────────
phase_label("ph5", "Phase 5: Fetch final result")
seq("s30", "wf",  "act",  "execute_activity(get_task_result, taskId)",
    color="#f08c00")
seq("s31", "act", "mcp",  "tasks/result(taskId)  [JSON-RPC id:6]",
    color="#228be6",
    note="terminal state: no elicitation — server returns CallToolResult immediately")
seq("s32", "mcp", "act",  "← CallToolResult('PAID')  [id:6]",
    color="#2f9e44", dashed=True)
seq("s33", "act", "wf",   "← 'Invoice PAID'",
    color="#f08c00", dashed=True)
seq("s34", "wf",  "wf",   "")   # placeholder for workflow.return
E.pop()  # remove that placeholder arrow
_y[0] -= 50

seq("s35", "ui",  "wf",   "query result  (or await workflow handle)",
    color="#2f9e44")
seq("s36", "wf",  "ui",   "← 'Invoice PAID'",
    color="#2f9e44", dashed=True)


# ============================================================
# Write
# ============================================================
diagram = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": E,
    "appState": {
        "gridSize": None,
        "viewBackgroundColor": "#ffffff",
    },
    "files": {},
}

out = "docs/design/client-architecture.excalidraw"
with open(out, "w") as f:
    json.dump(diagram, f, indent=2)

print(f"Wrote {out}  ({len(E)} elements)")
